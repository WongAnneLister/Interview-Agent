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

## 开发依赖（部署在 VPS 上）

Postgres、Redis、S3 兼容对象存储（SeaweedFS）以及 Prometheus、Grafana 运行在 VPS 的 Docker 中，端口只绑定在服务器的 127.0.0.1 上，本机通过 SSH 隧道访问。需要先在 `.env` 中填好 `VPS_HOST / VPS_PORT / VPS_USER`，并配置好 SSH 密钥免密登录。

```bash
make vps-deploy   # 部署或更新 VPS 上的依赖（首次会在服务器上随机生成密码）
make vps-env      # 把连接信息写入本机 .env（不显示密码）
make tunnel       # 建立 SSH 隧道，开发时保持运行
make check-env    # 检查各依赖是否可用
make migrate      # 把数据库迁移到最新版本
```

改了 `backend/src/interview/db/models.py` 之后，用 `make migration name=add_xxx` 生成迁移，人工检查后再 `make migrate`。

隧道建立后：Postgres `127.0.0.1:25432`，Redis `:26379`，S3 `http://127.0.0.1:29000`（文件浏览 `http://127.0.0.1:29001`）。

### 可观测性（Langfuse、Prometheus、Grafana）

都放在 compose 的 `observability` profile 里，可以单独启停，不影响核心服务。

- **Langfuse v4 运行在本机 Docker**（`deploy/local/docker-compose.yml`）：VPS 内存放不下 ClickHouse，所以本机这套自带 Postgres、Redis、S3（SeaweedFS）和 ClickHouse，合计内存上限约 6 GB（Docker Desktop 至少分配 8 GB），与 VPS 上的业务依赖互不影响。链路数据直接发到本机，不经过隧道。
- **Prometheus、Grafana 仍在 VPS**：通过隧道的反向端口抓取本机 API、语音 worker 和评测进程的指标。

```bash
make vps-deploy   # 上传最新配置（Prometheus 抓取任务等）
make obs-up       # 生成本机 Langfuse 的随机密码和 API 密钥（写入 .env），启动本机 Langfuse 和 VPS 上的 Prometheus、Grafana
make tunnel       # Grafana / Prometheus 走隧道
make check-env    # Langfuse / Prometheus / Grafana 显示 OK；Langfuse 首次启动需要 2–3 分钟完成数据库迁移
make obs-down     # 不用时停止（数据保留）
```

| 服务 | 地址 | 登录 |
| --- | --- | --- |
| Langfuse | `http://127.0.0.1:23100` | `.env` 中的 `LANGFUSE_ADMIN_EMAIL` / `LANGFUSE_ADMIN_PASSWORD` |
| Grafana | `http://127.0.0.1:23000` | 用户 admin，密码见 `.env` 中的 `GRAFANA_ADMIN_PASSWORD` |
| Prometheus | `http://127.0.0.1:29090` | 无 |

首次 `make obs-up` 要下载约 1.5 GB 镜像（Langfuse web / worker、ClickHouse）。国内网络拉 Docker Hub 很慢时，可以先在 Docker Desktop 配好镜像加速源；仍然不行的话，可以在 VPS 上拉取 arm64 版本后经 SSH 传回本机，例如：

```bash
ssh -p <端口> root@<VPS> "docker pull -q --platform linux/arm64 langfuse/langfuse:4 && docker save --platform linux/arm64 langfuse/langfuse:4 | gzip" | gunzip | docker load
```

验证整条链路：`.env` 中设置 `OBSERVABILITY_ENABLED=true` 后运行 `make obs-smoke`，脚本会发送一条示例链路（HTTP 请求 → 数据库查询 → 大模型 generation），并打印 Langfuse 里这条链路的地址；同时在 `EVAL_METRICS_PORT` 上暴露 `interview_smoke_total` 等指标 60 秒，可在 Grafana 的 Explore 里查到。

Langfuse 首次启动时会自动创建组织 `Interview Agent`、项目 `interview-agent-dev` 和该项目的 API 密钥（即 `.env` 中的 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`），无需在网页上手动创建。Grafana 已自动配置 Prometheus 数据源。本机 API、语音 worker、评测进程没运行时，Prometheus 里对应的目标显示为 down，属于正常现象。

### LiveKit（本机运行）

LiveKit Server 以原生程序运行在本机（浏览器、语音 worker 都在本机，媒体不出本机），支持 Windows（Git Bash）和 macOS。

```bash
make fetch-livekit   # 准备 LiveKit Server，放到 .tools/livekit/
make livekit         # 启动（前台运行）；首次会生成 LIVEKIT_API_KEY / SECRET 写入 .env
```

- Windows：下载官方 release 并校验 SHA-256。
- macOS：官方 release 没有 macOS 版，脚本会拉取固定 tag 的官方源码、校验 commit 哈希后用 Go 编译。需要 git、Go（1.21 以上任意版本，会自动下载 LiveKit 要求的工具链）和 Xcode 命令行工具（`xcode-select --install`）。首次编译需下载几百 MB 依赖；国内网络可先设置 `export GOPROXY=https://goproxy.cn,direct`。

信令地址 `ws://127.0.0.1:7880`。媒体 UDP 端口 7882 绑定在局域网 IP 上（WebRTC 不使用回环地址做 ICE 候选），系统防火墙首次可能弹窗询问是否允许传入连接（Windows 选“专用网络”，macOS 选“允许”）；没有 token 无法加入房间。

## 配置

复制 `.env.example` 为 `.env` 后按注释填写。`.env` 包含密钥，不能提交（pre-commit 会拦截）。

## 目录

```text
backend/   Python 后端、语音 worker、评测与实测工具
web/       Next.js 前端（PR-23 起）
docs/      PRD、开发计划、ADR、实测报告
```
