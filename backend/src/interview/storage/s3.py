"""S3 兼容对象存储（开发用 SeaweedFS，生产可换 OSS）。

只用标准 S3 API（aioboto3），不用存储产品专有 SDK。SeaweedFS 不支持虚拟主机式访问，
所以固定 path-style 寻址。每次调用单独建连，超时见 S3_TIMEOUT_S。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol, Self, cast

import aioboto3  # pyright: ignore[reportMissingTypeStubs]
import structlog
from botocore.config import Config  # pyright: ignore[reportMissingTypeStubs]
from botocore.exceptions import BotoCoreError, ClientError  # pyright: ignore[reportMissingTypeStubs]

from interview.config import S3Settings
from interview.observability.tracing import span
from interview.storage.base import ObjectNotFound, StorageError, require_expires

S3_TIMEOUT_S = 30.0

log = structlog.get_logger(__name__)

type ClientFactory = Callable[[], AbstractAsyncContextManager[Any]]


class S3Body(Protocol):
    async def read(self, amt: int | None = None) -> bytes: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None: ...


class S3Client(Protocol):
    async def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str
    ) -> object: ...

    async def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]: ...

    async def delete_object(self, *, Bucket: str, Key: str) -> object: ...

    async def generate_presigned_url(
        self, ClientMethod: str, *, Params: Mapping[str, str], ExpiresIn: int
    ) -> str: ...


def client_params(settings: S3Settings, timeout_s: float) -> dict[str, Any]:
    """aioboto3 Session.client('s3', **params) 的参数。"""
    return {
        "endpoint_url": settings.endpoint,
        "aws_access_key_id": settings.access_key.get_secret_value(),
        "aws_secret_access_key": settings.secret_key.get_secret_value(),
        "region_name": settings.region,
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=timeout_s,
            read_timeout=timeout_s,
            retries={"max_attempts": 1},
        ),
    }


def _from_boto(exc: Exception, bucket: str, key: str) -> StorageError:
    if isinstance(exc, ClientError):
        response = cast(Mapping[str, Any], getattr(exc, "response", {}))
        error = cast(Mapping[str, Any], response.get("Error", {}))
        code = str(error.get("Code", ""))
        meta = cast(Mapping[str, Any], response.get("ResponseMetadata", {}))
        if code in {"NoSuchKey", "404", "NotFound"} or meta.get("HTTPStatusCode") == 404:
            return ObjectNotFound(bucket, key)
    log.warning("s3_error", error=type(exc).__name__)
    return StorageError("对象存储调用失败")


class S3ObjectStore:
    def __init__(
        self,
        settings: S3Settings,
        *,
        client_factory: ClientFactory | None = None,
        timeout_s: float = S3_TIMEOUT_S,
    ) -> None:
        self._settings = settings
        self._timeout_s = timeout_s
        self._session = aioboto3.Session()
        self._client_factory = client_factory

    @asynccontextmanager
    async def _connect(self) -> AsyncGenerator[S3Client]:
        if self._client_factory is not None:
            async with self._client_factory() as client:
                yield cast(S3Client, client)
            return
        # aioboto3 没有类型；Session.client 返回异步上下文管理器
        opened = cast(
            AbstractAsyncContextManager[S3Client],
            self._session.client("s3", **client_params(self._settings, self._timeout_s)),  # pyright: ignore[reportUnknownMemberType]
        )
        async with opened as client:
            yield client

    async def _call[T](
        self,
        operation: str,
        bucket: str,
        key: str,
        action: Callable[[S3Client], Awaitable[T]],
    ) -> T:
        with span(f"s3.{operation}", provider="s3") as current:
            current.set_attribute("s3.bucket", bucket)
            current.set_attribute("s3.key", key)
            try:
                async with self._connect() as client:
                    return await action(client)
            except (ClientError, BotoCoreError) as exc:
                # botocore 1.40 起 ClientError 不再继承 BotoCoreError，两边都要接
                raise _from_boto(exc, bucket, key) from exc

    async def put(self, bucket: str, key: str, data: bytes, *, content_type: str) -> None:
        await self._call(
            "put",
            bucket,
            key,
            lambda client: client.put_object(
                Bucket=bucket, Key=key, Body=data, ContentType=content_type
            ),
        )

    async def get(self, bucket: str, key: str) -> bytes:
        async def read(client: S3Client) -> bytes:
            response = await client.get_object(Bucket=bucket, Key=key)
            body = cast(S3Body, response["Body"])
            async with body:
                return await body.read()

        return await self._call("get", bucket, key, read)

    async def delete(self, bucket: str, key: str) -> None:
        await self._call(
            "delete", bucket, key, lambda client: client.delete_object(Bucket=bucket, Key=key)
        )

    async def presign(self, bucket: str, key: str, *, expires_s: int) -> str:
        require_expires(expires_s)
        return await self._call(
            "presign",
            bucket,
            key,
            lambda client: client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires_s,
            ),
        )
