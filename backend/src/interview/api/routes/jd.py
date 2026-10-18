"""JD 即时解析预览，不保存 JD 或创建面试计划。"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends

from interview.api.deps import CurrentUserId, get_jd_llm
from interview.api.errors import AppError
from interview.domain.jd import JDParsed, JDText, parse_jd
from interview.prompts.structured import StructuredOutputError
from interview.providers.base import LLMProvider

router = APIRouter(prefix="/api/jd", tags=["jd"])
log = structlog.get_logger(__name__)


@router.post("/parse")
async def parse_jd_route(
    body: JDText,
    user_id: CurrentUserId,
    llm: Annotated[LLMProvider, Depends(get_jd_llm)],
) -> JDParsed:
    try:
        result = await parse_jd(llm, body.text)
    except StructuredOutputError as exc:
        log.warning("jd_parse_failed", attempts=exc.attempts)
        raise AppError(
            "职位描述解析失败，请稍后重试", code="parse_failed", status_code=502
        ) from exc
    return result.value
