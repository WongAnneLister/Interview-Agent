"""对象存储接口。业务代码只依赖这里，不直接使用厂商 SDK。

put / get / delete 读写对象；presign 生成短时效的下载链接（有效期由调用方传入）。
单元测试使用 storage/memory.py；开发环境的 SeaweedFS 和生产可替换的 OSS 用 storage/s3.py。
"""

from __future__ import annotations

from typing import Protocol


class StorageError(Exception):
    """对象存储调用失败。消息里不要放密钥、连接串或对象内容。"""


class ObjectNotFound(StorageError):
    """桶里没有这个 key。"""

    def __init__(self, bucket: str, key: str) -> None:
        super().__init__("对象不存在")
        self.bucket = bucket
        self.key = key


def require_expires(expires_s: int) -> None:
    """签名链接必须有正的有效期。"""
    if expires_s <= 0:
        raise ValueError("签名链接有效期必须大于 0 秒")


class ObjectStore(Protocol):
    async def put(self, bucket: str, key: str, data: bytes, *, content_type: str) -> None: ...

    async def get(self, bucket: str, key: str) -> bytes: ...

    async def delete(self, bucket: str, key: str) -> None: ...

    async def presign(self, bucket: str, key: str, *, expires_s: int) -> str: ...
