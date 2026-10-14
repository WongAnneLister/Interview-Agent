# 常用命令入口。Python 相关命令都在 backend/ 下通过 uv 运行。
# Windows 上可以用 Git Bash + make（例如 winget install ezwinports.make），
# 或者直接执行每个目标里对应的 uv 命令。

BACKEND := backend
UV := uv run --directory $(BACKEND)

.PHONY: help install fmt lint typecheck test check test-live hooks obs-smoke migrate migration seed test-db \
	vps-deploy vps-env vps-ps vps-logs obs-up obs-down tunnel check-env fetch-livekit livekit

help:
	@echo "install    安装依赖并安装 git hooks"
	@echo "fmt        自动格式化并修复可自动修复的 lint 问题"
	@echo "lint       格式检查 + lint（不修改文件）"
	@echo "typecheck  pyright 类型检查"
	@echo "test       单元测试（不含 live 测试）"
	@echo "check      lint + typecheck + test（合并前必须通过）"
	@echo "test-live  调用真实厂商的测试（需要 .env，手动运行）"
	@echo "migrate    把数据库迁移到最新版本（需要隧道）"
	@echo "migration  按模型变化生成迁移：make migration name=add_xxx（需要隧道，生成后人工检查）"
	@echo "seed       创建本地用户并把 id 写入 .env 的 DEFAULT_USER_ID（需要隧道，先 migrate）"
	@echo "test-db    数据库集成测试：建临时库、跑迁移和测试、结束后删除（需要隧道）"
	@echo "obs-smoke  发送一条示例链路到 Langfuse 并暴露示例指标（需要隧道和 obs-up）"
	@echo "vps-deploy 部署 / 更新 VPS 上的开发依赖（可重复执行）"
	@echo "obs-up     启动可观测性服务（本机 Langfuse + VPS 上的 Prometheus、Grafana）"
	@echo "obs-down   停止可观测性服务（数据保留，不影响核心服务）"
	@echo "vps-env    把 VPS 上生成的连接信息写入本机 .env（不显示密码）"
	@echo "vps-ps     查看 VPS 上依赖容器的状态"
	@echo "vps-logs   查看 VPS 上依赖容器的日志（service=postgres 指定服务）"
	@echo "tunnel     建立到 VPS 的 SSH 隧道（开发时保持运行）"
	@echo "check-env  检查各依赖是否可用（需要隧道已建立）"
	@echo "fetch-livekit 下载并校验本机 LiveKit Server（官方 release）"
	@echo "livekit    在本机启动 LiveKit Server（前台运行）"

install:
	uv sync --directory $(BACKEND)
	$(UV) pre-commit install

hooks:
	$(UV) pre-commit run --all-files

fmt:
	$(UV) ruff format .
	$(UV) ruff check --fix .

lint:
	$(UV) ruff format --check .
	$(UV) ruff check .

typecheck:
	$(UV) pyright

test:
	$(UV) pytest

check: lint typecheck test

test-live:
	$(UV) pytest -m live

migrate:
	$(UV) alembic upgrade head

migration:
	@test -n "$(name)" || { echo "用法：make migration name=add_xxx"; exit 1; }
	$(UV) alembic revision --autogenerate -m "$(name)"

seed:
	$(UV) python -m interview.db.seed

test-db:
	$(UV) pytest -m db

obs-smoke:
	$(UV) python tools/obs_smoke.py

vps-deploy:
	bash scripts/vps-deploy.sh

vps-env:
	bash scripts/vps-env.sh

vps-ps:
	bash -c 'source scripts/_lib.sh && vps_ssh "cd $$VPS_DIR && docker compose --profile observability ps -a"'

vps-logs:
	bash -c 'source scripts/_lib.sh && vps_ssh "cd $$VPS_DIR && docker compose --profile observability logs --tail=100 $(service)"'

obs-up:
	bash scripts/obs.sh up

obs-down:
	bash scripts/obs.sh down

tunnel:
	bash scripts/tunnel.sh

check-env:
	python scripts/check_env.py

fetch-livekit:
	bash scripts/fetch-livekit.sh

livekit:
	bash scripts/livekit.sh
