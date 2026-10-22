# Hivemind — root Makefile
#
# Delegates into the two packages:
#   app/  — Python container app (MCP server + loops); tooled with ruff / mypy / pytest in a venv
#   cli/  — Node/TS host CLI (hivemind); tooled via npm scripts (prettier/eslint/tsc/vitest)
#
# Targets: fmt lint typecheck test check
#   `check` runs the UNIT layer for both packages (integration tests that need Postgres are
#   skipped automatically) and must be green on a clean checkout.
#
# Everything below is self-bootstrapping: the Python venv and the CLI node_modules are created
# on first use, so a clean checkout can run `make check` directly.

SHELL := /bin/bash

APP_DIR   := app
CLI_DIR   := cli

APP_VENV  := $(APP_DIR)/.venv
PY        := $(APP_VENV)/bin/python
VENV_STAMP := $(APP_VENV)/.installed
CLI_STAMP  := $(CLI_DIR)/node_modules/.install-stamp

.DEFAULT_GOAL := check

.PHONY: help fmt lint typecheck test check \
        app-fmt app-lint app-typecheck app-test \
        cli-fmt cli-lint cli-typecheck cli-test \
        deps app-deps cli-deps clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

## ---------------------------------------------------------------------------
## Dependency bootstrap (idempotent, stamp-guarded)
## ---------------------------------------------------------------------------

deps: app-deps cli-deps ## Install dependencies for both packages

app-deps: $(VENV_STAMP) ## Create the app venv and install app + dev tooling

$(VENV_STAMP): $(APP_DIR)/pyproject.toml
	python3 -m venv $(APP_VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e "./$(APP_DIR)[dev]"
	touch $@

cli-deps: $(CLI_STAMP) ## Install CLI node_modules

$(CLI_STAMP): $(CLI_DIR)/package.json
	cd $(CLI_DIR) && npm install
	touch $@

## ---------------------------------------------------------------------------
## Aggregate targets (run across both packages)
## ---------------------------------------------------------------------------

fmt: app-fmt cli-fmt ## Auto-format both packages

lint: app-lint cli-lint ## Lint both packages

typecheck: app-typecheck cli-typecheck ## Type-check both packages

test: app-test cli-test ## Run the unit test layer for both packages

check: lint typecheck test ## Full verification (unit layer) — must be green on a clean checkout

## ---------------------------------------------------------------------------
## Python app (app/)
## ---------------------------------------------------------------------------

app-fmt: app-deps
	$(PY) -m ruff format $(APP_DIR)
	$(PY) -m ruff check --fix $(APP_DIR)

app-lint: app-deps
	$(PY) -m ruff format --check $(APP_DIR)
	$(PY) -m ruff check $(APP_DIR)

app-typecheck: app-deps
	$(PY) -m mypy $(APP_DIR) --config-file $(APP_DIR)/pyproject.toml

# Unit layer only: integration tests are marked `integration` and are excluded here.
app-test: app-deps
	$(PY) -m pytest $(APP_DIR) -m "not integration"

## ---------------------------------------------------------------------------
## Node CLI (cli/)
## ---------------------------------------------------------------------------

cli-fmt: cli-deps
	cd $(CLI_DIR) && npm run fmt

cli-lint: cli-deps
	cd $(CLI_DIR) && npm run lint

cli-typecheck: cli-deps
	cd $(CLI_DIR) && npm run typecheck

cli-test: cli-deps
	cd $(CLI_DIR) && npm test

## ---------------------------------------------------------------------------
## Housekeeping
## ---------------------------------------------------------------------------

clean: ## Remove generated venv / node_modules / caches
	rm -rf $(APP_VENV)
	rm -rf $(CLI_DIR)/node_modules
	find $(APP_DIR) -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(APP_DIR)/.pytest_cache $(APP_DIR)/.ruff_cache $(APP_DIR)/.mypy_cache
