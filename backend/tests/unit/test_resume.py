"""简历上传与解析：虚构 PDF、假 LLM、类型 / 大小 / 无文字 / 越权。"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, cast

import httpx
import pytest
from pypdf import PdfReader, PdfWriter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlite_helpers import create_tables
from test_api import error_of, make_settings, open_client

from interview.api.app import create_app
from interview.api.deps import get_resume_llm
from interview.api.resources import Resources
from interview.db.models import Resume, User
from interview.db.session import make_sessionmaker
from interview.domain.resume import (
    MAX_RESUME_BYTES,
    ResumeError,
    ResumeHasNoText,
    ResumeNotPdf,
    ResumeParsed,
    ResumeRepository,
    ResumeTooLarge,
    check_resume_file,
    extract_text,
    parse_resume,
)
from interview.logging import configure_logging
from interview.prompts.loader import get_prompt_library
from interview.providers.base import LLMProvider, ProviderTimeout
from interview.providers.fake import FakeLLM
from interview.providers.registry import ProviderRegistry
from interview.storage.base import StorageError
from interview.storage.memory import MemoryObjectStore

USER = uuid.UUID("00000000-0000-4000-8000-000000000013")
TEXT_LIMIT = 50_000
UPLOAD_BODY_LIMIT = MAX_RESUME_BYTES + 64 * 1024

PARSED = {
    "name": "Alex Chen",
    "work": [
        {
            "company": "Northwind Labs",
            "title": "Backend Engineer",
            "start": "2022",
            "end": "2026",
            "highlights": ["Built a payment service in Go"],
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
    "education": [
        {
            "school": "Example University",
            "degree": "BSc",
            "field": "Computer Science",
            "start": "2018",
            "end": "2022",
        }
    ],
}


def text_pdf(lines: list[str]) -> bytes:
    """生成只有几行西文的 PDF，供 pypdf 抽出文字。"""
    commands = ["BT", "/F1 12 Tf", "72 740 Td", "16 TL"]
    for index, line in enumerate(lines):
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(f"({safe}) Tj" if index == 0 else f"T* ({safe}) Tj")
    commands.append("ET")
    stream = "\n".join(commands).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(offsets)}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def encrypted_pdf(user_password: str, owner_password: str) -> bytes:
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(RESUME_PDF)))
    writer.encrypt(user_password=user_password, owner_password=owner_password)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


RESUME_PDF = text_pdf(
    [
        "Alex Chen",
        "Northwind Labs, Backend Engineer",
        "Project Ledger, owner, Go and PostgreSQL",
    ]
)


def test_check_resume_file_rejects_type_and_size() -> None:
    with pytest.raises(ResumeNotPdf):
        check_resume_file(b"not a pdf")
    check_resume_file(b"%PDF-" + b"a" * (MAX_RESUME_BYTES - 5))
    with pytest.raises(ResumeTooLarge):
        check_resume_file(b"%PDF-" + b"a" * (MAX_RESUME_BYTES - 4))


def test_extract_text_from_generated_pdf_and_blank_page() -> None:
    assert "Northwind Labs" in extract_text(RESUME_PDF)
    with pytest.raises(ResumeHasNoText, match="扫描"):
        extract_text(blank_pdf())


async def test_parse_resume_uses_v1_prompt_and_drops_contact_fields() -> None:
    payload = {**PARSED, "phone": "13800000000", "email": "a@b.c", "address": "Somewhere"}
    llm = FakeLLM(reply=json.dumps(payload))
    result = await parse_resume(llm, "Northwind Labs\n13800000000\na@b.c")
    assert result.prompt_id == "resume_parse"
    assert result.prompt_version == 1
    assert result.value == ResumeParsed.model_validate(PARSED)
    assert "phone" not in result.value.model_dump()
    system, user = llm.calls[0]["messages"]
    assert "手机号" in system["content"]
    assert "邮箱" in system["content"]
    assert "地址" in system["content"]
    assert "Northwind Labs" in user["content"]
    assert '<user_data type="resume">' in user["content"]
    rendered = get_prompt_library().render("resume_parse", resume_text="x")
    assert (rendered.id, rendered.version) == ("resume_parse", 1)


class SessionProbe:
    """数当前打开的数据库会话。简历解析进行时这个数应该是 0。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.depth = 0

    def __call__(self) -> Any:
        probe = self
        context = self._inner()

        class _Tracked:
            async def __aenter__(self) -> Any:
                probe.depth += 1
                return await context.__aenter__()

            async def __aexit__(self, *args: object) -> Any:
                try:
                    return await context.__aexit__(*args)
                finally:
                    probe.depth -= 1

        return _Tracked()


