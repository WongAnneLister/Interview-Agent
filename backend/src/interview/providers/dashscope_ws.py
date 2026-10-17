"""百炼 WebSocket 任务协议（run-task / finish-task）的公共部分，识别和合成共用。

- 连接：Authorization: Bearer <密钥>；保留 1 个空闲连接，空闲超过 IDLE_MAX_S 主动丢弃
  （服务端在任务结束后约 60 秒无新任务会断开）。warmup() 提前建好连接放进空闲槽。
- 开始任务：发 run-task 等 task-started；此前连接断开或建连失败时换新连接重试一次
  （还没发数据，重试是安全的）。
- task-finished 后连接可复用（下一个任务用新的 task_id）；task-failed 后服务端会关闭连接。
- 错误映射：握手状态码、task-failed 的 error_code / error_message、超时与断线
  都映射到 ProviderError。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Awaitable
from contextlib import aclosing, suppress
from typing import Any, cast

import structlog
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.protocol import State

from interview.providers.base import (
    AuthError,
    BadResponse,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)

IDLE_MAX_S = 50.0

log = structlog.get_logger(__name__)

type Message = tuple[dict[str, Any], dict[str, Any]]  # (header, payload)


def command(action: str, task_id: str, payload: dict[str, Any]) -> str:
    header = {"action": action, "task_id": task_id, "streaming": "duplex"}
    return json.dumps({"header": header, "payload": payload}, ensure_ascii=False)


class DashScopeTaskClient:
    def __init__(
        self,
        *,
        name: str,  # 日志事件前缀，如 asr / tts
        provider: str,
        model: str,
        api_key: str,
        url: str,
        timeout_s: float,
    ) -> None:
        self.name = name
        self.provider = provider
        self.model = model
        self.timeout_s = timeout_s
        self._api_key = api_key
        self._url = url
        self._idle: tuple[ClientConnection, float] | None = None

    # ---- 连接 ----

    async def warmup(self) -> None:
        """空闲槽里没有可用连接时新建一个；已有可用连接则什么都不做。"""
        conn = await self.guard_call(self._acquire())
        await self.release(conn)

    async def release(self, conn: ClientConnection) -> None:
        if self._idle is None and conn.state is State.OPEN:
            self._idle = (conn, time.monotonic())
        else:
            await self.discard(conn)

    async def discard(self, conn: ClientConnection | None) -> None:
        if conn is not None:
            with suppress(Exception):
                await conn.close()

    async def aclose(self) -> None:
        if self._idle is not None:
            conn, _ = self._idle
            self._idle = None
            await self.discard(conn)

    async def _acquire(self) -> ClientConnection:
        if self._idle is not None:
            conn, since = self._idle
            self._idle = None
            if conn.state is State.OPEN and time.monotonic() - since < IDLE_MAX_S:
                return conn
            await self.discard(conn)
        return await connect(
            self._url,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            open_timeout=self.timeout_s,
            close_timeout=self.timeout_s,
        )

    # ---- 任务 ----

    async def start_task(self, task_id: str, payload: dict[str, Any]) -> ClientConnection:
        """发 run-task 并等到 task-started，返回可以继续发送数据的连接。"""
        message = command("run-task", task_id, payload)
        for attempt in (1, 2):
            conn: ClientConnection | None = None
            try:
                conn = await self._acquire()
                async with asyncio.timeout(self.timeout_s):
                    await conn.send(message)
                    while True:
                        header, _ = self.parse(await conn.recv())
                        if header.get("event") == "task-started":
                            return conn
                        if header.get("event") == "task-failed":
                            raise self.task_error(header)
            except ProviderError:
                await self.discard(conn)
                raise
            except TimeoutError as exc:
                await self.discard(conn)
                raise self.error(ProviderTimeout, "建连或等待 task-started 超时") from exc
            except (ConnectionClosed, OSError) as exc:
                # 还没发数据：空闲连接已被服务端断开、或建连失败，换新连接重试一次
                await self.discard(conn)
                if attempt == 2:
                    raise self.error(ProviderUnavailable, "任务开始前连接失败") from exc
                log.warning(f"{self.name}_reconnect", task_id=task_id, error=type(exc).__name__)
            except BaseException:
                await self.discard(conn)
                raise
        raise AssertionError("unreachable")

    async def send(self, conn: ClientConnection, data: str | bytes) -> None:
        async with asyncio.timeout(self.timeout_s):
            await conn.send(data)

    async def next_raw(self, conn: ClientConnection, sender: asyncio.Task[None]) -> str | bytes:
        """等下一条服务端消息（文本或二进制）；发送端出错时立即抛出，发送端结束后每条消息都有超时。"""
        recv = asyncio.ensure_future(conn.recv())
        try:
            while True:
                waiting: set[asyncio.Future[Any]] = {recv} if sender.done() else {recv, sender}
                timeout = self.timeout_s if sender.done() else None
                done, _ = await asyncio.wait(
                    waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if recv in done:
                    return recv.result()
                if not done:
                    raise self.error(ProviderTimeout, "发出 finish-task 后等待结果超时")
                sender.result()  # 发送端出错时在这里抛出；正常结束则继续等消息
        finally:
            if not recv.done():
                recv.cancel()  # websockets 保证取消 recv() 不会丢数据

    async def guarded[T](self, inner: AsyncGenerator[T]) -> AsyncGenerator[T]:
        """包住一个任务生成器：把断线、超时、握手失败映射为 ProviderError。

        aclosing：外层被 aclose() 时立即关闭内层生成器（释放连接、写入用量），不等垃圾回收。
        """
        try:
            async with aclosing(inner) as items:
                async for item in items:
                    yield item
        except Exception as exc:  # 取消（CancelledError / GeneratorExit）不是 Exception，原样传出
            mapped = self.map_exception(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    async def guard_call[T](self, awaitable: Awaitable[T]) -> T:
        try:
            return await awaitable
        except Exception as exc:
            mapped = self.map_exception(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    def map_exception(self, exc: Exception) -> Exception:
        match exc:
            case ProviderError():
                return exc
            case InvalidStatus():
                return self.status_error(exc.response.status_code)
            case TimeoutError():
                return self.error(ProviderTimeout, "连接或发送超时")
            case ConnectionClosed() | OSError():
                return self.error(ProviderUnavailable, "连接失败或中断")
            case _:
                return exc

    # ---- 解析与错误 ----

    def parse(self, data: str | bytes) -> Message:
        try:
            message = json.loads(data)
        except ValueError as exc:
            raise self.error(BadResponse, "服务端消息不是 JSON") from exc
        if not isinstance(message, dict):
            raise self.error(BadResponse, "服务端消息格式不正确")
        body = cast(dict[str, Any], message)
        header: Any = body.get("header")
        payload: Any = body.get("payload") or {}
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise self.error(BadResponse, "服务端消息缺少 header / payload")
        return cast(dict[str, Any], header), cast(dict[str, Any], payload)

    def error(self, kind: type[ProviderError], message: str) -> ProviderError:
        return kind(
            f"{self.provider}/{self.model}: {message}", provider=self.provider, model=self.model
        )

    def status_error(self, status: int) -> ProviderError:
        if status in (401, 403):
            return self.error(AuthError, f"握手被拒绝 HTTP {status}（密钥无效或无权限）")
        if status == 429:
            return self.error(RateLimited, "握手被拒绝 HTTP 429")
        if status >= 500:
            return self.error(ProviderUnavailable, f"握手失败 HTTP {status}")
        return self.error(ProviderError, f"握手被拒绝 HTTP {status}")

    def task_error(self, header: dict[str, Any]) -> ProviderError:
        """task-failed 按 error_code / error_message 关键词归类（官方未公开完整错误码表）。"""
        code = str(header.get("error_code") or "")
        message = str(header.get("error_message") or "")
        text = f"{code} {message}".lower()
        kind: type[ProviderError] = ProviderError
        if any(k in text for k in ("throttl", "ratequota", "rate limit", "too many")):
            kind = RateLimited
        elif any(k in text for k in ("auth", "apikey", "api key", "accessdenied", "arrearage")):
            kind = AuthError
        elif "timeout" in text:
            kind = ProviderTimeout
        elif any(k in text for k in ("internal", "server error", "unavailable")):
            kind = ProviderUnavailable
        log.warning(f"{self.name}_task_failed", error_code=code, error_message=message)
        return self.error(kind, f"任务失败 {code}: {message}")
