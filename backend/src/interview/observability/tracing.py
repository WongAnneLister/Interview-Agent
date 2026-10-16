"""OpenTelemetry 链路追踪：按进程初始化，用 OTLP/HTTP 导出到自部署的 Langfuse。

- span() / @traced / llm_span() 是业务代码唯一需要用的入口；统一属性名见 Attr。
- interview_id、is_eval 在 span 内写入 contextvar，之后在该上下文中开始的所有 span
  （包括 httpx、SQLAlchemy 等自动埋点产生的）都会带上，Langfuse 才能按它们过滤。
  不用 OTel Baggage：Baggage 会随 HTTP 请求头发给外部厂商。
- 同时写入 Langfuse 能识别的字段（session.id、langfuse.observation.* 等），
  映射规则见 https://langfuse.com/integrations/native/opentelemetry 。
- OBSERVABILITY_ENABLED=false 时使用 ALWAYS_OFF 采样器：span 不记录、不导出，
  但仍有合法的 trace_id，日志关联照常工作。init_tracing() 之前所有工具同样是空操作。
"""

from __future__ import annotations

import base64
import functools
import inspect
import json
from collections.abc import Awaitable, Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, cast

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from opentelemetry.util.types import AttributeValue

from interview.config import LangfuseSettings, ObservabilitySettings

ServiceName = Literal["api", "worker", "eval"]
ObservationType = Literal["span", "generation", "agent", "tool", "event"]

_INSTRUMENTATION_NAME = "interview"
_EXPORT_TIMEOUT_S = 10.0


class Attr:
    """统一属性名（OTel 侧），所有 span 都用这些名字。"""

    INTERVIEW_ID = "interview.id"
    TURN_ID = "turn.id"
    AGENT_ROLE = "agent.role"
    PROVIDER = "provider"
    MODEL = "model"
    IS_EVAL = "is_eval"


class LF:
    """Langfuse 识别的属性名。"""

    SESSION_ID = "session.id"
    TRACE_TAGS = "langfuse.trace.tags"
    ENVIRONMENT = "langfuse.environment"
    TYPE = "langfuse.observation.type"
    INPUT = "langfuse.observation.input"
    OUTPUT = "langfuse.observation.output"
    MODEL = "langfuse.observation.model.name"
    MODEL_PARAMETERS = "langfuse.observation.model.parameters"
    USAGE_DETAILS = "langfuse.observation.usage_details"
    COST_DETAILS = "langfuse.observation.cost_details"
    COMPLETION_START_TIME = "langfuse.observation.completion_start_time"
    PROMPT_NAME = "langfuse.observation.prompt.name"
    PROMPT_VERSION = "langfuse.observation.prompt.version"
    METADATA = "langfuse.observation.metadata."  # 后接顶层键名，Langfuse 只能按顶层键过滤


# 当前上下文中需要传给所有后续 span 的属性
_inherited: ContextVar[Mapping[str, AttributeValue]] = ContextVar(
    "interview_inherited_span_attrs", default=MappingProxyType({})
)
_provider: TracerProvider | None = None


class _ContextAttributesProcessor(SpanProcessor):
    """span 开始时盖上环境和继承属性，自动埋点产生的 span 也能按 interview 过滤。"""

    def __init__(self, environment: str) -> None:
        self._environment = environment

    def on_start(self, span: SdkSpan, parent_context: context_api.Context | None = None) -> None:
        span.set_attribute(LF.ENVIRONMENT, self._environment)
        span.set_attributes(_inherited.get())


def langfuse_exporter(langfuse: LangfuseSettings) -> OTLPSpanExporter:
    """Langfuse 的 OTLP/HTTP 接收端：Basic 鉴权（公钥:私钥），v4 需要声明 ingestion 版本。"""
    auth = f"{langfuse.public_key}:{langfuse.secret_key.get_secret_value()}"
    return OTLPSpanExporter(
        endpoint=f"{langfuse.host.rstrip('/')}/api/public/otel/v1/traces",
        headers={
            "Authorization": "Basic " + base64.b64encode(auth.encode()).decode(),
            "x-langfuse-ingestion-version": "4",
        },
        timeout=_EXPORT_TIMEOUT_S,
    )


