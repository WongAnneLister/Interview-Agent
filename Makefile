# 常用命令入口。Python 相关命令都在 backend/ 下通过 uv 运行。
# Windows 上可以用 Git Bash + make（例如 winget install ezwinports.make），
# 或者直接执行每个目标里对应的 uv 命令。

BACKEND := backend
UV := uv run --directory $(BACKEND)

.PHONY: help install fmt lint typecheck test check test-live hooks \
	vps-deploy vps-env vps-ps vps-logs tunnel check-env

help:
	@echo "install    安装依赖并安装 git hooks"
	@echo "fmt        自动格式化并修复可自动修复的 lint 问题"
	@echo "lint       格式检查 + lint（不修改文件）"
	@echo "typecheck  pyright 类型检查"
	@echo "test       单元测试（不含 live 测试）"
	@echo "check      lint + typecheck + test（合并前必须通过）"
	@echo "test-live  调用真实厂商的测试（需要 .env，手动运行）"
	@echo "vps-deploy 部署 / 更新 VPS 上的开发依赖（可重复执行）"
	@echo "vps-env    把 VPS 上生成的连接信息写入本机 .env（不显示密码）"
	@echo "vps-ps     查看 VPS 上依赖容器的状态"
	@echo "vps-logs   查看 VPS 上依赖容器的日志（service=postgres 指定服务）"
	@echo "tunnel     建立到 VPS 的 SSH 隧道（开发时保持运行）"
	@echo "check-env  检查各依赖是否可用（需要隧道已建立）"

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

vps-deploy:
	bash scripts/vps-deploy.sh

vps-env:
	bash scripts/vps-env.sh

vps-ps:
	bash -c 'source scripts/_lib.sh && vps_ssh "cd $$VPS_DIR && docker compose ps"'

vps-logs:
	bash -c 'source scripts/_lib.sh && vps_ssh "cd $$VPS_DIR && docker compose logs --tail=100 $(service)"'

tunnel:
	bash scripts/tunnel.sh

check-env:
	python scripts/check_env.py
