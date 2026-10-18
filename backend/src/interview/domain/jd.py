"""JD 预览：校验模型输出、归一化能力权重、清理技术术语。"""

from __future__ import annotations

from math import fsum, isclose

from pydantic import BaseModel, ConfigDict, Field, field_validator

from interview.db.models import UsageRole
from interview.observability.tracing import span
from interview.prompts.loader import get_prompt_library
from interview.prompts.structured import StructuredResult, generate_structured
from interview.providers.base import LLMProvider

MAX_JD_CHARS = 10_000
MAX_TERM_CHARS = 20
MAX_TERM_COUNT = 30


class JDText(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_JD_CHARS, description="职位描述原文")

    @field_validator("text")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("职位描述不能为空白")
        return value


class Competency(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(min_length=1, description="能力项名称")
    description: str = Field(min_length=1, description="与岗位要求相关的能力描述")
    weight: float = Field(ge=0, allow_inf_nan=False, strict=True, description="相对权重")
    probes: list[str] = Field(min_length=1, description="考察要点")

    @field_validator("probes")
    @classmethod
    def non_blank_probes(cls, values: list[str]) -> list[str]:
        if any(not value for value in values):
            raise ValueError("考察要点不能为空白")
        return values


class JDParsed(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    title: str = Field(description="岗位名称，未注明时为空")
    level: str = Field(description="级别推断，无法判断时为空")
    competencies: list[Competency] = Field(min_length=3, max_length=6)
    tech_terms: list[str] = Field(description="技术术语，最多 30 个，每个不超过 20 字符")

    @field_validator("competencies")
    @classmethod
    def normalize_weights(cls, values: list[Competency]) -> list[Competency]:
        # 先缩放避免有限大数相加溢出；至少有一个正权重才能归一化。
        scale = max(item.weight for item in values)
        if scale == 0:
            raise ValueError("能力项权重不能全部为零")
        if scale <= 1 and isclose(
            fsum(item.weight for item in values), 1, abs_tol=1e-12, rel_tol=0
        ):
            return values  # 已归一化的 JSON 再次校验时保持稳定。
        scaled = [item.weight / scale for item in values]
        total = fsum(scaled)
        return [
            item.model_copy(update={"weight": weight / total})
            for item, weight in zip(values, scaled, strict=True)
        ]

    @field_validator("tech_terms")
    @classmethod
    def clean_terms(cls, values: list[str]) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()
        for value in values:
            term = " ".join(value.split())
            key = term.casefold()
            if not term or len(term) > MAX_TERM_CHARS or key in seen:
                continue
            seen.add(key)
            terms.append(term)
            if len(terms) == MAX_TERM_COUNT:
                break
        return terms


async def parse_jd(llm: LLMProvider, text: str) -> StructuredResult[JDParsed]:
    JDText(text=text)  # 领域入口也拒绝超长/空白输入，不产生无效的模型调用。
    prompt = get_prompt_library().render("jd_parse", jd_text=text)
    with span("jd.parse", role=UsageRole.JD_PARSE.value):
        return await generate_structured(llm, prompt, JDParsed, max_tokens=4096)
