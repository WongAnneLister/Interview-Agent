"""报告持久化结构与模型输出草稿；最终分数和有效证据由代码确定。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from interview.domain.evaluator import DraftEvidence, EvidenceQuote

REPORT_DISCLAIMER = "AI 生成，仅供练习参考，不代表任何企业的录用判断。"


class QuestionReview(BaseModel):
    question_id: str
    question: str
    competency: str
    asked: bool
    score: int | None = Field(default=None, ge=1, le=5)
    evidence: list[EvidenceQuote] = Field(default_factory=list[EvidenceQuote])
    summary: str = ""
    strengths: list[str] = Field(default_factory=list)
    missing_points: list[str] = Field(default_factory=list)
    improved_answer: str = ""


class CompetencyReview(BaseModel):
    name: str
    weight: float
    score: float | None
    conclusion: str
    evidence: list[EvidenceQuote]


class ReportBody(BaseModel):
    schema_version: Literal[1] = 1
    overall_score: float | None
    overall: str
    top_improvements: list[str]
    competencies: list[CompetencyReview]
    questions: list[QuestionReview]
    disclaimer: str = REPORT_DISCLAIMER
    degraded: list[str]


class QuestionReviewDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    score: int | None = Field(ge=1, le=5)
    evidence: list[DraftEvidence]
    summary: str
    strengths: list[str]
    missing_points: list[str]
    improved_answer: str


class CompetencyConclusion(BaseModel):
    name: str
    conclusion: str


class ReportSummaryDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    competencies: list[CompetencyConclusion]
    overall: str
    top_improvements: list[str] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_names(self, info: ValidationInfo) -> Self:
        context = cast(Mapping[str, Any] | None, info.context)
        expected = context.get("expected_names") if context else None
        names = [item.name for item in self.competencies]
        if not isinstance(expected, list):
            raise ValueError("缺少能力项名称的校验上下文")
        if len(names) != len(set(names)) or set(names) != set(cast(list[str], expected)):
            raise ValueError("能力项名称必须与输入完全一致且不能重复")
        return self


class QuestionReviewResult(QuestionReview):
    provider: str
    model: str
    prompt_version: str
    latency_ms: int
    retries: int
    evidence_dropped: int
    evidence_retried: bool
