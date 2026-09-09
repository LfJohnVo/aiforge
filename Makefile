# Agent Forge - developer & operator entrypoints.
# Every target is idempotent and safe to re-run.

SHELL := /bin/sh
.DEFAULT_GOAL := help
UV ?= uv
RUN := $(UV) run
COMPOSE_FILE := deploy/compose/docker-compose.yml
PROFILE ?= core
# Compose resuelve `.env` junto al fichero compose (deploy/compose/), no en el
# directorio desde el que corres el comando. Sin esto, el `.env` de la raiz --que es
# donde `.env.example` y el HANDOFF dicen crearlo-- no lo lee nadie y `make up` falla
# pidiendo una variable que si esta puesta.
ENV_FILE := $(if $(wildcard .env),--env-file .env,)
COMPOSE := docker compose -f $(COMPOSE_FILE) $(ENV_FILE) --profile $(PROFILE)

.PHONY: help install check lint fmt type test test-unit test-integration test-policies cov \
        up down logs ps restart evals evals-ci ingest repo-graph verify-ledger \
        new-instance new-connector sbom sbom-image scan scan-image clean docs-check

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
# SHARED=1 joins the base stack's infrastructure instead of cloning it: one container
# per extra cell instead of five. The isolation is then the tenant_id + instance
# namespacing every key already carries, which is what that namespacing is for.
new-instance: ## make new-instance NAME=ventas TENANT=acme-mx [SHARED=1]
	$(RUN) python scripts/new_instance.py --name "$(NAME)" --tenant "$(TENANT)" \
		$(if $(SHARED),--shared,)

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

# The exclusions are not cosmetic: `.venv` is 1.6 GB of packages `uv.lock` already
# pins, and without them syft walks it for over ten minutes to rediscover the same
# dependency set. Measured, not assumed.
sbom: ## Generate a CycloneDX SBOM of the source and the lockfile
	syft dir:. -o cyclonedx-json=sbom.json \
		--exclude './.venv' --exclude './node_modules' --exclude './.git' \
		--exclude './**/__pycache__' --exclude './var' --exclude './instances'

sbom-image: ## SBOM of the shipped image, which is what a customer actually runs
	docker build -f deploy/compose/Dockerfile --target api -t agent-forge/api:sbom .
	syft agent-forge/api:sbom -o cyclonedx-json=sbom.image.json

# `--timeout 30m` and the skips are both load-bearing, and both were found by running
# it: with secret scanning on and `.git` included, trivy walks past its 5-minute
# default and dies with a context deadline, so the target failed for everyone.
scan: ## Fail on CRITICAL vulnerabilities or committed secrets
	trivy fs --severity CRITICAL --exit-code 1 --scanners vuln,secret \
		--skip-dirs .venv --skip-dirs node_modules --skip-dirs var --skip-dirs .git \
		--skip-dirs .ruff_cache --skip-dirs .mypy_cache --skip-dirs .pytest_cache \
		--timeout 30m .

scan-image: ## Scan the shipped image; CRITICAL blocks
	docker build -f deploy/compose/Dockerfile --target api -t agent-forge/api:scan .
	trivy image --severity CRITICAL --exit-code 1 --ignore-unfixed \
		--timeout 30m agent-forge/api:scan

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov coverage.xml .coverage dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

backup: ## Respalda Postgres, Qdrant y el ledger en var/backups
	$(RUN) python scripts/backup.py --out "$${BACKUP_DIR:-var/backups}"

backup-verify: ## Comprueba la cadena de evidencia sin escribir nada
	$(RUN) python scripts/backup.py --verify-only
