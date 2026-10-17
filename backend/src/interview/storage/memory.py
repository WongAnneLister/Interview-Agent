"""进程内的对象存储，供单元测试和不连对象存储的本地路径使用。"""

from __future__ import annotations

import time

from interview.storage.base import ObjectNotFound, require_expires


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def put(self, bucket: str, key: str, data: bytes, *, content_type: str) -> None:
        del content_type  # 内存实现不按类型区分；签名与 S3 接口保持一致
        self.objects[(bucket, key)] = data

    async def get(self, bucket: str, key: str) -> bytes:
        try:
            return self.objects[(bucket, key)]
        except KeyError:
            raise ObjectNotFound(bucket, key) from None

    async def delete(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)

    async def presign(self, bucket: str, key: str, *, expires_s: int) -> str:
        require_expires(expires_s)
        expires_at = int(time.time()) + expires_s
        return f"memory://{bucket}/{key}?exp={expires_at}"