class TrackingLLM(FakeLLM):
    def __init__(self, probe: SessionProbe) -> None:
        super().__init__(reply=json.dumps(PARSED))
        self.probe = probe
        self.depth_during_complete: int | None = None

    async def complete(self, messages: Any, **kwargs: Any) -> Any:
        self.depth_during_complete = self.probe.depth
        return await super().complete(messages, **kwargs)


@pytest.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await create_tables(conn, User, Resume)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as session:
        session.add(User(id=USER, email="local@example.com"))
        await session.commit()

    settings = make_settings(user=USER)
    store = MemoryObjectStore()
    probe = SessionProbe(sessionmaker)
    llm = TrackingLLM(probe)

    def factory(current: object) -> Resources:
        assert current is settings
        return Resources(
            settings=settings,
            sessionmaker=cast(async_sessionmaker[AsyncSession], probe),
            providers=ProviderRegistry(settings.providers),
            storage=store,
        )

    def override_llm() -> LLMProvider:
        return llm

    app = create_app(settings, resource_factory=factory, init_observability=False)
    app.dependency_overrides[get_resume_llm] = override_llm
    holder = SettingsHolder(settings, store, llm, app)
    holder.sessionmaker = sessionmaker
    async with open_client(app) as client:
        yield client, store, holder
    await engine.dispose()


class SettingsHolder:
    def __init__(
        self, settings: object, store: MemoryObjectStore, llm: TrackingLLM, app: Any
    ) -> None:
        self.settings = settings
        self.store = store
        self.llm = llm
        self.app = app
        self.sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _upload(data: bytes, name: str = "resume.pdf") -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, data, "application/pdf")}


