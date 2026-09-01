# Agent Forge - developer & operator entrypoints.
# Every target is idempotent and safe to re-run.

SHELL := /bin/sh
.DEFAULT_GOAL := help
UV ?= uv
RUN := $(UV) run
COMPOSE_FILE := deploy/compose/docker-compose.yml
PROFILE ?= core
COMPOSE := docker compose -f $(COMPOSE_FILE) --profile $(PROFILE)

.PHONY: help install check lint fmt type test test-unit test-integration test-policies cov \
        up down logs ps restart evals evals-ci ingest repo-graph verify-ledger \
        new-instance new-connector sbom scan clean docs-check

help: ## Show this help
	@awk 'BEGIN{FS=":.*##"; printf "\nAgent Forge targets\n\n"} /^[a-zA-Z_-]+:.*?##/ {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""

# ------------------------------------------------------------------ toolchain
install: ## Create the dev virtualenv from uv.lock
	$(UV) sync --extra dev --extra databases
	$(RUN) pre-commit install

fmt: ## Auto-format and auto-fix
	$(RUN) ruff format src tests scripts
	$(RUN) ruff check --fix src tests scripts

lint: ## Lint (no writes)
	$(RUN) ruff check src tests scripts
	$(RUN) ruff format --check src tests scripts

type: ## Static types, strict
	$(RUN) mypy

test-unit: ## Unit tests only (no infrastructure needed)
	$(RUN) pytest tests/unit tests/policies -m "not integration and not e2e"

test-integration: ## Integration tests (needs `make up PROFILE=full`)
	$(RUN) pytest tests/integration -m integration

test-policies: ## Rego policy tests
	$(RUN) pytest tests/policies

test: ## Full pytest suite
	$(RUN) pytest

cov: ## Coverage gate: whole package plus core/, governance/, knowledge/
	$(RUN) pytest --cov --cov-report=term-missing --cov-report=xml
	$(RUN) python scripts/coverage_gate.py --min 80 --package agent_forge \
		--package agent_forge/core --package agent_forge/governance --package agent_forge/knowledge
	@echo "note: the real adapters (Redis, Qdrant, Postgres, NATS) are covered by"
	@echo "      tests/integration, which self-skips without Docker. Run 'make test-integration'."

check: lint type test-unit ## Fast pre-commit gate: lint + types + unit tests

docs-check: ## Verify every document required by the spec exists and is non-trivial
	$(RUN) python scripts/docs_check.py

# ------------------------------------------------------------------ compose
up: ## Start the stack: make up PROFILE=core|serving|knowledge|events|observability|maintenance|full
	$(COMPOSE) up -d --wait
	@echo "agent-api -> http://127.0.0.1:$${AGENT_API_PORT:-8080}/docs"

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down --remove-orphans

down-hard: ## Stop the stack and DELETE volumes (destructive)
	$(COMPOSE) down --remove-orphans --volumes

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Tail logs
	$(COMPOSE) logs -f --tail=100

restart: ## Restart agent-api only
	$(COMPOSE) restart agent-api

# ------------------------------------------------------------------ product
new-instance: ## make new-instance NAME=ventas TENANT=acme-mx
	$(RUN) python scripts/new_instance.py --name "$(NAME)" --tenant "$(TENANT)"

new-connector: ## make new-connector NAME=servicenow
	$(RUN) python scripts/new_connector.py --name "$(NAME)"

ingest: ## Trigger a knowledge sync for the active profile
	$(RUN) python scripts/ingest.py --profile "$${AGENT_FORGE_PROFILE:-configs/agent.profile.example.yaml}"

repo-graph: ## Regenerate docs/graphs/* and docs/REPO_MAP.md from the AST
	$(RUN) python scripts/repo_graph.py --root . --out docs/graphs --repo-map docs/REPO_MAP.md

verify-ledger: ## Validate the evidence hash chain
	$(RUN) python scripts/verify_ledger.py --path "$${LEDGER_PATH:-./var/ledger}"

seed: ## Load the demo corpus and demo tenant
	$(RUN) python scripts/seed.py

# ------------------------------------------------------------------ quality
evals: ## Run the evaluation harness locally
	$(RUN) python scripts/run_evals.py

evals-ci: ## Run evals with CI thresholds enforced
	$(RUN) python scripts/run_evals.py --enforce-thresholds

sbom: ## Generate a CycloneDX SBOM with syft
	syft dir:. -o cyclonedx-json=sbom.json

scan: ## Fail on CRITICAL vulnerabilities
	trivy fs --severity CRITICAL --exit-code 1 --scanners vuln,secret .

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov coverage.xml .coverage dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
