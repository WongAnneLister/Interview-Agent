"""仓储基类：业务代码访问 UserOwned 表的入口。

构造时必须给 user_id（为空直接报错），并把会话绑定到这个用户，
之后会话级拦截（db/scope.py）会给所有查询加上 user_id 条件，作为第二道防线。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from interview.db.models import UserOwned
from interview.db.scope import UserScopeMismatchError, bind_user


class UserScopedRepository[M: UserOwned]:
    """子类设置 model，例如 `class TurnRepository(UserScopedRepository[Turn]): model = Turn`。"""

    model: type[M]

    def __init__(self, session: AsyncSession, user_id: uuid.UUID | None) -> None:
        self.session = session
        self.user_id = bind_user(session.sync_session, user_id)  # 为空时抛 MissingUserScopeError

    def select(self) -> Select[tuple[M]]:
        """已带 user_id 条件的查询，调用方可以继续追加 where / order_by。"""
        return select(self.model).where(self.model.user_id == self.user_id)

    async def get(self, pk: Any) -> M | None:
        """按主键取一条；属于其他用户时返回 None。"""
        return await self.session.get(self.model, pk)

    async def list(self, *where: Any) -> Sequence[M]:
        result = await self.session.scalars(self.select().where(*where))
        return result.all()

    def add(self, obj: M) -> M:
        """加入会话；对象的 user_id 必须是本仓储的用户（不在这里自动填写，避免掩盖错误）。"""
        if obj.user_id != self.user_id:
            raise UserScopeMismatchError(f"{type(obj).__name__}.user_id 与仓储的用户不一致")
        self.session.add(obj)
        return obj

    async def delete(self, obj: M) -> None:
        if obj.user_id != self.user_id:
            raise UserScopeMismatchError(f"{type(obj).__name__}.user_id 与仓储的用户不一致")
        await self.session.delete(obj)