async def test_upload_get_and_patch(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, store, holder = api
    created = await client.post("/api/resumes", files=_upload(RESUME_PDF, "简历.pdf"))
    assert created.status_code == 201
    body = created.json()
    assert body["parsed"] == PARSED
    resume_id = body["id"]
    assert "phone" not in json.dumps(body["parsed"])

    stored = [item for item in store.objects if item[0] == "resumes"]
    assert len(stored) == 1
    bucket, key = stored[0]
    assert key == f"{USER}/{resume_id}.pdf"
    assert await store.get(bucket, key) == RESUME_PDF
    assert "Northwind Labs" in holder.llm.calls[0]["messages"][1]["content"]

    fetched = await client.get(f"/api/resumes/{resume_id}")
    assert fetched.status_code == 200
    assert fetched.json()["parsed"]["skills"] == ["Go", "PostgreSQL"]

    revised = json.loads(json.dumps(PARSED))
    revised["skills"] = ["Go", "Redis"]
    patched = await client.patch(f"/api/resumes/{resume_id}", json=revised)
    assert patched.status_code == 200
    assert patched.json()["parsed"]["skills"] == ["Go", "Redis"]
    again = await client.get(f"/api/resumes/{resume_id}")
    assert again.json()["parsed"]["skills"] == ["Go", "Redis"]


async def test_rejects_non_pdf_even_if_content_type_says_pdf(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, store, _holder = api
    response = await client.post("/api/resumes", files=_upload(b"hello, this is text"))
    assert response.status_code == 415
    assert error_of(response)["code"] == "unsupported_media_type"
    assert store.objects == {}


async def test_rejects_file_over_10mb(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, store, _holder = api
    blob = b"%PDF-" + b"0" * (MAX_RESUME_BYTES - 4)
    response = await client.post("/api/resumes", files=_upload(blob))
    assert response.status_code == 413
    assert error_of(response)["code"] == "file_too_large"
    assert store.objects == {}


async def test_rejects_scanned_pdf_without_text(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, store, holder = api
    response = await client.post("/api/resumes", files=_upload(blank_pdf()))
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "resume_no_text"
    assert "扫描" in error["message"]
    assert store.objects == {}
    assert holder.llm.calls == []


async def test_other_user_cannot_read_or_update(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, _store, holder = api
    created = await client.post("/api/resumes", files=_upload(RESUME_PDF))
    assert created.status_code == 201
    resume_id = created.json()["id"]

    missing = await client.get(f"/api/resumes/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert error_of(missing)["message"] == "简历不存在"

    holder.settings.app.default_user_id = uuid.uuid4()  # type: ignore[attr-defined]
    denied = await client.get(f"/api/resumes/{resume_id}")
    assert denied.status_code == 404
    assert error_of(denied)["message"] == "简历不存在"
    patched = await client.patch(f"/api/resumes/{resume_id}", json=PARSED)
    assert patched.status_code == 404

    holder.settings.app.default_user_id = USER  # type: ignore[attr-defined]
    owned = await client.get(f"/api/resumes/{resume_id}")
    assert owned.status_code == 200
    assert owned.json()["parsed"]["name"] == "Alex Chen"


def test_extract_text_reads_owner_locked_pdf_and_rejects_user_password() -> None:
    assert "Alex Chen" in extract_text(encrypted_pdf("", "owner-secret"))
    with pytest.raises(ResumeError, match="加密") as caught:
        extract_text(encrypted_pdf("user-secret", "owner-secret"))
    assert caught.value.code == "resume_unreadable"


def test_extract_text_rejects_resume_longer_than_50k_characters() -> None:
    huge = text_pdf(["A" * (TEXT_LIMIT + 1)])
    with pytest.raises(ResumeError, match="太长") as caught:
        extract_text(huge)
    assert caught.value.code == "resume_too_long"
    assert "A" * 20 in extract_text(text_pdf(["A" * TEXT_LIMIT]))


async def test_parse_resume_strips_phone_and_email_from_free_text() -> None:
    payload = json.loads(json.dumps(PARSED))
    payload["name"] = "Alex Chen alex@northwind.example"
    payload["work"][0]["highlights"] = ["138-0000-0000", "Built a payment service"]
    payload["skills"] = ["Go", "alex@northwind.example", "PostgreSQL"]
    result = await parse_resume(FakeLLM(reply=json.dumps(payload)), "resume")
    dumped = json.dumps(result.value.model_dump(), ensure_ascii=False)
    assert "138-0000-0000" not in dumped
    assert "13800000000" not in dumped
    assert "alex@northwind.example" not in dumped
    assert result.value.name == "Alex Chen"
    assert result.value.work[0].highlights == ["Built a payment service"]
    assert result.value.skills == ["Go", "PostgreSQL"]


async def test_patch_rejects_partial_body_and_keeps_stored_resume(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, _store, _holder = api
    created = await client.post("/api/resumes", files=_upload(RESUME_PDF))
    resume_id = created.json()["id"]
    rejected = await client.patch(f"/api/resumes/{resume_id}", json={"skills": ["Go"]})
    assert rejected.status_code == 422
    assert error_of(rejected)["code"] == "validation_error"
    kept = await client.get(f"/api/resumes/{resume_id}")
    assert kept.json()["parsed"]["work"][0]["company"] == "Northwind Labs"
    assert kept.json()["parsed"]["skills"] == ["Go", "PostgreSQL"]


async def test_patch_accepts_explicit_empty_sections(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, _store, _holder = api
    created = await client.post("/api/resumes", files=_upload(RESUME_PDF))
    resume_id = created.json()["id"]
    revised = json.loads(json.dumps(PARSED))
    revised["work"] = []
    revised["projects"] = []
    revised["skills"] = []
    revised["education"] = []
    patched = await client.patch(f"/api/resumes/{resume_id}", json=revised)
    assert patched.status_code == 200
    assert patched.json()["parsed"]["work"] == []
    assert patched.json()["parsed"]["name"] == "Alex Chen"


async def test_storage_failure_does_not_call_the_model(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, store, holder = api

    async def fail_put(*_args: object, **_kwargs: object) -> None:
        raise StorageError("down")

    store.put = fail_put  # type: ignore[method-assign]
    response = await client.post("/api/resumes", files=_upload(RESUME_PDF))
    assert response.status_code == 503
    assert error_of(response)["code"] == "service_unavailable"
    assert holder.llm.calls == []
    assert store.objects == {}


async def test_parse_failure_does_not_keep_the_file(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, store, holder = api
    holder.llm._reply = "not json"
    response = await client.post("/api/resumes", files=_upload(RESUME_PDF))
    assert response.status_code == 502
    assert error_of(response)["code"] == "parse_failed"
    assert store.objects == {}


async def test_model_unavailable_returns_503_and_drops_the_file(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, store, holder = api
    holder.llm._errors.append(ProviderTimeout("timeout", provider="fake", model="m"))
    response = await client.post("/api/resumes", files=_upload(RESUME_PDF))
    assert response.status_code == 503
    assert error_of(response)["code"] == "model_unavailable"
    assert store.objects == {}


async def test_model_call_does_not_hold_a_database_session(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, _store, holder = api
    created = await client.post("/api/resumes", files=_upload(RESUME_PDF))
    assert created.status_code == 201
    assert holder.llm.depth_during_complete == 0


def _upload_scope(content_length: int | None) -> dict[str, Any]:
    headers = [(b"content-type", b"multipart/form-data; boundary=bound")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/resumes",
        "raw_path": b"/api/resumes",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "root_path": "",
    }


async def test_declared_body_over_the_limit_is_rejected_before_reading(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    _client, store, holder = api
    reads = 0

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"should-not-be-read", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await holder.app(_upload_scope(UPLOAD_BODY_LIMIT + 1), receive, send)
    assert reads == 0
    assert sent[0]["status"] == 413
    body = json.loads(sent[1]["body"])
    assert body["error"]["code"] == "file_too_large"
    assert store.objects == {}
    assert holder.llm.calls == []


async def test_chunked_body_over_the_limit_stops_early(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    _client, _store, holder = api
    chunk = b"a" * (1024 * 1024)
    total_chunks = UPLOAD_BODY_LIMIT // len(chunk) + 2
    index = 0

    async def receive() -> dict[str, Any]:
        nonlocal index
        if index >= total_chunks:
            return {"type": "http.request", "body": b"", "more_body": False}
        index += 1
        return {"type": "http.request", "body": chunk, "more_body": index < total_chunks}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await holder.app(_upload_scope(None), receive, send)
    assert sent[0]["status"] == 413
    assert index < total_chunks


@pytest.mark.parametrize("role", [None, ""])
async def test_upload_keeps_projects_without_role(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
    role: str | None,
) -> None:
    client, _store, holder = api
    payload = json.loads(json.dumps(PARSED))
    if role is None:
        del payload["projects"][0]["role"]
    else:
        payload["projects"][0]["role"] = role
    holder.llm._reply = json.dumps(payload)
    response = await client.post("/api/resumes", files=_upload(RESUME_PDF))
    assert response.status_code == 201
    assert len(holder.llm.calls) == 1
    project = response.json()["parsed"]["projects"][0]
    assert project["name"] == "Ledger" and project["role"] == ""


async def test_patch_redacts_contacts_before_persisting(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, _store, _holder = api
    created = await client.post("/api/resumes", files=_upload(RESUME_PDF))
    path = f"/api/resumes/{created.json()['id']}"
    payload = json.loads(json.dumps(PARSED))
    payload["name"] += " alex@northwind.example"
    payload["skills"] += ["13800000000"]
    payload["projects"][0]["highlights"] += ["联系 alex@northwind.example"]
    updated = await client.patch(path, json=payload)
    fetched = await client.get(path)
    assert updated.status_code == fetched.status_code == 200
    assert fetched.json()["parsed"]["skills"] == ["Go", "PostgreSQL"]
    for response in (updated, fetched):
        assert "alex@northwind.example" not in response.text
        assert "13800000000" not in response.text


async def test_database_failure_does_not_log_resume_fields(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, store, holder = api
    engine = holder.llm.probe._inner.kw["bind"]
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA foreign_keys=ON"))
    holder.settings.app.default_user_id = uuid.uuid4()  # type: ignore[attr-defined]
    holder.llm._reply = json.dumps({"name": "PRIVATE_NAME", "skills": ["PRIVATE_SKILL"]})
    output = io.StringIO()
    configure_logging(stream=output)
    response = await client.post("/api/resumes", files=_upload(RESUME_PDF))
    assert response.status_code == 500
    assert store.objects == {}
    logged = output.getvalue()
    assert "http_unhandled_error" in logged and "IntegrityError" in logged
    assert "PRIVATE_NAME" not in logged and "PRIVATE_SKILL" not in logged


LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)
OTHER_PDF = text_pdf(["Sam Lee", "Contoso, Data Engineer", "Skills: Python, Spark"])


async def test_same_file_is_reused_without_parsing_again(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, store, holder = api
    first = await client.post("/api/resumes", files=_upload(RESUME_PDF, "dir/简历.pdf"))
    assert first.json()["reused"] is False
    assert first.json()["file_name"] == "简历.pdf"
    again = await client.post("/api/resumes", files=_upload(RESUME_PDF, "copy.pdf"))
    assert again.status_code == 201
    assert again.json()["reused"] is True
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["file_name"] == "简历.pdf"
    assert len(holder.llm.calls) == 1  # 第二次没有调用模型
    assert len(store.objects) == 1

    other = await client.post("/api/resumes", files=_upload(OTHER_PDF))
    assert other.json()["reused"] is False
    assert len(holder.llm.calls) == 2


async def test_another_users_identical_file_is_parsed_for_them(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, _store, holder = api
    await client.post("/api/resumes", files=_upload(RESUME_PDF))
    holder.settings.app.default_user_id = uuid.uuid4()  # type: ignore[attr-defined]
    assert (await client.get("/api/resumes")).json() == []
    holder.settings.app.default_user_id = USER  # type: ignore[attr-defined]
    assert len((await client.get("/api/resumes")).json()) == 1


async def test_list_shows_recent_first_with_one_entry_per_file(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    client, _store, holder = api
    assert (await client.get("/api/resumes")).json() == []
    first = (await client.post("/api/resumes", files=_upload(RESUME_PDF, "a.pdf"))).json()
    second = (await client.post("/api/resumes", files=_upload(OTHER_PDF, "b.pdf"))).json()
    # 旧数据：同一文件存了两份（指纹相同），以及加指纹之前的一份（指纹为空）
    assert holder.sessionmaker is not None
    async with holder.sessionmaker() as session:
        repo = ResumeRepository(session, USER)
        original = await repo.get(uuid.UUID(first["id"]))
        assert original is not None
        for sha in (original.file_sha256, None):
            repo.add(
                Resume(
                    user_id=USER,
                    file_key="old.pdf",
                    file_sha256=sha,
                    parsed_json=PARSED,
                    created_at=LONG_AGO,
                    updated_at=LONG_AGO,
                )
            )
        repo.add(Resume(user_id=USER, file_key="unparsed.pdf", parsed_json=None))
        await session.commit()
    await client.patch(f"/api/resumes/{first['id']}", json=PARSED)  # 最近修改的排最前

    listed = (await client.get("/api/resumes")).json()
    assert [item["id"] for item in listed[:2]] == [first["id"], second["id"]]
    assert len(listed) == 3  # 同一文件只留一份，没有指纹的旧简历单独出现，未解析的不出现
    assert listed[0] | {"created_at": None, "updated_at": None} == {
        "id": first["id"],
        "file_name": "a.pdf",
        "name": "Alex Chen",
        "headline": "Backend Engineer · Northwind Labs",
        "created_at": None,
        "updated_at": None,
    }
    assert listed[2]["file_name"] is None
    assert len((await client.get("/api/resumes?limit=1")).json()) == 1
    assert (await client.get("/api/resumes?limit=0")).status_code == 422


async def test_backfill_fills_hashes_from_stored_originals() -> None:
    from backfill_resume_hashes import backfill

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await create_tables(conn, User, Resume)
    sessionmaker = make_sessionmaker(engine)
    store = MemoryObjectStore()
    await store.put("resumes", "kept.pdf", RESUME_PDF, content_type="application/pdf")
    async with sessionmaker() as session:
        session.add(User(id=USER, email="local@example.com"))
        await session.commit()
    async with sessionmaker() as session:
        repo = ResumeRepository(session, USER)
        repo.add(Resume(user_id=USER, file_key="kept.pdf", parsed_json=PARSED))
        repo.add(Resume(user_id=USER, file_key="gone.pdf", parsed_json=PARSED))
        await session.commit()

    assert await backfill(sessionmaker, store, bucket="resumes", user_id=USER) == (1, 1)
    async with sessionmaker() as session:
        rows = await ResumeRepository(session, USER).list()
        hashes = {row.file_key: row.file_sha256 for row in rows}
    assert hashes == {"kept.pdf": hashlib.sha256(RESUME_PDF).hexdigest(), "gone.pdf": None}
    assert await backfill(sessionmaker, store, bucket="resumes", user_id=USER) == (0, 1)
    await engine.dispose()


async def test_reupload_fills_in_a_missing_file_name(
    api: tuple[httpx.AsyncClient, MemoryObjectStore, SettingsHolder],
) -> None:
    # 加文件名之前上传的旧简历：重传同一文件时补上文件名，仍然不重新解析
    client, _store, holder = api
    first = (await client.post("/api/resumes", files=_upload(RESUME_PDF, "old.pdf"))).json()
    assert holder.sessionmaker is not None
    async with holder.sessionmaker() as session:
        saved = await ResumeRepository(session, USER).get(uuid.UUID(first["id"]))
        assert saved is not None
        saved.file_name = None
        await session.commit()

    again = (await client.post("/api/resumes", files=_upload(RESUME_PDF, "我的简历.pdf"))).json()
    assert (again["reused"], again["file_name"]) == (True, "我的简历.pdf")
    renamed = (await client.post("/api/resumes", files=_upload(RESUME_PDF, "other.pdf"))).json()
    assert renamed["file_name"] == "我的简历.pdf"  # 已有文件名就不改
    assert len(holder.llm.calls) == 1
    assert (await client.get("/api/resumes")).json()[0]["file_name"] == "我的简历.pdf"
