"""把每场评测的分数写进 Langfuse（POST /api/public/scores，v4 接口收一个数组）。

分数挂在这场面试的 trace 上（OTel trace id），metadata 带提示词版本，便于不同版本对比。
Langfuse 没配置或不可用时只打印警告，不影响评测结果。
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import structlog

from evals.metrics import SessionMetrics
from interview.config import LangfuseSettings

TIMEOUT_S = 10.0

log = structlog.get_logger(__name__)


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def session_scores(
    run_id: str, m: SessionMetrics, prompt_versions: dict[str, int]
) -> list[dict[str, Any]]:
    values: dict[str, float | None] = {
        "eval.overall_score": m.overall_score,
        "eval.follow_up_ok": _ratio(m.follow_ups_ok, m.follow_ups),
        "eval.relevance": _ratio(m.questions_relevant, m.questions),
        "eval.evidence_verified": _ratio(m.evidence_verified, m.evidence_quotes),
        "eval.duration_dev": m.duration_dev,
        "eval.leaks": float(m.leaks) if m.leaks is not None else None,
    }
    metadata = {
        "run_id": run_id,
        "fixture": m.fixture,
        "persona": m.persona,
        "interview_id": m.interview_id,
        "prompt_versions": prompt_versions,
    }
    return [
        {
            # 同一次评测重复上传时 id 不变
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}/{m.interview_id}/{name}")),
            "traceId": m.trace_id,
            "name": name,
            "value": value,
            "dataType": "NUMERIC",
            "metadata": metadata,
        }
        for name, value in values.items()
        if value is not None
    ]


async def upload(
    settings: LangfuseSettings,
    run_id: str,
    sessions: list[SessionMetrics],
    prompt_versions: dict[str, int],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """返回一行状态说明，写进结果文件。transport 供测试注入。"""
    if not settings.configured:
        return "Langfuse 未配置，未上传分数"
    scores = [
        score
        for m in sessions
        if m.trace_id and m.error is None
        for score in session_scores(run_id, m, prompt_versions)
    ]
    if not scores:
        return "没有可上传的分数"
    auth = (settings.public_key, settings.secret_key.get_secret_value())
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as client:
            response = await client.post(
                f"{settings.host.rstrip('/')}/api/public/scores", json=scores, auth=auth
            )
    except httpx.HTTPError as exc:
        log.warning("eval_langfuse_failed", error=type(exc).__name__)
        return f"Langfuse 上传失败：{type(exc).__name__}"
    if response.status_code == 207:
        body = response.json()
        return f"Langfuse 部分接受：{body.get('accepted')} 条成功，{body.get('rejected')} 条被拒"
    if response.status_code >= 400:
        log.warning("eval_langfuse_rejected", status=response.status_code)
        return f"Langfuse 拒绝上传：HTTP {response.status_code}"
    return f"已上传 {len(scores)} 个分数到 Langfuse"
