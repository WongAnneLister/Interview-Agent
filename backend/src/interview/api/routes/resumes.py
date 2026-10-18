"""简历上传、查看和修正。

POST 收 multipart 的 file 字段：校验 PDF 与大小，抽取文字，原件放进 resumes 桶后再解析。
解析或入库失败时删掉刚写入的原件。GET / PATCH 只返回当前用户自己的简历；
别人的 id 与不存在一样，都是 404。PATCH 必须提交整份解析结果。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel

from interview.api.deps import (
    CurrentUserId,
    DbSession,
    get_resources,
    get_resume_llm,
    get_settings,
    get_storage,
)
from interview.api.errors import AppError, NotFoundError, ServiceUnavailableError
from interview.api.resources import Resources
from interview.config import Settings
from interview.db.models import Resume
from interview.domain.resume import (
    MAX_RESUME_BYTES,
    ResumeError,
    ResumeParsed,
    ResumeReplacement,
    ResumeRepository,
    ResumeTooLarge,
    check_resume_file,
    extract_text,
    parse_resume,
    redact_contacts,
)
from interview.prompts.structured import StructuredOutputError
from interview.providers.base import LLMProvider
from interview.storage.base import ObjectStore, StorageError

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/resumes", tags=["resumes"])


class ResumeOut(BaseModel):
    id: uuid.UUID
    parsed: ResumeParsed
    created_at: datetime
    updated_at: datetime


def _http(exc: ResumeError) -> AppError:
    return AppError(exc.message, code=exc.code, status_code=exc.status_code)


def _view(resume: Resume) -> ResumeOut:
    raw = resume.parsed_json
    if not isinstance(raw, dict):
        raise NotFoundError("简历不存在")
    return ResumeOut(
        id=resume.id,
        parsed=ResumeParsed.model_validate(raw),
        created_at=resume.created_at,
        updated_at=resume.updated_at,
    )


async def _owned(session: DbSession, user_id: uuid.UUID, resume_id: uuid.UUID) -> Resume:
    resume = await ResumeRepository(session, user_id).get(resume_id)
    if resume is None or resume.user_id != user_id:
        raise NotFoundError("简历不存在")
    return resume


async def _discard(storage: ObjectStore, bucket: str, key: str, resume_id: uuid.UUID) -> None:
    try:
        await storage.delete(bucket, key)
    except Exception as exc:
        log.warning(
            "resume_store_rollback_failed",
            resume_id=str(resume_id),
            error=type(exc).__name__,
        )


async def _read_limited(upload: UploadFile) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_RESUME_BYTES:
            raise ResumeTooLarge("简历不能超过 10 MB")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("", status_code=201)
async def create_resume(
    user_id: CurrentUserId,
    settings: Annotated[Settings, Depends(get_settings)],
    storage: Annotated[ObjectStore, Depends(get_storage)],
    llm: Annotated[LLMProvider, Depends(get_resume_llm)],
    resources: Annotated[Resources, Depends(get_resources)],
    file: Annotated[UploadFile, File()],
) -> ResumeOut:
    try:
        try:
            data = await _read_limited(file)
        finally:
            await file.close()
        check_resume_file(data)
        text = extract_text(data)
    except ResumeError as exc:
        raise _http(exc) from exc

    # 先存原件再调用模型：存储失败不必付解析费用。数据库会话只在写入时打开。
    resume_id = uuid.uuid4()
    bucket = settings.s3.bucket_resumes
    key = f"{user_id}/{resume_id}.pdf"
    stored = False
    try:
        try:
            await storage.put(bucket, key, data, content_type="application/pdf")
        except StorageError as exc:
            log.warning("resume_store_failed", error=type(exc).__name__)
            raise ServiceUnavailableError("简历原件保存失败，请稍后重试") from exc
        stored = True
        try:
            parsed = await parse_resume(llm, text)
        except StructuredOutputError as exc:
            log.warning("resume_parse_failed", attempts=exc.attempts)
            raise AppError(
                "简历解析失败，请稍后重试", code="parse_failed", status_code=502
            ) from exc

        async with resources.sessionmaker() as session:
            resume = Resume(
                id=resume_id,
                user_id=user_id,
                file_key=key,
                parsed_json=parsed.value.model_dump(),
            )
            ResumeRepository(session, user_id).add(resume)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            stored = False
            await session.refresh(resume)
            # 会话关闭后对象会过期，响应要在关闭前组装好
            view = _view(resume)
        log.info("resume_created", resume_id=str(resume_id), bytes=len(data))
        return view
    finally:
        if stored:
            await _discard(storage, bucket, key, resume_id)


@router.get("/{resume_id}")
async def get_resume(resume_id: uuid.UUID, user_id: CurrentUserId, session: DbSession) -> ResumeOut:
    return _view(await _owned(session, user_id, resume_id))


@router.patch("/{resume_id}")
async def update_resume(
    resume_id: uuid.UUID,
    body: ResumeReplacement,
    user_id: CurrentUserId,
    session: DbSession,
) -> ResumeOut:
    resume = await _owned(session, user_id, resume_id)
    resume.parsed_json = redact_contacts(
        ResumeParsed.model_validate(body.model_dump())
    ).model_dump()
    resume.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(resume)
    return _view(resume)
