"""创建 v0 唯一的本地用户，并把它的 id 写入 .env 的 DEFAULT_USER_ID。

用法：make seed（先 make migrate）。可重复执行：用户已存在时只回写 id。
只读取 DATABASE_URL，不需要其他配置。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from interview.config import DEFAULT_ENV_FILE, DatabaseSettings
from interview.db.models import User
from interview.db.session import create_engine, make_sessionmaker

SEED_EMAIL = "local@localhost"
ENV_KEY = "DEFAULT_USER_ID"


async def ensure_default_user(engine: AsyncEngine, email: str = SEED_EMAIL) -> uuid.UUID:
    """按邮箱（不区分大小写）查找用户，不存在则创建；返回用户 id。"""
    async with make_sessionmaker(engine)() as session:
        user = await session.scalar(select(User).where(func.lower(User.email) == email.lower()))
        if user is None:
            user = User(email=email)
            session.add(user)
            await session.commit()
        return user.id


def write_env_var(path: Path, key: str, value: str) -> None:
    """替换 .env 里已有的 key=...；没有则追加到末尾。其他行原样保留。"""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"{key}={value}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    path.write_text(text, encoding="utf-8")


async def main(env_file: Path = DEFAULT_ENV_FILE) -> None:
    url = DatabaseSettings(_env_file=env_file).url.get_secret_value()  # pyright: ignore[reportCallIssue]
    engine = create_engine(url)
    try:
        user_id = await ensure_default_user(engine)
    finally:
        await engine.dispose()
    write_env_var(env_file, ENV_KEY, str(user_id))
    print(f"本地用户 {SEED_EMAIL}：{ENV_KEY}={user_id} 已写入 {env_file.name}")


if __name__ == "__main__":
    asyncio.run(main())
