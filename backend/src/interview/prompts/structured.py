"""结构化输出：让大模型输出 JSON，用 Pydantic 校验，失败时把错误反馈给模型重试。

- 系统提示末尾自动加上输出要求和 JSON Schema（含 “JSON” 字样：百炼和 DeepSeek 的 JSON 模式
  都要求消息里出现这个词）。json_mode=True 时请求带 response_format={"type": "json_object"}；
  两家都支持（https://help.aliyun.com/zh/model-studio/json-mode 、
  https://api-docs.deepseek.com/guides/json_mode ），
  但都只保证“是 JSON”，不保证字段，所以一律再校验。
- 校验失败：把模型上次的输出和错误摘要（只含字段位置和错误类型，不含输入内容）追加到对话里重试，
  最多 max_retries 次；重试调用的用量标记 is_retry（重试也是成本）。用尽后抛 StructuredOutputError。
- prompt id / 版本 / 内容哈希 / 重试次数写入当前 span（Langfuse 的提示词字段同时写入）。
  日志不记录模型输出。
- 厂商错误（超时、限流等）不在这里重试，直接抛出，由注册表的主备切换处理。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog
from opentelemetry import trace
from pydantic import BaseModel, ValidationError

from interview.observability.tracing import LF
from interview.prompts.loader import RenderedPrompt
from interview.providers.base import ChatMessage, LLMProvider
from interview.providers.observed import marked_retry

DEFAULT_MAX_RETRIES = 2
SCHEMA_INSTRUCTION = (
    "输出要求：只输出一个 JSON 对象，不要输出解释、Markdown 或代码块标记。"
    "JSON 必须符合下面的 JSON Schema：\n{schema}"
)
RETRY_INSTRUCTION = (
    "你上一次的输出不符合要求，问题如下：\n{errors}\n"
    "请修正后重新输出完整的 JSON 对象，只输出 JSON。"
)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    value: T
    prompt_id: str
    prompt_version: int
    retries: int  # 0 表示第一次就成功
    provider: str = ""  # 产出 value 的那次调用实际用的厂商和型号（可能是备用）
    model: str = ""


@dataclass
class StructuredOutputError(Exception):
    """重试用尽仍无法得到合法输出。errors 是每次失败的错误摘要（不含模型输出原文）。"""

    prompt_id: str
    prompt_version: int
    attempts: int
    errors: list[str] = field(default_factory=list[str])

    def __str__(self) -> str:
        return (
            f"提示词 {self.prompt_id} v{self.prompt_version} 尝试 {self.attempts} 次"
            f"仍未得到合法的结构化输出；最后一次的问题：{self.errors[-1] if self.errors else '-'}"
        )


class _Invalid(Exception):
    def __init__(self, summary: str) -> None:
        super().__init__(summary)
        self.summary = summary


def extract_json(text: str) -> str:
    """去掉代码块标记和 JSON 前后的多余文字，返回最可能是 JSON 对象的那一段。"""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    if not stripped.startswith("{"):
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end > start:
            stripped = stripped[start : end + 1]
    return stripped


def _summarize(exc: ValidationError) -> str:
    """错误摘要只保留字段位置、错误类型和说明，不带输入值（可能含用户数据）。"""
    lines: list[str] = []
    for error in exc.errors(include_input=False, include_url=False)[:10]:
        location = ".".join(str(part) for part in error["loc"]) or "(整体)"
        lines.append(f"- {location}：{error['msg']}（{error['type']}）")
    return "\n".join(lines)


def parse[T: BaseModel](
    text: str, schema: type[T], *, context: Mapping[str, Any] | None = None
) -> T:
    if not text.strip():
        raise _Invalid("- (整体)：输出为空")
    try:
        return schema.model_validate_json(extract_json(text), context=context)
    except ValidationError as exc:
        raise _Invalid(_summarize(exc)) from exc


def with_schema(messages: list[ChatMessage], schema: type[BaseModel]) -> list[ChatMessage]:
    """把输出要求和 JSON Schema 追加到系统提示（没有系统提示时新建一条）。"""
    instruction = SCHEMA_INSTRUCTION.format(
        schema=json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )
    if messages and messages[0]["role"] == "system":
        first: ChatMessage = {
            "role": "system",
            "content": f"{messages[0]['content']}\n\n{instruction}",
        }
        return [first, *messages[1:]]
    return [{"role": "system", "content": instruction}, *messages]


async def generate_structured[T: BaseModel](
    llm: LLMProvider,
    prompt: RenderedPrompt,
    schema: type[T],
    *,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    timeout_s: float | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    json_mode: bool = True,
    context: Mapping[str, Any] | None = None,
) -> StructuredResult[T]:
    """调用大模型生成符合 schema 的对象。max_tokens 要留足，JSON 被截断会被当作非法输出重试。

    context 透传给 model_validate_json()，供模型校验器读取（如计划阶段的 PlanContext）。
    """
    current = trace.get_current_span()
    current.set_attributes(
        {
            LF.PROMPT_NAME: prompt.id,
            LF.PROMPT_VERSION: prompt.version,
            "prompt.id": prompt.id,
            "prompt.version": prompt.version,
            "prompt.hash": prompt.content_hash,
            LF.METADATA + "prompt_hash": prompt.content_hash,
            "structured.schema": schema.__name__,
        }
    )
    extra = {"response_format": {"type": "json_object"}} if json_mode else None
    messages: list[ChatMessage] = with_schema(prompt.messages, schema)
    errors: list[str] = []
    for attempt in range(max_retries + 1):
        with marked_retry(attempt > 0):
            reply = await llm.complete(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout_s=timeout_s,
                extra=extra,
            )
        try:
            value = parse(reply.text, schema, context=context)
        except _Invalid as exc:
            errors.append(exc.summary)
            log.warning(
                "structured_output_invalid",
                prompt_id=prompt.id,
                prompt_version=prompt.version,
                attempt=attempt + 1,
                problems=exc.summary.count("\n") + 1,
            )
            messages = [
                *messages,
                {"role": "assistant", "content": reply.text},
                {"role": "user", "content": RETRY_INSTRUCTION.format(errors=exc.summary)},
            ]
            continue
        current.set_attributes({"structured.retries": attempt, "structured.failed": False})
        return StructuredResult(
            value, prompt.id, prompt.version, attempt, reply.provider, reply.model
        )
    current.set_attributes({"structured.retries": max_retries, "structured.failed": True})
    raise StructuredOutputError(prompt.id, prompt.version, max_retries + 1, errors)