def init_tracing(
    service: ServiceName,
    observability: ObservabilitySettings,
    langfuse: LangfuseSettings | None = None,
    *,
    environment: str = "dev",
    exporter: SpanExporter | None = None,
    set_global: bool = True,
) -> TracerProvider:
    """进程启动时调用一次。exporter 供测试注入（如内存导出器，同步导出）。

    set_global=True 时同时设为全局 TracerProvider，第三方库的埋点也会走这里；
    OTel 全局只能设置一次，测试中传 False。
    """
    global _provider
    shutdown_tracing()
    resource = Resource.create({"service.name": service, "deployment.environment": environment})
    if not observability.enabled:
        provider = TracerProvider(sampler=ALWAYS_OFF, resource=resource)
    else:
        sampler = ParentBased(TraceIdRatioBased(observability.sample_ratio))
        provider = TracerProvider(sampler=sampler, resource=resource)
        provider.add_span_processor(_ContextAttributesProcessor(environment))
        if exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        else:
            if langfuse is None or not langfuse.configured:
                raise ValueError("启用可观测性时必须提供 Langfuse 地址和密钥")
            provider.add_span_processor(BatchSpanProcessor(langfuse_exporter(langfuse)))
    if set_global:
        trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def shutdown_tracing() -> None:
    """导出剩余 span 并释放；进程退出前调用。"""
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None


def tracer_provider() -> trace.TracerProvider:
    """自动埋点用的 TracerProvider；未初始化时为全局（默认空操作）的那个。"""
    return _provider or trace.get_tracer_provider()


def _tracer() -> Tracer:
    return tracer_provider().get_tracer(_INSTRUMENTATION_NAME)


