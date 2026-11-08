"""后端接口变更时，要求同步导出前端使用的 OpenAPI 快照。"""

from pathlib import Path

import pytest


def test_openapi_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    # 导出不能读取本机环境；非法值也不应影响生成结果。
    monkeypatch.setenv("APP_ENV", "invalid-for-export")
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "invalid-for-export")
    monkeypatch.setenv("LLM_FRONT_EXTRA_BODY", "invalid-for-export")
    monkeypatch.setenv("REPORT_CONCURRENCY", "invalid-for-export")

    from export_openapi import export_schema

    snapshot = Path(__file__).resolve().parents[3] / "web" / "openapi.json"
    assert snapshot.exists(), "OpenAPI 快照不存在，请运行 make gen-types"
    assert export_schema() == snapshot.read_text(encoding="utf-8"), (
        "OpenAPI 快照与后端接口不一致，请运行 make gen-types"
    )
