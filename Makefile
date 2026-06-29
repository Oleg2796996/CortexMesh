# CortexMesh coordinator — convenience targets.
# All commands are idempotent and safe to re-run.

SHELL := /usr/bin/env bash

API_KEY ?= $(shell cat /tmp/cm_key 2>/dev/null || echo mesh_dev_key_change_me)
PORT    ?= 8001
TLS_PORT?= 8443
BASE    ?= http://127.0.0.1:$(PORT)
TLS_BASE?= https://127.0.0.1:$(TLS_PORT)

DSN ?= postgresql://cortexmesh:cortexmesh@127.0.0.1:5432/cortexmesh

.PHONY: help venv install run stop logs health metrics smoke smoke-v112 smoke-tls smoke-all \
        schema migrate-current migrate-up migrate-down migrate-new migrate-history \
        backup-db backup-list backup-restore \
        prod-build prod-up prod-down prod-logs deploy-prod rollback-prod logs-prod clean

help:
	@echo "CortexMesh v1.1.2 — make targets:"
	@echo "  --- local dev ---"
	@echo "  make install       Install Python deps into .venv"
	@echo "  make schema        Apply app/schema.sql (legacy / one-shot bootstrap)"
	@echo "  make run           Start uvicorn on 127.0.0.1:$(PORT) (foreground)"
	@echo "  make stop          Stop uvicorn on :$(PORT)"
	@echo "  make health        curl /health"
	@echo "  make metrics       curl /metrics (Prometheus exposition)"
	@echo "  --- smoke ---"
	@echo "  make smoke         35-scenario smoke against $(BASE)"
	@echo "  make smoke-v112    28-scenario smoke (Redis + embeddings)"
	@echo "  make smoke-tls     25-scenario TLS smoke against $(TLS_BASE)"
	@echo "  make smoke-all     All three smoke suites"
	@echo "  --- migrations (Alembic, v1.1.2+) ---"
	@echo "  make migrate-current    Show current DB revision"
	@echo "  make migrate-up         Apply all pending migrations"
	@echo "  make migrate-down       Roll back one migration"
	@echo "  make migrate-new msg=X  Create a new empty revision"
	@echo "  make migrate-history    Show full migration graph"
	@echo "  --- backup / restore ---"
	@echo "  make backup-db      pg_dump local DB to backups/ (gzip, retain last 7)"
	@echo "  make backup-list    List available backups"
	@echo "  make backup-restore FILE=backups/cortex_xxx.sql.gz"
	@echo "  --- prod (docker compose) ---"
	@echo "  make prod-build    docker compose build"
	@echo "  make prod-up       docker compose up -d (TLS on :443)"
	@echo "  make prod-down     docker compose down"
	@echo "  make prod-logs     docker compose logs -f --tail=100"
	@echo "  make deploy-prod   Run scripts/deploy_prod.sh on a remote host"
	@echo "  make rollback-prod Run scripts/rollback_prod.sh"

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

metrics:
	@curl -sS $(BASE)/metrics | head -50

smoke:
	@API_KEY=$(API_KEY) bash /tmp/cm_smoke.sh

smoke-v112:
	@API_KEY=$(API_KEY) bash /tmp/cm_smoke_v112.sh

smoke-tls:
	@API_KEY=$(API_KEY) bash /tmp/cm_smoke_tls.sh

smoke-all: smoke smoke-v112 smoke-tls
	@echo "ALL SUITES GREEN"

# ─── migrations ──────────────────────────────────────────────────────────────

migrate-current:
	@CORTEXMESH_DB_DSN=$(DSN) .venv/bin/alembic current

migrate-up:
	@CORTEXMESH_DB_DSN=$(DSN) .venv/bin/alembic upgrade head

migrate-down:
	@CORTEXMESH_DB_DSN=$(DSN) .venv/bin/alembic downgrade -1

migrate-history:
	@.venv/bin/alembic history --verbose

migrate-new:
	@if [ -z "$(msg)" ]; then echo "usage: make migrate-new msg='short description'"; exit 1; fi
	@CORTEXMESH_DB_DSN=$(DSN) .venv/bin/alembic revision -m "$(msg)"

# ─── backup / restore ───────────────────────────────────────────────────────

BACKUP_DIR := backups

backup-db:
	@mkdir -p $(BACKUP_DIR)
	@TS=$$(date -u +%Y%m%dT%H%M%SZ); \
	OUT=$(BACKUP_DIR)/cortex_$${TS}.sql.gz; \
	PGPASSWORD=$${POSTGRES_PASSWORD:-cortexmesh} \
	  pg_dump -h 127.0.0.1 -U cortexmesh -d cortexmesh \
	          --no-owner --no-privileges --clean --if-exists \
	  | gzip > $$OUT; \
	echo "wrote $$OUT ($$(stat -c %s $$OUT) bytes)"; \
	ls -1t $(BACKUP_DIR)/cortex_*.sql.gz | tail -n +8 | xargs -r rm -f; \
	echo "retained last 7 backups"

backup-list:
	@ls -lh $(BACKUP_DIR)/cortex_*.sql.gz 2>/dev/null || echo "(no backups)"

backup-restore:
	@if [ -z "$(FILE)" ]; then echo "usage: make backup-restore FILE=backups/cortex_xxx.sql.gz"; exit 1; fi
	@if [ ! -f "$(FILE)" ]; then echo "FILE=$(FILE) not found"; exit 1; fi
	@gunzip -c $(FILE) | PGPASSWORD=$${POSTGRES_PASSWORD:-cortexmesh} \
	  psql -h 127.0.0.1 -U cortexmesh -d cortexmesh -v ON_ERROR_STOP=1
	@echo "restored from $(FILE)"

# ─── prod ────────────────────────────────────────────────────────────────────

prod-build:
	docker compose build

prod-up:
	docker compose up -d
	@echo "stack up — check https://$${CORTEXMESH_DOMAIN:-cortex.example.com}/health"

prod-down:
	docker compose down

prod-logs:
	docker compose logs -f --tail=100

deploy-prod:
	@bash scripts/deploy_prod.sh

rollback-prod:
	@bash scripts/rollback_prod.sh

# Track A addition: per-service logs (api + nginx + db + redis)
logs-prod:
	docker compose logs -f --tail=100 api nginx db redis

clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned"
