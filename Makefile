# spineagent —— 一键开发 / CI 命令(始终从包根跑)。
#
# `make` 或 `make help` 列出全部目标。默认走包内 venv;可覆盖解释器:
#   make test PYTHON=python3.14
#
# 跨平台:用 $(VENV)/$(PYTHON) 变量(对齐家族 ragspine 的写法)。

.DEFAULT_GOAL := help
PYTHON ?= .venv/bin/python
VENV   ?= .venv

# ---- 安装 --------------------------------------------------------------------------

.PHONY: install
install: ## 建 venv 并可编辑安装:先装兄弟薄核 corespine,再装 spineagent[dev]
	uv venv $(VENV)
	VIRTUAL_ENV="$(CURDIR)/$(VENV)" uv pip install -e ../corespine
	VIRTUAL_ENV="$(CURDIR)/$(VENV)" uv pip install -e ".[dev]"

# ---- 质量门 ------------------------------------------------------------------------

.PHONY: ci
ci: fmt-check lint typecheck test ## 本地 CI 门:格式 + lint + 类型检查 + 测试(与 CI 同一套门)

.PHONY: test
test: ## 跑测试套件(离线、确定性)
	$(PYTHON) -m pytest -q

.PHONY: lint
lint: ## ruff 静态检查(风格 + import 序 + 死代码)
	$(PYTHON) -m ruff check src/spineagent tests examples benchmarks

.PHONY: typecheck
typecheck: ## mypy --strict 类型检查(出货代码 src/spineagent)
	$(PYTHON) -m mypy

.PHONY: fmt
fmt: ## ruff 自动格式化
	$(PYTHON) -m ruff format src/spineagent tests examples benchmarks

.PHONY: fmt-check
fmt-check: ## ruff 格式门:只检查不改写(格式漂移即红)
	$(PYTHON) -m ruff format --check src/spineagent tests examples benchmarks

# ---- demo --------------------------------------------------------------------------

.PHONY: demo
demo: ## 跑一键离线 demo(多 agent 顺序/并行 + 工具派发 + 隐私 trace,零网络)
	$(PYTHON) examples/quickstart.py

.PHONY: bench
bench: ## 跑编排开销基线(工具循环 + Coordinator 并行/串行,零真实 API;基线见 benchmarks/BENCH.md)
	$(PYTHON) benchmarks/bench_orchestration.py

# ---- meta --------------------------------------------------------------------------

.PHONY: help
help: ## 列出可用目标
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
