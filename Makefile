# 常用命令入口。Python 相关命令都在 backend/ 下通过 uv 运行。
# Windows 上可以用 Git Bash + make（例如 winget install ezwinports.make），
# 或者直接执行每个目标里对应的 uv 命令。

BACKEND := backend
UV := uv run --directory $(BACKEND)

.PHONY: help install fmt lint typecheck test check test-live hooks

help:
	@echo "install    安装依赖并安装 git hooks"
	@echo "fmt        自动格式化并修复可自动修复的 lint 问题"
	@echo "lint       格式检查 + lint（不修改文件）"
	@echo "typecheck  pyright 类型检查"
	@echo "test       单元测试（不含 live 测试）"
	@echo "check      lint + typecheck + test（合并前必须通过）"
	@echo "test-live  调用真实厂商的测试（需要 .env，手动运行）"

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
