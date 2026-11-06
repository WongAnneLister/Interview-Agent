"""评测数据：人设和 fixture 能全部加载，挑选参数校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.data import load_fixtures, load_personas, select

PERSONAS = {"strong", "average", "weak", "nervous", "verbose", "adversarial"}
FIXTURES = {"backend", "ai_app", "frontend", "data", "voice_agent"}


def test_all_personas_and_fixtures_load() -> None:
    personas = load_personas()
    fixtures = load_fixtures()
    assert set(personas) == PERSONAS
    assert set(fixtures) == FIXTURES
    assert personas["adversarial"].adversarial
    assert personas["nervous"].think_ratio > 0
    assert personas["verbose"].style is not None


def test_id_must_match_file_name(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "evals" / "personas" / "weak.yaml"
    (tmp_path / "other.yaml").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="必须与文件名一致"):
        load_personas(tmp_path)


def test_select_defaults_to_all_and_rejects_unknown() -> None:
    available: dict[str, object] = {"a": 1, "b": 2}
    assert select(available, None, "人设") == ["a", "b"]
    assert select(available, ["b"], "人设") == ["b"]
    with pytest.raises(ValueError, match="未知的人设：c"):
        select(available, ["c"], "人设")
