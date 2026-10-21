"""文字面试 WebSocket：WS /ws/interviews/{id}/text。

协议（消息都是 JSON 文本帧，用 type 区分，模型见下方并导出到 OpenAPI 的 components）：
- 客户端 → 服务端：user_text（候选人的一轮回答）、command（pause / resume / repeat / skip / end）、
  ping（心跳，前端每 20 秒发一次，服务端回 pong）。格式错误只回 error，不断开。
- 服务端 → 客户端：编排器事件（interviewer_* / state / stage / ended）、pong、error。

连接规则：
- 只有面试的所属用户能连；别人的面试和不存在的面试一样，先回 error 再以 4404 关闭。
- 同一场面试同时只允许一个连接，第二个以 4409 关闭；
  已结束（completed / aborted / failed）以 4410 关闭。
- 新面试连上后由服务端开场；进行中的面试重新连上时只推当前阶段和状态，
  不重复说话（完整续面见 PR-39）。

时间由服务端掌握，前端不发 tick：
- 服务端每 WS_TICK_INTERVAL_S 秒调用一次 on_tick 推进阶段和思考超时；
  收尾语说完编排器自动进入 ended，服务端推送 ended 后以 1000 关闭，并触发报告生成入口。
- now_ms 从上次保存的 clock_ms 接着走，断线期间的时间不计入面试时长。

断线与超时：
- WS_IDLE_TIMEOUT_S 内收不到客户端任何消息（含 ping）视为断线，以 4408 关闭。
- 断线时正在进行的事件流被取消，生成器经 aclosing 一定会 aclose()；随后 checkpoint() 把状态写库，
  aclose() 等后台评估落库。面试保持 in_progress，重新连接可以继续。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncGenerator
from enum import IntEnum
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator
from pydantic.json_schema import JsonSchemaMode
from starlette.websockets import WebSocketState
from structlog.contextvars import bound_contextvars

from interview.api.deps import CurrentUserId, get_resources
from interview.api.resources import Resources
from interview.db.models import InterviewStatus
from interview.domain import report
from interview.domain.orchestrator import InterviewOrchestrator
from interview.domain.orchestrator_events import (
    CommandName,
    EndedEvent,
    InterviewerDelta,
    InterviewerDone,
    InterviewerSentence,
    OrchestratorEvent,
    StageEvent,
    StateEvent,
    WireEvent,
)
from interview.domain.orchestrator_store import InterviewNotFound
from interview.domain.plan import InterviewRepository
from interview.domain.state_machine import Phase
from interview.observability.prometheus import counter
from interview.observability.tracing import use_trace_id

router = APIRouter(tags=["interviews"])
log = structlog.get_logger(__name__)

MAX_TEXT_CHARS = 4000
MAX_FRAME_CHARS = 16_000
QUEUE_SIZE = 8
ENDED_STATUSES = {InterviewStatus.COMPLETED, InterviewStatus.ABORTED, InterviewStatus.FAILED}

_closed = counter("text_ws_closed", "文字面试 WebSocket 关闭次数", ["reason"])


class CloseCode(IntEnum):
    NORMAL = 1000
    INTERNAL = 1011
    IDLE = 4408
    NOT_FOUND = 4404
    BUSY = 4409
    ENDED = 4410


class UserTextMessage(BaseModel):
    type: Literal["user_text"]
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("回答不能为空")
        return value


class CommandMessage(BaseModel):
    type: Literal["command"]
    command: CommandName


class PingMessage(BaseModel):
    type: Literal["ping"]


TextClientMessage = Annotated[
    UserTextMessage | CommandMessage | PingMessage, Field(discriminator="type")
]


class PongEvent(WireEvent):
    type: Literal["pong"] = "pong"


class ErrorEvent(WireEvent):
    type: Literal["error"] = "error"
    code: str
    message: str
    trace_id: str


TextServerMessage = Annotated[
    InterviewerDelta
    | InterviewerSentence
    | InterviewerDone
    | StateEvent
    | StageEvent
    | EndedEvent
    | PongEvent
    | ErrorEvent,
    Field(discriminator="type"),
]

_client_adapter: TypeAdapter[UserTextMessage | CommandMessage | PingMessage] = TypeAdapter(
    TextClientMessage
)


def openapi_schemas() -> dict[str, Any]:
    """WebSocket 消息的 JSON Schema，并入 OpenAPI 的 components（WebSocket 路由不会自动导出）。"""
    template = "#/components/schemas/{model}"
    schemas: dict[str, Any] = {}
    exports: list[tuple[str, TypeAdapter[Any], JsonSchemaMode]] = [
        ("TextClientMessage", _client_adapter, "validation"),
        ("TextServerMessage", TypeAdapter(TextServerMessage), "serialization"),
    ]
    for name, adapter, mode in exports:
        schema = dict(adapter.json_schema(mode=mode, ref_template=template))
        schemas.update(schema.pop("$defs", {}))
        schemas[name] = schema
    return schemas


class _Tick:
    pass


type _Item = UserTextMessage | CommandMessage | _Tick


def _raise_if_cancelling() -> None:
    """取消和超时同时到达时，超时可能把取消吞掉（变成 TimeoutError），这里补抛。"""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


class TextSession:
    """一个连接的生命周期：读消息、按顺序驱动编排器、推事件。"""

    def __init__(
        self,
        websocket: WebSocket,
        orchestrator: InterviewOrchestrator,
        *,
        trace_id: str,
        idle_timeout_s: float,
        tick_interval_s: float,
    ) -> None:
        self._ws = websocket
        self._orchestrator = orchestrator
        self._trace_id = trace_id
        self._idle_timeout_s = idle_timeout_s
        self._tick_interval_s = tick_interval_s
        self._inbox: asyncio.Queue[_Item] = asyncio.Queue(QUEUE_SIZE)
        self._send_lock = asyncio.Lock()
        state = orchestrator.state
        self._base_ms = state.clock_ms if state is not None else 0
        self._started = time.monotonic()
        self.close_reason = "client_gone"

    def now_ms(self) -> int:
        return self._base_ms + int((time.monotonic() - self._started) * 1000)

    async def send(self, message: WireEvent) -> bool:
        if self._ws.application_state is not WebSocketState.CONNECTED:
            return False
        try:
            async with self._send_lock:
                await self._ws.send_text(message.model_dump_json())
        except (WebSocketDisconnect, RuntimeError, OSError):
            return False
        return True

    async def close(self, code: CloseCode, reason: str) -> None:
        self.close_reason = reason
        if self._ws.application_state is WebSocketState.CONNECTED:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError):
                await self._ws.close(code, reason)

    def error(self, code: str, message: str) -> ErrorEvent:
        return ErrorEvent(code=code, message=message, trace_id=self._trace_id)

    async def run(self) -> None:
        reader = asyncio.create_task(self._read())
        driver = asyncio.create_task(self._drive())
        try:
            await asyncio.wait({reader, driver}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # 断线时取消正在进行的事件流：_forward 里的 aclosing 保证生成器被 aclose()
            for task in (reader, driver):
                task.cancel()
            results = await asyncio.gather(reader, driver, return_exceptions=True)
        failure = next(
            (
                r
                for r in results
                if isinstance(r, Exception) and not isinstance(r, WebSocketDisconnect)
            ),
            None,
        )
        if failure is not None:
            log.error("text_ws_failed", error=type(failure).__name__)
            await self.send(self.error("internal_error", "服务器内部错误，请稍后重连"))
            await self.close(CloseCode.INTERNAL, "internal_error")

    async def _read(self) -> None:
        while True:
            try:
                async with asyncio.timeout(self._idle_timeout_s):
                    message = await self._ws.receive()
            except TimeoutError:
                _raise_if_cancelling()
                await self.send(self.error("idle_timeout", "长时间没有收到消息，连接已断开"))
                await self.close(CloseCode.IDLE, "idle_timeout")
                return
            if message["type"] == "websocket.disconnect":
                return
            raw = message.get("text")
            if raw is None or len(raw) > MAX_FRAME_CHARS:
                await self.send(self.error("invalid_message", "只接受不超过上限的 JSON 文本消息"))
                continue
            try:
                parsed = _client_adapter.validate_json(raw)
            except ValidationError:
                await self.send(self.error("invalid_message", "消息格式不正确"))
                continue
            if isinstance(parsed, PingMessage):
                await self.send(PongEvent())
                continue
            try:
                self._inbox.put_nowait(parsed)
            except asyncio.QueueFull:
                await self.send(self.error("too_many_messages", "消息太多，请等面试官说完"))

    async def _drive(self) -> None:
        orchestrator = self._orchestrator
        if orchestrator.state is None:
            if await self._forward(orchestrator.start(self.now_ms())):
                return
        else:
            for event in orchestrator.snapshot():
                await self.send(event)
        while True:
            try:
                item = await asyncio.wait_for(self._inbox.get(), self._tick_interval_s)
            except TimeoutError:
                _raise_if_cancelling()
                item = _Tick()
            now = self.now_ms()
            if isinstance(item, UserTextMessage):
                events = orchestrator.on_user_turn(item.text, now, now)
            elif isinstance(item, CommandMessage):
                events = orchestrator.on_command(item.command, now)
            else:
                events = orchestrator.on_tick(now)
            if await self._forward(events):
                return

    async def _forward(self, events: AsyncGenerator[OrchestratorEvent]) -> bool:
        """推送一个事件流，返回面试是否已结束。结束时以 1000 关闭连接。"""
        async with contextlib.aclosing(events) as stream:
            async for event in stream:
                await self.send(event)
                if isinstance(event, EndedEvent):
                    await self.close(CloseCode.NORMAL, "interview_ended")
                    return True
        return False


async def _reject(
    websocket: WebSocket, code: CloseCode, error: str, message: str, trace_id: str
) -> None:
    """先 accept 再关闭：浏览器拿不到握手失败的原因，只能读到关闭码和最后一条消息。"""
    await websocket.accept()
    body = ErrorEvent(code=error, message=message, trace_id=trace_id)
    await websocket.send_text(body.model_dump_json())
    await websocket.close(code, error)
    _closed.labels(reason=error).inc()
    log.info("text_ws_rejected", reason=error)


@router.websocket("/ws/interviews/{interview_id}/text")
async def text_interview(
    websocket: WebSocket,
    interview_id: uuid.UUID,
    user_id: CurrentUserId,
    resources: Annotated[Resources, Depends(get_resources)],
) -> None:
    trace_id = uuid.uuid4().hex
    with (
        bound_contextvars(trace_id=trace_id, interview_id=str(interview_id)),
        use_trace_id(int(trace_id, 16)),
    ):
        live = resources.live_interviews
        if interview_id in live:
            await _reject(
                websocket, CloseCode.BUSY, "already_connected", "这场面试已在其他窗口打开", trace_id
            )
            return
        live.add(interview_id)  # 检查和占用之间没有 await，同一进程内不会两个连接都通过
        try:
            await _serve(websocket, interview_id, user_id, resources, trace_id)
        finally:
            live.discard(interview_id)


async def _serve(
    websocket: WebSocket,
    interview_id: uuid.UUID,
    user_id: uuid.UUID,
    resources: Resources,
    trace_id: str,
) -> None:
    async with resources.sessionmaker() as session:
        interview = await InterviewRepository(session, user_id).get(interview_id)
        status = (
            interview.status if interview is not None and interview.user_id == user_id else None
        )
    if status in ENDED_STATUSES:
        await _reject(websocket, CloseCode.ENDED, "interview_ended", "面试已结束", trace_id)
        return

    async def on_ended(ended_id: uuid.UUID) -> None:
        # 与 POST 的 pending 落库和任务登记串行，避免结束回调同时创建同一报告行。
        async with resources.report_requests:
            await report.request_report(ended_id, user_id=user_id, runner=resources.reports)

    try:
        if status is None:
            raise InterviewNotFound("面试不存在")
        orchestrator = await InterviewOrchestrator.load(
            interview_id=interview_id,
            user_id=user_id,
            sessionmaker=resources.sessionmaker,
            providers=resources.providers,
            on_ended=on_ended,
        )
    except InterviewNotFound:
        await _reject(websocket, CloseCode.NOT_FOUND, "not_found", "面试不存在", trace_id)
        return
    if orchestrator.state is not None and orchestrator.state.phase is Phase.ENDED:
        await orchestrator.checkpoint()  # 上次进入 ended 后没来得及收尾：补做
        await orchestrator.aclose()
        await _reject(websocket, CloseCode.ENDED, "interview_ended", "面试已结束", trace_id)
        return

    await websocket.accept()
    settings = resources.settings.app
    text_session = TextSession(
        websocket,
        orchestrator,
        trace_id=trace_id,
        idle_timeout_s=settings.ws_idle_timeout_s,
        tick_interval_s=settings.ws_tick_interval_s,
    )
    started = time.monotonic()
    log.info("text_ws_connected", resumed=orchestrator.state is not None)
    try:
        await text_session.run()
    finally:
        try:
            await orchestrator.checkpoint()
        except Exception as exc:
            log.warning("text_ws_checkpoint_failed", error=type(exc).__name__)
        await orchestrator.aclose()
        await text_session.close(CloseCode.NORMAL, text_session.close_reason)
        _closed.labels(reason=text_session.close_reason).inc()
        log.info(
            "text_ws_closed",
            reason=text_session.close_reason,
            duration_s=round(time.monotonic() - started, 1),
        )