def _to_json(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


@contextmanager
def span(
    name: str,
    *,
    interview_id: str | None = None,
    turn_id: str | None = None,
    role: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    is_eval: bool | None = None,
    observation_type: ObservationType = "span",
    **attrs: AttributeValue,
) -> Generator[Span]:
    """开一个 span 并设为当前 span；异常会被记录并把状态设为 ERROR，然后继续抛出。

    interview_id、is_eval 会传给块内开始的所有子 span。
    """
    own, inherited = _span_attributes(
        interview_id=interview_id,
        turn_id=turn_id,
        role=role,
        provider=provider,
        model=model,
        is_eval=is_eval,
        observation_type=observation_type,
        attrs=attrs,
    )
    token = _inherited.set(inherited)
    try:
        with _tracer().start_as_current_span(name, attributes=own) as current:
            yield current
    finally:
        _inherited.reset(token)


def _span_attributes(
    *,
    interview_id: str | None,
    turn_id: str | None,
    role: str | None,
    provider: str | None,
    model: str | None,
    is_eval: bool | None,
    observation_type: ObservationType,
    attrs: Mapping[str, AttributeValue],
) -> tuple[dict[str, AttributeValue], dict[str, AttributeValue]]:
    """返回 (span 自身的属性, 传给子 span 的继承属性)。"""
    inherited = dict(_inherited.get())
    if interview_id is not None:
        inherited[Attr.INTERVIEW_ID] = interview_id
        inherited[LF.SESSION_ID] = interview_id  # 一场面试在 Langfuse 里是一个会话
    if is_eval is not None:
        inherited[Attr.IS_EVAL] = is_eval
        if is_eval:
            inherited[LF.TRACE_TAGS] = ("eval",)

    # 显式写类型：否则带 model 属性的普通 span 会被 Langfuse 推断成 generation
    own: dict[str, AttributeValue] = {LF.TYPE: observation_type, **inherited}
    for key, meta_key, value in (
        (Attr.TURN_ID, "turn_id", turn_id),
        (Attr.AGENT_ROLE, "agent_role", role),
        (Attr.PROVIDER, "provider", provider),
    ):
        if value is not None:
            own[key] = value
            own[LF.METADATA + meta_key] = value
    if model is not None:
        own[Attr.MODEL] = model
    own.update(attrs)
    return own, inherited


class OpenSpan:
    """不设为当前 span 的 span，用于跨越多次 await / yield 的流式调用。

    在 async generator 里直接用 span() 会让它在挂起期间一直是调用方的当前 span，
    所以改为每次取值时用 activate() 临时激活（子 span 挂在它下面、继承 interview_id），
    结束时调用 end()。
    """

    def __init__(self, target: Span, inherited: Mapping[str, AttributeValue]) -> None:
        self.span = target
        self._inherited = MappingProxyType(dict(inherited))
        self._ended = False

    @contextmanager
    def activate(self) -> Generator[Span]:
        token = _inherited.set(self._inherited)
        try:
            with trace.use_span(
                self.span, end_on_exit=False, record_exception=False, set_status_on_exception=False
            ):
                yield self.span
        finally:
            _inherited.reset(token)

    def end(self, error: BaseException | None = None) -> None:
        """结束 span；有 error 时记录异常并把状态设为 ERROR（与 span() 一致）。可重复调用。"""
        if self._ended:
            return
        self._ended = True
        if error is not None:
            self.span.record_exception(error)
            self.span.set_status(Status(StatusCode.ERROR, f"{type(error).__name__}: {error}"))
        self.span.end()


def open_span(
    name: str,
    *,
    interview_id: str | None = None,
    turn_id: str | None = None,
    role: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    is_eval: bool | None = None,
    observation_type: ObservationType = "span",
    **attrs: AttributeValue,
) -> OpenSpan:
    """与 span() 参数和属性相同，但只开始、不设为当前 span；父 span 取调用时的当前 span。"""
    own, inherited = _span_attributes(
        interview_id=interview_id,
        turn_id=turn_id,
        role=role,
        provider=provider,
        model=model,
        is_eval=is_eval,
        observation_type=observation_type,
        attrs=attrs,
    )
    return OpenSpan(_tracer().start_span(name, attributes=own), inherited)


def llm_attributes(
    model: str,
    *,
    input: Any = None,
    model_parameters: Mapping[str, Any] | None = None,
    prompt_name: str | None = None,
    prompt_version: int | None = None,
) -> dict[str, AttributeValue]:
    """generation 专有的属性（型号、输入、参数、提示词版本），供 llm_span() 和 open_span() 使用。"""
    attrs: dict[str, AttributeValue] = {LF.MODEL: model, "gen_ai.request.model": model}
    if input is not None:
        attrs[LF.INPUT] = _to_json(input)
    if model_parameters is not None:
        attrs[LF.MODEL_PARAMETERS] = _to_json(model_parameters)
    if prompt_name is not None:
        attrs[LF.PROMPT_NAME] = prompt_name
    if prompt_version is not None:
        attrs[LF.PROMPT_VERSION] = prompt_version
    return attrs


@contextmanager
def llm_span(
    name: str,
    *,
    provider: str,
    model: str,
    role: str | None = None,
    interview_id: str | None = None,
    turn_id: str | None = None,
    is_eval: bool | None = None,
    input: Any = None,
    model_parameters: Mapping[str, Any] | None = None,
    prompt_name: str | None = None,
    prompt_version: int | None = None,
) -> Generator[Span]:
    """大模型调用：在 Langfuse 里显示为 generation（型号、参数、输入、提示词版本）。

    输出和 token 用 set_llm_output() 写入；用量入库与成本由 observability/usage.py 负责。
    """
    attrs = llm_attributes(
        model,
        input=input,
        model_parameters=model_parameters,
        prompt_name=prompt_name,
        prompt_version=prompt_version,
    )
    with span(
        name,
        interview_id=interview_id,
        turn_id=turn_id,
        role=role,
        provider=provider,
        model=model,
        is_eval=is_eval,
        observation_type="generation",
        **attrs,
    ) as current:
        yield current


def set_llm_output(
    target: Span,
    output: Any = None,
    *,
    usage: Mapping[str, int] | None = None,
    completion_start_time: datetime | None = None,
) -> None:
    """写入 generation 的输出、token 用量（键用 Langfuse 约定：input / output / total 等）
    和首 token 时间。只写 span 属性，不记录用量表；之后调用 usage.record_usage() 会覆盖 usage。"""
    if output is not None:
        target.set_attribute(LF.OUTPUT, _to_json(output))
    if usage is not None:
        target.set_attribute(LF.USAGE_DETAILS, json.dumps(dict(usage)))
    if completion_start_time is not None:
        target.set_attribute(LF.COMPLETION_START_TIME, completion_start_time.isoformat())


def traced[**P, R](
    name: str | None = None,
    *,
    role: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """装饰器版 span()，同步和 async 函数都可用；默认用函数的限定名作为 span 名。"""

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        span_name = name or fn.__qualname__
        options: dict[str, Any] = {"role": role, "provider": provider, "model": model}

        if inspect.iscoroutinefunction(fn):
            async_fn = cast(Callable[P, Awaitable[Any]], fn)

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                with span(span_name, **options):
                    return await async_fn(*args, **kwargs)

            return cast(Callable[P, R], async_wrapper)

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with span(span_name, **options):
                return fn(*args, **kwargs)

        return wrapper

    return decorate
