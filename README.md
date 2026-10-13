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

Postgres、Redis、S3 兼容对象存储（SeaweedFS）、Prometheus、Grafana 运行在 VPS 的 Docker 中，端口只绑定在服务器的 127.0.0.1 上，本机通过 SSH 隧道访问。需要先在 `.env` 中填好 `VPS_HOST / VPS_PORT / VPS_USER`，并配置好 SSH 密钥免密登录。

```bash
make vps-deploy   # 部署或更新 VPS 上的依赖（首次会在服务器上随机生成密码）
make vps-env      # 把连接信息写入本机 .env（不显示密码）
make tunnel       # 建立 SSH 隧道，开发时保持运行
make check-env    # 检查各依赖是否可用
```

隧道建立后：Postgres `127.0.0.1:25432`，Redis `:26379`，S3 `http://127.0.0.1:29000`（文件浏览 `http://127.0.0.1:29001`），Prometheus `http://127.0.0.1:29090`，Grafana `http://127.0.0.1:23000`（用户 admin，密码见 `.env` 中的 `GRAFANA_ADMIN_PASSWORD`）。

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
