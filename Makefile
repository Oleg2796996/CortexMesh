# CortexMesh coordinator — convenience targets.
# All commands are idempotent and safe to re-run.

SHELL := /usr/bin/env bash

API_KEY ?= $(shell cat /tmp/cm_key 2>/dev/null || echo mesh_dev_key_change_me)
PORT    ?= 8001
TLS_PORT?= 8443
BASE    ?= http://127.0.0.1:$(PORT)
TLS_BASE?= https://127.0.0.1:$(TLS_PORT)

.PHONY: help venv install run stop logs health smoke smoke-v112 smoke-tls smoke-all schema migrate clean prod-build prod-up prod-down prod-logs deploy-prod rollback-prod backup-db logs-prod

help:
	@echo "CortexMesh v1.1.1 — make targets:"
	@echo "  make install       Install Python deps into .venv"
	@echo "  make schema        Apply app/schema.sql to local Postgres"
	@echo "  make run           Start uvicorn on 127.0.0.1:$(PORT) (foreground)"
	@echo "  make stop          Stop uvicorn on 127.0.0.1:$(PORT)"
	@echo "  make health        curl /health"
	@echo "  make smoke         35-scenario smoke against $(BASE)"
	@echo "  make smoke-v112    28-scenario smoke (Redis + embeddings)"
	@echo "  make smoke-tls     25-scenario TLS smoke against $(TLS_BASE)"
	@echo "  make smoke-all     All three smoke suites"
	@echo "  make prod-build    docker compose build"
	@echo "  make prod-up       docker compose up -d (TLS on :443)"
	@echo "  make prod-down     docker compose down"
	@echo "  make prod-logs     docker compose logs -f --tail=100"
	@echo "  --- prod (Track A scripts) ---"
	@echo "  make deploy-prod    bash scripts/deploy_prod.sh  (full safe deploy)"
	@echo "  make rollback-prod  bash scripts/rollback_prod.sh (down + best-effort fallback)"
	@echo "  make backup-db      bash scripts/backup_db.sh    (gzip + retain last 7)"
	@echo "  make logs-prod      docker compose logs -f --tail=100 api nginx db redis"

install:
	@if [ ! -d .venv ]; then python3 -m venv .venv; fi
	.venv/bin/pip install --upgrade pip wheel
	.venv/bin/pip install -r requirements.txt

schema:
	@PGPASSWORD=$${POSTGRES_PASSWORD:-cortexmesh} \
	psql -h 127.0.0.1 -U cortexmesh -d cortexmesh \
	     -v ON_ERROR_STOP=1 -f app/schema.sql

run:
	@if pgrep -f 'uvicorn.*--port $(PORT)' >/dev/null; then \
	  echo "uvicorn already running on :$(PORT)"; \
	else \
	  CORTEXMESH_API_KEY=$(API_KEY) \
	  .venv/bin/uvicorn app.main:app \
	    --host 127.0.0.1 --port $(PORT) --log-level info; \
	fi

stop:
	@-pkill -9 -f 'uvicorn.*--port $(PORT)' 2>/dev/null
	@sleep 1
	@pgrep -fa 'uvicorn.*--port $(PORT)' || echo "stopped"

health:
	@curl -sS $(BASE)/health | python3 -m json.tool

smoke:
	@API_KEY=$(API_KEY) bash /tmp/cm_smoke.sh

smoke-v112:
	@API_KEY=$(API_KEY) bash /tmp/cm_smoke_v112.sh

smoke-tls:
	@API_KEY=$(API_KEY) bash /tmp/cm_smoke_tls.sh

smoke-all: smoke smoke-v112 smoke-tls
	@echo "ALL SUITES GREEN"

prod-build:
	docker compose build

prod-up:
	docker compose up -d
	@echo "stack up — check https://$${CORTEXMESH_DOMAIN:-cortex.example.com}/health"

prod-down:
	docker compose down

prod-logs:
	docker compose logs -f --tail=100

# ─── Track A: production deploy / rollback / backup ─────────────────────────
# These call the scripts in ./scripts/ which do real preflight + healthcheck
# loops. They are idempotent — re-running will not corrupt the stack.

deploy-prod:
	@bash scripts/deploy_prod.sh

rollback-prod:
	@bash scripts/rollback_prod.sh

backup-db:
	@bash scripts/backup_db.sh

logs-prod:
	docker compose logs -f --tail=100 api nginx db redis

clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned"