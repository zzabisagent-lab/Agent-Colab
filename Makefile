SHELL := /bin/bash
export PATH := $(HOME)/.local/bin:$(PATH)
UV ?= uv
PNPM ?= pnpm
TEST_POSTGRES_PASSWORD ?= colab
TEST_POSTGRES_PORT ?= 54329
TEST_POSTGRES_CONTAINER ?= agent-colab-test-pg

.PHONY: bootstrap lint typecheck test test-db test-db-up test-db-down build check-docs secret-scan ci compose-up compose-down web-install

bootstrap: web-install
	$(UV) sync --all-extras

web-install:
	cd web-admin && $(PNPM) install --frozen-lockfile

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy
	$(UV) run bandit -q -c pyproject.toml -r server sidecar
	$(UV) run bandit -q -c pyproject.toml -ll -r tools
	cd web-admin && $(PNPM) run lint && $(PNPM) exec tsc -b

typecheck:
	$(UV) run mypy

test:
	$(UV) run pytest

test-db-up:
	docker rm -f $(TEST_POSTGRES_CONTAINER) >/dev/null 2>&1 || true
	docker run -d --name $(TEST_POSTGRES_CONTAINER) \
		-e POSTGRES_USER=colab \
		-e POSTGRES_PASSWORD=$(TEST_POSTGRES_PASSWORD) \
		-e POSTGRES_DB=colab_test \
		-p 127.0.0.1:$(TEST_POSTGRES_PORT):5432 \
		postgres:16.11-alpine >/dev/null
	@for i in {1..60}; do \
		docker exec $(TEST_POSTGRES_CONTAINER) pg_isready -U colab -d colab_test >/dev/null 2>&1 && exit 0; \
		sleep 1; \
	done; \
	echo "test PostgreSQL did not become ready" >&2; exit 1

test-db-down:
	docker rm -f $(TEST_POSTGRES_CONTAINER) >/dev/null 2>&1 || true

test-db: test-db-up
	AGENT_COLAB_TEST_DATABASE_URL=postgresql://colab:$(TEST_POSTGRES_PASSWORD)@127.0.0.1:$(TEST_POSTGRES_PORT)/colab_test $(UV) run pytest -m db

build:
	$(UV) build
	cd web-admin && $(PNPM) run build

check-docs:
	$(UV) run python -m tools.trace_matrix --check
	$(UV) run python -m tools.criteria_lint
	$(UV) run python -m tools.phase_dag_lint
	$(UV) run python -m tools.plan_baseline_lint
	$(UV) run python -m tools.policy_lint
	$(UV) run python -m tools.name_role_lint
	$(UV) run python -m tools.threat_model_lint
	$(UV) run python -m tools.gen_event_schemas --check
	$(UV) run python -m tools.gen_event_fixtures --check

secret-scan:
	gitleaks git --no-banner --redact . || (echo "gitleaks not installed or findings present" && exit 1)

ci: lint test check-docs build

COMPOSE ?= docker compose --env-file deploy/dev/compose.env

compose-up:
	$(COMPOSE) up -d --build --wait --wait-timeout 900

compose-down:
	$(COMPOSE) down -v --remove-orphans
