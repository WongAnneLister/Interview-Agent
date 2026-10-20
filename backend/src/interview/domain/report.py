"""面试报告生成（PR-21 实现）。

本 PR 只提供入口：面试进入 ended、状态写好之后由编排器的 on_ended 调用。
PR-21 在这里启动后台任务生成报告；编排器会 await 这个函数，
所以它必须立即返回，不能在这里等报告生成完。
"""

from __future__ import annotations

import uuid

import structlog

log = structlog.get_logger(__name__)


async def request_report(interview_id: uuid.UUID, *, user_id: uuid.UUID) -> None:
    """面试结束后触发报告生成。PR-21 之前只记一条日志。"""
    log.info("report_requested", interview_id=str(interview_id), user_id=str(user_id))
