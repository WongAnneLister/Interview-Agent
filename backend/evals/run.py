"""文字层评测入口：make eval / make eval-quick。

用法：python -m evals.run [--quick] [--personas strong,weak] [--fixtures backend] [--concurrency 3]
需要隧道（数据库）和真实厂商密钥；用量记在评测用户 eval@localhost 名下并带 is_eval。
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from datetime import datetime

import structlog
from sqlalchemy import func, select

from evals import langfuse_scores, results
from evals.cost import run_cost
from evals.data import Fixture, Persona, load_fixtures, load_personas
from evals.data import select as pick
from evals.metrics import SessionMetrics, aggregate, score_session
from evals.preflight import probe
from evals.session import EvalContext, SessionOutcome, run_session
from evals.stability import Stability, measure
from interview.config import Settings, load_settings
from interview.db.seed import ensure_default_user
from interview.db.session import create_engine, make_sessionmaker
from interview.logging import configure_logging
from interview.observability.tracing import init_tracing, shutdown_tracing
from interview.prompts.loader import get_prompt_library
from interview.providers.registry import ProviderRegistry

EVAL_EMAIL = "eval@localhost"
QUICK_FIXTURE = "backend"  # 快速模式所有人设都用同一份材料，区分度可直接比较

log = structlog.get_logger(__name__)


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="模拟候选人文字层评测")
    parser.add_argument("--quick", action="store_true", help="每个人设只跑 1 场（同一份材料）")
    parser.add_argument("--personas", help="逗号分隔的人设 id，默认全部")
    parser.add_argument("--fixtures", help="逗号分隔的材料 id，默认全部（--quick 时为 backend）")
    parser.add_argument("--concurrency", type=int, default=3, help="同时进行的面试场数")
    parser.add_argument("--repeats", type=int, default=5, help="稳定性重复评分次数，0 表示跳过")
    return parser.parse_args(argv)


def _split(value: str | None) -> list[str] | None:
    return [item.strip() for item in value.split(",") if item.strip()] if value else None


def _models(settings: Settings) -> dict[str, str]:
    p = settings.providers
    roles = {"front": p.llm_front, "eval": p.llm_eval, "live_eval": p.llm_live_eval}
    return {k: f"{v.provider}/{v.model}" for k, v in roles.items() if v.configured}


async def run(argv: list[str] | None = None) -> int:
    args = _args(argv)
    personas_all, fixtures_all = load_personas(), load_fixtures()
    personas = pick(dict(personas_all), _split(args.personas), "人设")
    default_fixtures = [QUICK_FIXTURE] if args.quick else None
    fixtures = pick(dict(fixtures_all), _split(args.fixtures) or default_fixtures, "材料")
    settings = load_settings()
    configure_logging(level=settings.app.log_level, fmt=settings.app.log_format)
    init_tracing(
        "eval", settings.observability, settings.langfuse, environment=settings.app.app_env
    )
    engine = create_engine(settings.db.url.get_secret_value())
    sm = make_sessionmaker(engine)
    providers = ProviderRegistry(settings.providers, sessionmaker=sm)
    started = time.perf_counter()
    run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    try:
        try:
            user_id = await ensure_default_user(engine, EVAL_EMAIL)
        except OSError as exc:
            print(f"连不上数据库（{type(exc).__name__}）：先在另一个终端运行 make tunnel，再重试。")
            return 1
        async with sm() as session:
            since = await session.scalar(select(func.now()))  # 用数据库时间，避免时区和时钟差
        assert since is not None  # noqa: S101
        print("探测各模型（关闭主备切换）…", flush=True)
        problems = await probe(settings.providers, user_id, sm)
        if problems:
            print("以下模型连不上，评测会悄悄跑在备用模型上，已停止：")
            for item in problems:
                print(f"  ❌ {item}")
            return 1
        ec = EvalContext(sm, providers, user_id, settings.report)
        semaphore = asyncio.Semaphore(args.concurrency)

        async def one(fixture: Fixture, persona: Persona) -> SessionMetrics:
            async with semaphore:
                print(f"开始 {fixture.id}/{persona.id}", flush=True)
                outcome: SessionOutcome = await run_session(ec, fixture, persona)
                try:
                    metrics = await score_session(ec, outcome, persona)
                except Exception as exc:  # 打分阶段出错（如隧道断开）只记在这一场，不中断整次评测
                    outcome.error = outcome.error or f"打分失败 {type(exc).__name__}: {exc}"
                    metrics = await score_session(ec, outcome, persona)  # 有错误时不再读库
                print(
                    f"完成 {fixture.id}/{persona.id}：总分 {metrics.overall_score}"
                    f"{'，出错 ' + metrics.error if metrics.error else ''}",
                    flush=True,
                )
                return metrics

        sessions = list(
            await asyncio.gather(
                *(one(fixtures_all[f], personas_all[p]) for f in fixtures for p in personas)
            )
        )
        stability: Stability | None = None
        base = next((s for s in sessions if s.persona == "average" and s.error is None), None)
        if base is not None and args.repeats > 0:
            print(f"稳定性：{base.fixture}/average 重复评分 {args.repeats} 次", flush=True)
            try:
                stability = await measure(ec, uuid.UUID(base.interview_id), args.repeats)
            except Exception as exc:
                print(f"稳定性重跑失败（{type(exc).__name__}），跳过", flush=True)
        library = get_prompt_library()
        prompt_versions = {i: library.get(i).meta.version for i in library.ids()}
        upload = await langfuse_scores.upload(settings.langfuse, run_id, sessions, prompt_versions)
        try:
            cost = await run_cost(sm, user_id, since)
        except OSError as exc:
            print(f"汇总费用时连不上数据库（{type(exc).__name__}），费用记为未知", flush=True)
            cost = None
        result = results.RunResult(
            run_id=run_id,
            mode="quick" if args.quick else "full",
            personas=personas,
            fixtures=fixtures,
            prompt_versions=prompt_versions,
            models=_models(settings),
            metrics=aggregate(sessions, stability),
            sessions=sessions,
            wall_s=round(time.perf_counter() - started, 1),
            langfuse=upload,
            **results.cost_fields(cost),
        )
        path = results.write(result)
    finally:
        await providers.aclose()
        await engine.dispose()
        shutdown_tracing()
    failed = [m for m in result.metrics if m.passed is False]
    print(
        f"\n结果：{path}\n估算费用：{results.money(result.cost_cny)}；未达标指标 {len(failed)} 项"
    )
    for m in failed:
        print(f"  ❌ {m.name}：{m.value}（目标 {m.target}）")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
