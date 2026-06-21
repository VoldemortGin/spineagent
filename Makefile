# spineagent —— 一键开发 / CI 命令(始终从包根跑)。
#
# `make` 或 `make help` 列出全部目标。默认走包内 venv;可覆盖解释器:
#   make test PYTHON=python3.12
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
ci: lint test ## 本地 CI 门:lint + 测试(与 CI 同一套门)

.PHONY: test
test: ## 跑测试套件(离线、确定性)
	$(PYTHON) -m pytest -q

.PHONY: lint
lint: ## ruff 静态检查(风格 + import 序 + 死代码)
	$(PYTHON) -m ruff check src/spineagent tests examples

.PHONY: fmt
fmt: ## ruff 自动格式化
	$(PYTHON) -m ruff format src/spineagent tests examples

# ---- demo --------------------------------------------------------------------------

.PHONY: demo
demo: ## 跑一键离线 demo(多 agent 顺序/并行 + 工具派发 + 隐私 trace,零网络)
	$(PYTHON) examples/quickstart.py

# ---- meta --------------------------------------------------------------------------

.PHONY: help
help: ## 列出可用目标
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
