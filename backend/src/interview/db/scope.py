"""会话级 user_id 拦截。

会话工厂创建的会话都是 UserScopedSession。会话先用 bind_user() 绑定一个用户，之后：
- 查询、ORM UPDATE / DELETE 涉及 UserOwned 表时，自动加上 user_id = 绑定用户 的条件；
  会话没有绑定用户时直接抛 MissingUserScopeError。
- flush 时新增或修改的 UserOwned 对象，user_id 必须等于绑定用户，否则抛 UserScopeMismatchError。

不覆盖的情况：text() 原生 SQL、直接对 Table 的 Core 语句、ORM 批量 INSERT。
这些写法只允许出现在迁移和种子脚本里；唯一例外是 observability/budget.py 的每日总支出
（跨用户的系统级熔断，只返回一个总数）。
"""

from __future__ import annotations

import uuid
from functools import cache
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, UOWTransaction, with_loader_criteria

from interview.db.models import Base, UserOwned

_USER_KEY = "scope_user_id"


class UserScopeError(RuntimeError):
    """违反按 user_id 隔离数据的约定。"""


class MissingUserScopeError(UserScopeError):
    """访问 UserOwned 表时没有给出 user_id。"""


class UserScopeMismatchError(UserScopeError):
    """对象或会话的 user_id 与绑定的用户不一致。"""


class UserScopedSession(Session):
    """带 user_id 拦截的同步会话类（异步会话通过 sync_session_class 使用它）。"""


def bind_user(session: Session, user_id: uuid.UUID | None) -> uuid.UUID:
    """把会话绑定到一个用户并返回 user_id；同一会话不能改绑到另一个用户。"""
    if user_id is None:
        raise MissingUserScopeError("必须提供 user_id")
    current = session.info.get(_USER_KEY)
    if current is not None and current != user_id:
        raise UserScopeMismatchError("会话已绑定到另一个用户")
    session.info[_USER_KEY] = user_id
    return user_id


def bound_user(session: Session) -> uuid.UUID | None:
    value = session.info.get(_USER_KEY)
    return value if isinstance(value, uuid.UUID) else None


@cache
def _user_owned_classes() -> tuple[type[UserOwned], ...]:
    return tuple(m.class_ for m in Base.registry.mappers if issubclass(m.class_, UserOwned))


def _touches_user_owned(state: ORMExecuteState) -> bool:
    return any(issubclass(m.class_, UserOwned) for m in state.all_mappers)


@event.listens_for(UserScopedSession, "do_orm_execute")
def _filter_by_user(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if not _touches_user_owned(state):
        return
    user_id = bound_user(state.session)
    if user_id is None:
        raise MissingUserScopeError("查询 UserOwned 表前必须用 bind_user() 绑定用户")
    # 对每个 UserOwned 模型都加条件：语句里没出现的模型不受影响，出现在子查询、join 里的也会被过滤
    state.statement = state.statement.options(
        *(
            with_loader_criteria(cls, cls.user_id == user_id, include_aliases=True)
            for cls in _user_owned_classes()
        )
    )


@event.listens_for(UserScopedSession, "before_flush")
def _check_owner(session: Session, _flush: UOWTransaction, _instances: Any) -> None:
    pending = [obj for obj in (*session.new, *session.dirty) if isinstance(obj, UserOwned)]
    if not pending:
        return
    user_id = bound_user(session)
    if user_id is None:
        raise MissingUserScopeError("写入 UserOwned 表前必须用 bind_user() 绑定用户")
    for obj in pending:
        if obj.user_id != user_id:
            raise UserScopeMismatchError(f"{type(obj).__name__}.user_id 与会话绑定的用户不一致")
