"""评测结果：与上次对比、Markdown 渲染、Langfuse 分数上传。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from pydantic import SecretStr

from evals import langfuse_scores, results
from evals.metrics import Metric, SessionMetrics
from interview.config import LangfuseSettings


def metric(key: str, value: float | None, *, higher: bool = True) -> Metric:
    return Metric(key=key, name=key, value=value, target="-", passed=None, higher_is_better=higher)


def session(**kwargs: object) -> SessionMetrics:
    base: dict[str, object] = {
        "fixture": "backend",
        "persona": "strong",
        "interview_id": "00000000-0000-0000-0000-000000000001",
        "trace_id": "ab" * 16,
    }
    return SessionMetrics.model_validate(base | kwargs)


def run_result(run_id: str, metrics: list[Metric]) -> results.RunResult:
    return results.RunResult(
        run_id=run_id,
        mode="quick",
        personas=["strong"],
        fixtures=["backend"],
        prompt_versions={"interviewer": 1},
        models={"eval": "bailian/qwen"},
        metrics=metrics,
        sessions=[session(overall_score=4.5, notes=["追问 1：重复"])],
        cost_cny="1.2345",
        cost_by_role={"report": "0.5"},
        unpriced_events=0,
        wall_s=12,
    )


def test_compare_marks_regressions_by_direction() -> None:
    before = run_result("a", [metric("follow_up", 0.9), metric("duration", 0.05, higher=False)])
    notes = results.compare(
        [metric("follow_up", 0.8), metric("duration", 0.04, higher=False)], before
    )
    assert "⚠ 退化" in notes["follow_up"]
    assert "⚠" not in notes["duration"]
    assert results.compare([metric("follow_up", 0.8)], None) == {}


def test_write_compares_with_latest_earlier_run(tmp_path: Path) -> None:
    results.write(run_result("2026-11-01_100000", [metric("relevance", 1.0)]), tmp_path)
    results.write(run_result("2026-11-02_100000", [metric("relevance", 0.95)]), tmp_path)
    path = results.write(run_result("2026-11-03_100000", [metric("relevance", 0.5)]), tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "对比上次：2026-11-02_100000" in text
    assert "0.95 → 0.5" in text and "⚠ 退化" in text
    assert "backend/strong：追问 1：重复" in text
    saved = json.loads((tmp_path / "2026-11-03_100000.json").read_text(encoding="utf-8"))
    assert saved["cost_cny"] == "1.2345"


def test_session_scores_skip_missing_values_and_are_stable() -> None:
    m = session(overall_score=4.0, follow_ups=4, follow_ups_ok=3, duration_dev=0.1)
    first = langfuse_scores.session_scores("run", m, {"interviewer": 1})
    assert {s["name"] for s in first} == {
        "eval.overall_score",
        "eval.follow_up_ok",
        "eval.duration_dev",
    }
    assert first == langfuse_scores.session_scores("run", m, {"interviewer": 1})
    assert all(s["traceId"] == "ab" * 16 and s["dataType"] == "NUMERIC" for s in first)


async def test_upload_posts_array_and_reports_status() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"message": "Accepted"})

    settings = LangfuseSettings.model_construct(
        host="http://lf.test/", public_key="pk", secret_key=SecretStr("sk")
    )
    status = await langfuse_scores.upload(
        settings,
        "run",
        [session(overall_score=4.0), session(error="Boom", overall_score=1.0)],
        {},
        transport=httpx.MockTransport(handler),
    )
    assert status == "已上传 1 个分数到 Langfuse"
    (request,) = seen
    assert str(request.url) == "http://lf.test/api/public/scores"
    assert request.headers["authorization"].startswith("Basic ")
    assert [s["name"] for s in json.loads(request.content)] == ["eval.overall_score"]


async def test_upload_skips_when_not_configured() -> None:
    settings = LangfuseSettings.model_construct(host="", public_key="", secret_key=SecretStr(""))
    assert await langfuse_scores.upload(settings, "run", [session()], {}) == (
        "Langfuse 未配置，未上传分数"
    )
