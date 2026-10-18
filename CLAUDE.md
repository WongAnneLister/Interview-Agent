# CLAUDE.md

本文件是给 Claude Code 的项目说明。每次开始任务前先读完它；与本文件冲突的做法需要在 PR 描述里说明理由。

## 项目简介

语音模拟面试 agent：候选人上传简历和 JD，与 AI 面试官进行 15–30 分钟语音面试，结束后得到带原话证据的评估报告。
当前阶段是 v0 个人版（只在本机运行），但代码按以后可商业化的标准来写。

- 产品需求：`docs/PRD-语音模拟面试Agent.md`
- 开发计划与每个 PR 的提示词：`docs/Claude-Code开工执行文档.md`

## 工作方式

- 一个 PR 只做一件事，代码改动目标 ≤ 400 行（不含锁文件、生成代码和测试数据）；超出时先提出拆分方案。
- 严格按提示词里的“要做 / 不做”执行，不顺手做范围外的事；有想法写进 PR 描述的“没做什么”。
- **不确定的第三方 API（LiveKit Agents、百炼接口、Langfuse 等）先查已安装版本的源码或官方文档，不要凭记忆写。** 与提示词描述不符时以实际版本为准，并在 PR 描述里说明。
- 提交信息用 Conventional Commits（feat / fix / chore / test / docs / ci / refactor）。
- 完成前必须运行 `make check`（Windows 没有 make 时，运行 Makefile 中对应的 uv 命令）并全部通过。
- 密钥只放在 `.env`，永远不提交；新增配置项同步写进 `.env.example` 并注明用途。
- `docs/research/` 是个人调研资料，已在 .gitignore 中排除，不要引用或提交。
- **遇到会影响核心体验、成本或稳定性的风险，或不容易看出来的坑，就是项目重难点，必须记进 `docs/challenges.md`**：写清发现时间、问题、影响、计划或修复和状态，解决了改状态，不删除。实测数字写 `docs/benchmarks/findings.md`，需要做设计决策的写 ADR，`challenges.md` 只链接过去。同时更新写给非程序员的 `docs/challenges-plain.md`。

## 工程约定

### 技术栈

| 部分 | 选型 |
| --- | --- |
| 后端与语音 worker | Python 3.12、uv、FastAPI、Pydantic v2、SQLAlchemy 2（async）+ asyncpg、Alembic、httpx、websockets、structlog |
| 语音框架 | LiveKit Agents（Python，版本在 PR-27 锁定）、Silero VAD、官方 MultilingualModel 轮次检测 |
| 前端 | Next.js（App Router、TypeScript）、pnpm、Tailwind、livekit-client、@livekit/components-react |
| 模型厂商 | 阿里云百炼（大模型、流式识别、流式合成）；DeepSeek 备用大模型 |
| 可观测性 | OpenTelemetry（统一埋点）；Langfuse 自部署（大模型与 agent 链路、成本、评测分数、提示词版本对比）；Prometheus + Grafana（语音时序指标与系统指标）；自建最小用量表（预算保护，v1 计费以此为准） |
| 开发依赖 | VPS 上的 Docker Compose（deploy/vps/）：PostgreSQL、Redis、S3 兼容对象存储（SeaweedFS）、Prometheus、Grafana，只绑定 127.0.0.1，本机经 SSH 隧道访问（make tunnel）；LiveKit Server 在本机以官方原生程序运行（make fetch-livekit / make livekit，配置 deploy/local/livekit.yaml）；Langfuse 的部署位置在 PR-04b 确定 |
| 质量 | ruff（格式 + lint）、pyright（src 严格模式）、pytest + pytest-asyncio、ESLint + tsc、pre-commit、GitHub Actions |

### 目录结构

```text
Interview-Agent/
  CLAUDE.md  Makefile  .env.example  README.md
  deploy/vps/        VPS 上开发依赖的 docker-compose、prometheus.yml、grafana/
  deploy/local/      本机运行的服务配置（livekit.yaml）
  scripts/           vps-deploy.sh  vps-env.sh  tunnel.sh  check_env.py  fetch-livekit.sh  livekit.sh
  backend/
    pyproject.toml
    src/interview/
      config.py  logging.py  metrics.py
      observability/ tracing.py  usage.py  pricing.py  budget.py
      db/            models.py  session.py  migrations/
      providers/     base.py  registry.py  fake.py  bailian_llm.py  bailian_asr.py  bailian_tts.py
      prompts/       *.md（带版本号）  loader.py  structured.py
      domain/        resume.py  jd.py  plan.py  state_machine.py  evaluator.py
                     interviewer.py  orchestrator.py  report.py
      api/           app.py  deps.py  routes/
      voice/         worker.py  stt_plugin.py  tts_plugin.py  turn_policy.py  events.py
      storage/       base.py  s3.py（S3 兼容接口，开发用 SeaweedFS，生产可换 OSS）
    config/          pricing.yaml（各型号单价，带生效日期）
    tests/           unit/  live/
    evals/           personas/  fixtures/  run.py
    tools/benchmark/ asr.py  tts.py  llm.py
  web/               Next.js 应用
  docs/              adr/  benchmarks/
```

### 编码规范

- 全部函数带类型注解；`src/` 通过 pyright 严格模式。
- 所有 IO 用 async；每个网络调用必须有超时。
- 业务代码只通过 `providers/` 的接口调用厂商，不直接 import 厂商 SDK。
- 对象存储只用标准 S3 API（如 aioboto3），不使用任何存储产品专有的 SDK。
- 所有配置与密钥走环境变量（`config.py` 统一读取），代码里不写死型号和地址。
- 每张业务表都有 `user_id`，每个查询都按 `user_id` 过滤（v0 只有一个种子用户）。
- 提示词放在 `prompts/*.md`，文件头写版本号；以 git 为准，Langfuse 只记录版本号，不用它做提示词主存储。改了提示词正文必须把 version 加一并运行 `make prompts-lock` 更新 `prompts/versions.lock`（`make check` 会检查）。
- 可观测性：每个外部调用（大模型、识别、合成、数据库、对象存储）和关键步骤都用 `observability/tracing.py` 开 span；每次厂商调用都用 `observability/usage.py` 记录用量；span 必须带 interview_id 和角色标签。
- 日志用 structlog 输出 JSON，带 `trace_id`、`interview_id`；日志里不输出简历正文、转写全文和密钥。（完整的输入输出只进本地自部署的 Langfuse，不进日志。）
- 模型输出的 JSON 一律用 Pydantic 校验，校验失败最多重试 2 次。

### 测试规范

- 纯逻辑（状态机、证据校验、计划校验）单元测试覆盖主要分支。
- 厂商用 `providers/fake.py` 的假实现做测试；真实调用的测试放 `tests/live/`，加 `@pytest.mark.live`，默认不跑。
- `make check` = 格式检查 + lint + 类型检查 + 单元测试（前后端）。

### PR 描述模板

```markdown
## 做了什么
## 怎么验证
## 没做什么（留给后续 PR）
## 风险与待确认
```
