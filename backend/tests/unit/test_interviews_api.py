"""面试创建与计划 API：SQLite + 假 LLM。"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlite_helpers import create_tables
from test_api import error_of, make_settings, open_client

from interview.api.app import create_app
from interview.api.deps import routes_without_user
from interview.api.resources import Resources
from interview.config import Settings
from interview.db.models import (
    Interview,
    InterviewStatus,
    Plan,
    Resume,
    UsageRole,
    User,
)
from interview.db.session import make_sessionmaker
from interview.domain.plan import InterviewRepository, PlanRepository
from interview.domain.resume import ResumeRepository
from interview.observability import usage
from interview.providers.base import ChatMessage
from interview.providers.fake import FakeLLM
from interview.providers.registry import ProviderFactories, ProviderRegistry

USER = uuid.UUID("00000000-0000-4000-8000-000000000015")
RESUME_ID = uuid.UUID("00000000-0000-4000-8000-000000000016")
JD_TEXT = "高级后端工程师：熟悉 Go、PostgreSQL、Redis，有分布式系统设计和团队协作经验。"

JD_PAYLOAD: dict[str, Any] = {
    "title": "后端工程师",
    "level": "高级",
    "competencies": [
        {"name": "后端开发", "description": "服务开发", "weight": 4, "probes": ["性能", "稳定性"]},
        {"name": "系统设计", "description": "架构设计", "weight": 3, "probes": ["拆分", "扩展"]},
        {"name": "数据库", "description": "数据建模", "weight": 2, "probes": ["索引", "事务"]},
        {"name": "协作", "description": "团队合作", "weight": 1, "probes": ["沟通", "推动"]},
    ],
    "tech_terms": ["Go", "PostgreSQL"],
}

RESUME_DATA: dict[str, Any] = {
    "name": "Alex Chen",
    "work": [
        {
            "company": "Northwind Labs",
            "title": "Backend Engineer",
            "highlights": ["Built a payment service"],
        }
    ],
    "projects": [
        {
            "name": "Ledger",
            "role": "owner",
            "tech_stack": ["Go", "PostgreSQL"],
            "highlights": ["Cut checkout latency"],
        }
    ],
    "skills": ["Go", "PostgreSQL"],
    "education": [],
}


def _question(
    stage: str,
    competency: str,
    project: str,
    text: str,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "competency": competency,
        "project": project,
        "text": text,
        "key_points": ["关键要点"],
        "follow_ups": ["追问一", "追问二"],
    }


# 30 分钟：深挖容量 3、主阶段容量 4；这里出 5 题，数据库有 2 题，供 PATCH 删一题后仍被覆盖。
PLAN_PAYLOAD: dict[str, Any] = {
    "questions": [
        _question("resume_deep_dive", "后端开发", "Ledger", "讲讲 Ledger 的架构"),
        _question("resume_deep_dive", "系统设计", "Northwind Labs", "讲讲 Northwind 的拆分"),
        _question("technical", "数据库", "", "数据库题一"),
        _question("technical", "数据库", "", "数据库题二"),
        _question("technical", "协作", "", "协作题"),
    ]
}

CREATE_BODY: dict[str, Any] = {
    "resume_id": str(RESUME_ID),
    "jd_text": JD_TEXT,
    "type": "technical",
    "style": "gentle",
    "language": "zh",
    "duration_min": 30,
}


class FakePlanScript:
    """按系统提示区分 JD 解析和出题；fail_plan 时出题返回非法 JSON。"""

    def __init__(self) -> None:
        self.fail_plan = False

    def reply(self, messages: Sequence[ChatMessage]) -> str:
        system = messages[0]["content"] if messages else ""
        if "职位描述分析器" in system:
            return json.dumps(JD_PAYLOAD, ensure_ascii=False)
        if "面试计划" in system:
            if self.fail_plan:
                return "not json"
            return json.dumps(PLAN_PAYLOAD, ensure_ascii=False)
        raise AssertionError("unexpected prompt")


@dataclass
class Api:
    client: httpx.AsyncClient
    script: FakePlanScript
    llm: FakeLLM
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await create_tables(conn, User, Resume, Interview, Plan)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as session:
        session.add(User(id=USER, email="local@example.com"))
        ResumeRepository(session, USER).add(
            Resume(
                id=RESUME_ID,
                user_id=USER,
                file_key=f"{USER}/{RESUME_ID}.pdf",
                parsed_json=RESUME_DATA,
            )
        )
        await session.commit()

    script = FakePlanScript()
    llm = FakeLLM(reply=script.reply, chunk_size=10000)
    settings = make_settings(user=USER)
    settings.providers.llm_eval.provider = "fake"
    settings.providers.llm_eval.model = "fake-llm"
    registry = ProviderRegistry(
        settings.providers,
        factories=ProviderFactories(llm={"fake": lambda _role: llm}),
    )

    async def ignore_usage(**_kwargs: Any) -> None:
        return None

    # 用量默认不写库；需要断言的角色测试会再替换成收集器。
    monkeypatch.setattr(usage, "record_usage", ignore_usage)
    resources = Resources(settings=settings, sessionmaker=sessionmaker, providers=registry)

    def factory(current: Settings) -> Resources:
        assert current is settings
        return resources

    app = create_app(settings, resource_factory=factory, init_observability=False)
    assert routes_without_user(app) == []
    async with open_client(app) as client:
        yield Api(
            client=client,
            script=script,
            llm=llm,
            sessionmaker=sessionmaker,
            settings=settings,
        )
    await engine.dispose()


def editable(question: dict[str, Any]) -> dict[str, Any]:
    """PATCH 只能提交这四个字段。"""
    return {key: question[key] for key in ("id", "text", "key_points", "follow_ups")}


async def test_create_then_get_plan(api: Api) -> None:
    created = await api.client.post("/api/interviews", json=CREATE_BODY)
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "planned"
    assert body["resume_id"] == str(RESUME_ID)
    assert (body["type"], body["style"], body["language"]) == ("technical", "gentle", "zh")
    assert body["duration_min"] == 30

    plan = body["plan"]
    assert plan["prompt_version"] == "plan_questions@1"
    assert plan["tech_terms"] == ["Go", "PostgreSQL"]
    assert [question["id"] for question in plan["questions"]] == [
        "q1",
        "q2",
        "q3",
        "q4",
        "q5",
    ]
    assert [item["covered"] for item in plan["competencies"]] == [True, True, True, True]
    assert plan["stages"] == [
        {"stage": "opening", "seconds": 90, "capacity": 0},
        {"stage": "self_intro", "seconds": 180, "capacity": 0},
        {"stage": "resume_deep_dive", "seconds": 540, "capacity": 3},
        {"stage": "technical", "seconds": 720, "capacity": 4},
        {"stage": "candidate_questions", "seconds": 180, "capacity": 0},
        {"stage": "closing", "seconds": 90, "capacity": 0},
    ]

    fetched = await api.client.get(f"/api/interviews/{body['id']}/plan")
    assert fetched.status_code == 200
    assert fetched.json() == plan


async def test_other_users_resume_is_not_found(api: Api) -> None:
    api.settings.app.default_user_id = uuid.uuid4()
    response = await api.client.post("/api/interviews", json=CREATE_BODY)
    assert response.status_code == 404
    assert error_of(response)["message"] == "简历不存在"
    assert api.llm.calls == []


async def test_duration_must_be_15_20_or_30(api: Api) -> None:
    response = await api.client.post("/api/interviews", json={**CREATE_BODY, "duration_min": 10})
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_error"
    assert api.llm.calls == []


@pytest.mark.parametrize("jd_text", ["", "  ", "字" * 10001])
async def test_jd_text_reuses_jd_validation(api: Api, jd_text: str) -> None:
    response = await api.client.post("/api/interviews", json={**CREATE_BODY, "jd_text": jd_text})
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_error"
    assert api.llm.calls == []


async def test_plan_failure_returns_502_and_writes_nothing(api: Api) -> None:
    api.script.fail_plan = True
    response = await api.client.post("/api/interviews", json=CREATE_BODY)
    assert response.status_code == 502
    assert error_of(response)["code"] == "plan_failed"
    assert "not json" not in response.text
    async with api.sessionmaker() as session:
        assert list(await InterviewRepository(session, USER).list()) == []
        assert list(await PlanRepository(session, USER).list()) == []


async def test_patch_deletes_a_question(api: Api) -> None:
    created = await api.client.post("/api/interviews", json=CREATE_BODY)
    plan = created.json()["plan"]
    edits = [editable(question) for question in plan["questions"] if question["id"] != "q4"]
    patched = await api.client.patch(
        f"/api/interviews/{created.json()['id']}/plan", json={"questions": edits}
    )
    assert patched.status_code == 200
    questions = patched.json()["questions"]
    assert [question["id"] for question in questions] == ["q1", "q2", "q3", "q5"]
    assert questions[2]["text"] == "数据库题一"
    assert questions[2]["competency"] == "数据库"


async def test_patch_rejects_deleting_all_questions_of_a_competency(api: Api) -> None:
    created = await api.client.post("/api/interviews", json=CREATE_BODY)
    plan = created.json()["plan"]
    edits = [
        editable(question) for question in plan["questions"] if question["competency"] != "数据库"
    ]
    response = await api.client.patch(
        f"/api/interviews/{created.json()['id']}/plan", json={"questions": edits}
    )
    assert response.status_code == 422
    assert error_of(response)["code"] == "plan_invalid"
    assert "数据库" in error_of(response)["message"]


async def test_patch_rejects_unknown_and_duplicate_ids(api: Api) -> None:
    created = await api.client.post("/api/interviews", json=CREATE_BODY)
    plan = created.json()["plan"]
    first = editable(plan["questions"][0])

    unknown = await api.client.patch(
        f"/api/interviews/{created.json()['id']}/plan",
        json={"questions": [{**first, "id": "q99"}]},
    )
    assert unknown.status_code == 422
    assert error_of(unknown)["code"] == "plan_invalid"

    duplicate = await api.client.patch(
        f"/api/interviews/{created.json()['id']}/plan",
        json={"questions": [first, first]},
    )
    assert duplicate.status_code == 422
    assert error_of(duplicate)["code"] == "plan_invalid"


async def test_patch_ignores_stage_competency_and_project(api: Api) -> None:
    created = await api.client.post("/api/interviews", json=CREATE_BODY)
    plan = created.json()["plan"]
    edits = [dict(question) for question in plan["questions"]]
    edits[0].update(
        {
            "stage": "technical",
            "competency": "协作",
            "project": "Fake Project",
            "text": "改后的问题",
        }
    )
    patched = await api.client.patch(
        f"/api/interviews/{created.json()['id']}/plan", json={"questions": edits}
    )
    assert patched.status_code == 200
    first = patched.json()["questions"][0]
    assert (first["stage"], first["competency"], first["project"]) == (
        "resume_deep_dive",
        "后端开发",
        "Ledger",
    )
    assert first["text"] == "改后的问题"


async def test_patch_rejects_non_planned_interview(api: Api) -> None:
    created = await api.client.post("/api/interviews", json=CREATE_BODY)
    interview_id = uuid.UUID(created.json()["id"])
    async with api.sessionmaker() as session:
        interview = await InterviewRepository(session, USER).get(interview_id)
        assert interview is not None
        interview.status = InterviewStatus.IN_PROGRESS
        await session.commit()

    edits = [editable(question) for question in created.json()["plan"]["questions"]]
    response = await api.client.patch(
        f"/api/interviews/{interview_id}/plan", json={"questions": edits}
    )
    assert response.status_code == 409
    assert error_of(response)["code"] == "conflict"


async def test_usage_roles_and_interview_id(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[dict[str, Any]] = []

    async def record(**kwargs: Any) -> None:
        records.append(kwargs)

    monkeypatch.setattr(usage, "record_usage", record)
    created = await api.client.post("/api/interviews", json=CREATE_BODY)
    assert created.status_code == 201
    interview_id = uuid.UUID(created.json()["id"])

    assert len(records) == 2
    assert {item["role"] for item in records} == {UsageRole.JD_PARSE, UsageRole.PLAN}
    assert {item["interview_id"] for item in records} == {interview_id}
