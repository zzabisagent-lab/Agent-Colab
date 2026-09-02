SHELL := /bin/bash
export PATH := $(HOME)/.local/bin:$(PATH)
UV ?= uv
PNPM ?= pnpm
export AGENT_COLAB_TEST_DATABASE_URL ?= postgresql://colab@127.0.0.1:54329/colab_test

.PHONY: bootstrap lint typecheck test test-db build check-docs secret-scan ci compose-up compose-down web-install

bootstrap: web-install
	$(UV) sync --all-extras

web-install:
	cd web-admin && $(PNPM) install --frozen-lockfile

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy
	$(UV) run bandit -q -c pyproject.toml -r server sidecar tools
	cd web-admin && $(PNPM) run lint && $(PNPM) exec tsc -b

typecheck:
	$(UV) run mypy

test:
	$(UV) run pytest

test-db:
	$(UV) run pytest -m db

build:
	$(UV) build
	cd web-admin && $(PNPM) run build

check-docs:
	$(UV) run python -m tools.trace_matrix --check
	$(UV) run python -m tools.criteria_lint
	$(UV) run python -m tools.phase_dag_lint
	$(UV) run python -m tools.plan_baseline_lint
	$(UV) run python -m tools.policy_lint
	$(UV) run python -m tools.threat_model_lint
	$(UV) run python -m tools.gen_event_schemas --check
	$(UV) run python -m tools.gen_event_fixtures --check

secret-scan:
	gitleaks git --no-banner --redact . || (echo "gitleaks not installed or findings present" && exit 1)

ci: lint test check-docs build

compose-up:
	docker compose up -d --wait

compose-down:
	docker compose down -v
