"""对象存储：内存实现，以及 S3 客户端参数与调用映射（不连真实存储）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from interview.config import S3Settings
from interview.storage.base import ObjectNotFound, StorageError
from interview.storage.memory import MemoryObjectStore
from interview.storage.s3 import S3ObjectStore, client_params


def _settings() -> S3Settings:
    return S3Settings(
        endpoint="http://127.0.0.1:8333",
        access_key=SecretStr("ak"),
        secret_key=SecretStr("sk"),
    )


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> _Body:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def read(self, _amt: int | None = None) -> bytes:
        return self._data


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put", kwargs))
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]
        return {}

    async def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        data = self.objects.get((kwargs["Bucket"], kwargs["Key"]))
        if data is None:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        return {"Body": _Body(data)}

    async def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete", kwargs))
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)
        return {}

    async def generate_presigned_url(
        self, client_method: str, *, Params: dict[str, str], ExpiresIn: int
    ) -> str:
        # 参数名与 aiobotocore 的 generate_presigned_url 一致
        self.calls.append(
            ("presign", {"method": client_method, "params": Params, "expires": ExpiresIn})
        )
        return f"https://files.example/{Params['Bucket']}/{Params['Key']}?exp={ExpiresIn}"


def test_s3_client_uses_path_style_and_timeout() -> None:
    params = client_params(_settings(), timeout_s=12)
    config = params["config"]
    assert params["endpoint_url"] == "http://127.0.0.1:8333"
    assert params["aws_access_key_id"] == "ak"
    assert config.connect_timeout == 12
    assert config.read_timeout == 12
    assert config.s3["addressing_style"] == "path"
    assert config.signature_version == "s3v4"
    assert config.retries["max_attempts"] == 1


async def test_memory_put_get_delete_and_presign() -> None:
    store = MemoryObjectStore()
    await store.put("resumes", "a/1.pdf", b"%PDF-1", content_type="application/pdf")
    await store.put("recordings", "a/1.pdf", b"wav", content_type="audio/wav")
    assert await store.get("resumes", "a/1.pdf") == b"%PDF-1"
    url = await store.presign("resumes", "a/1.pdf", expires_s=60)
    assert "resumes/a/1.pdf" in url
    assert "exp=" in url
    await store.delete("resumes", "a/1.pdf")
    with pytest.raises(ObjectNotFound):
        await store.get("resumes", "a/1.pdf")
    assert await store.get("recordings", "a/1.pdf") == b"wav"


async def test_memory_presign_rejects_non_positive_ttl() -> None:
    store = MemoryObjectStore()
    with pytest.raises(ValueError):
        await store.presign("resumes", "missing.pdf", expires_s=0)


async def test_s3_store_maps_calls_and_missing_object() -> None:
    fake = _FakeS3()

    @asynccontextmanager
    async def opener() -> AsyncIterator[_FakeS3]:
        yield fake

    store = S3ObjectStore(_settings(), client_factory=opener)
    await store.put("resumes", "u/r.pdf", b"%PDF-1", content_type="application/pdf")
    assert await store.get("resumes", "u/r.pdf") == b"%PDF-1"
    url = await store.presign("resumes", "u/r.pdf", expires_s=600)
    assert url == "https://files.example/resumes/u/r.pdf?exp=600"
    await store.delete("resumes", "u/r.pdf")
    with pytest.raises(ObjectNotFound):
        await store.get("resumes", "u/r.pdf")

    put = fake.calls[0][1]
    assert put["Bucket"] == "resumes"
    assert put["Key"] == "u/r.pdf"
    assert put["Body"] == b"%PDF-1"
    assert put["ContentType"] == "application/pdf"
    presign = fake.calls[2][1]
    assert presign["method"] == "get_object"
    assert presign["expires"] == 600


async def test_s3_failure_hides_client_details() -> None:
    class _Broken:
        async def put_object(self, **_kwargs: Any) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "InvalidAccessKeyId", "Message": "secret-key-value"}},
                "PutObject",
            )

    @asynccontextmanager
    async def opener() -> AsyncIterator[_Broken]:
        yield _Broken()

    store = S3ObjectStore(_settings(), client_factory=opener)
    with pytest.raises(StorageError) as caught:
        await store.put("resumes", "u/r.pdf", b"%PDF-1", content_type="application/pdf")
    assert "secret-key-value" not in str(caught.value)
