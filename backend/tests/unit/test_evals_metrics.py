"""评测指标：汇总与达标判断。"""

from __future__ import annotations

import uuid

from evals.metrics import Metric, SessionMetrics, aggregate
from evals.stability import Stability


def metrics(fixture: str, persona: str, score: float | None, **kwargs: object) -> SessionMetrics:
    return SessionMetrics(
        fixture=fixture,
        persona=persona,
        interview_id=str(uuid.uuid4()),
        trace_id="",
        overall_score=score,
        **kwargs,  # type: ignore[arg-type]
    )


def by_key(items: list[Metric]) -> dict[str, Metric]:
    return {m.key: m for m in items}


def test_aggregate_targets() -> None:
    sessions = [
        metrics("a", "strong", 4.5, follow_ups=5, follow_ups_ok=4, duration_dev=0.1),
        metrics(
            "a",
            "weak",
            1.5,
            questions=10,
            questions_relevant=9,
            evidence_quotes=3,
            evidence_verified=3,
        ),
        metrics("a", "average", 3.0),
        metrics("a", "nervous", 2.4),
        metrics("a", "adversarial", 2.0, leaks=0),
        metrics("b", "strong", 3.5, duration_dev=0.2),
        metrics("b", "weak", 2.0),
        metrics("b", "verbose", 3.0, asked_to_summarize=True, hit_turn_cap=True),
    ]
    stability = Stability(uuid.uuid4(), 5, {"q1": [3, 3, 4, 3, 3]}, [3.0, 3.1, 3.0, 3.0, 3.0])
    m = by_key(aggregate(sessions, stability))
    assert (m["discrimination"].value, m["discrimination"].passed) == (1.5, False)
    assert m["stability_question"].passed and m["stability_overall"].passed
    assert (m["follow_up"].value, m["follow_up"].passed) == (0.8, True)
    assert (m["relevance"].value, m["relevance"].passed) == (0.9, True)
    assert (m["evidence"].value, m["evidence"].passed) == (1.0, True)
    assert (m["duration"].value, m["duration"].passed) == (0.2, False)
    assert (m["leaks"].value, m["leaks"].passed) == (0.0, True)
    assert (m["manipulation"].value, m["manipulation"].passed) == (-1.0, True)
    assert (m["nervous"].value, m["nervous"].passed) == (0.6, False)
    assert m["verbose_summary"].value == 1.0
    assert (m["completion"].value, m["completion"].passed) == (0.875, False)


def test_aggregate_without_pairs_has_no_verdict() -> None:
    m = by_key(aggregate([metrics("a", "average", None, error="Boom")], None))
    assert m["discrimination"].passed is None
    assert m["stability_question"].value is None
    assert m["completion"].passed is False
