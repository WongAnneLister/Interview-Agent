# Interview Agent

[![CI](https://github.com/WongAnneLister/Interview-Agent/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/WongAnneLister/Interview-Agent/actions/workflows/ci.yml)

语音模拟面试 agent：上传简历和 JD，与 AI 面试官进行一场 15–30 分钟的语音面试，结束后得到一份带原话证据的评估报告。

技术栈：Python（FastAPI、LiveKit Agents）+ Next.js，模型使用阿里云百炼。详见 [PRD](docs/PRD-语音模拟面试Agent.md) 和 [开发计划](docs/Claude-Code开工执行文档.md)。

> 当前处于 v0（个人版）开发阶段，功能按开发计划中的 PR 顺序逐步加入。

## 开发环境

需要：Python 3.12、[uv](https://docs.astral.sh/uv/)、Git。Windows 上建议在 Git Bash 中操作，并安装 make（例如 `winget install ezwinports.make`）。

```bash
make install   # 安装依赖并启用 pre-commit hooks
make check     # 格式检查 + lint + 类型检查 + 单元测试，合并前必须通过
```

没有 make 时，可以直接运行 Makefile 中对应的命令，例如：

```bash
uv sync --directory backend
uv run --directory backend pre-commit install
uv run --directory backend ruff format --check .
uv run --directory backend ruff check .
uv run --directory backend pyright
uv run --directory backend pytest
```

## 配置

复制 `.env.example` 为 `.env` 后按注释填写。`.env` 包含密钥，不能提交（pre-commit 会拦截）。

## 目录

```text
backend/   Python 后端、语音 worker、评测与实测工具
web/       Next.js 前端（PR-23 起）
docs/      PRD、开发计划、ADR、实测报告
```
