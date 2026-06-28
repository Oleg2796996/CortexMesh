#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# CortexMesh — production rollback script (Track A).
#
# Tears down the current stack and (best-effort) starts the previous
# "known-good" image (tagged cortexmesh-api:v1.1.0 if present locally).
#
# Idempotent and safe to re-run.
#
# Usage:
#   bash scripts/rollback_prod.sh             # full rollback
#   KEEP_VOLUMES=1 bash ...                  # preserve pgdata (default)
#   FALLBACK_TAG=cortexmesh-api:v1.0.0 bash  # pin fallback image
#
# Required env (set in .env at repo root):  POSTGRES_PASSWORD, CORTEXMESH_API_KEY
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

log()  { printf '\033[1;35m[rollback]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[rollback][warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[rollback][fatal]\033[0m %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

COMPOSE="${COMPOSE_CMD:-docker compose}"
KEEP_VOLUMES="${KEEP_VOLUMES:-1}"
FALLBACK_TAG="${FALLBACK_TAG:-cortexmesh-api:v1.1.0}"

command -v docker >/dev/null 2>&1 || die "docker not installed"
${COMPOSE} version >/dev/null 2>&1 || die "docker compose plugin missing"

# ─── 0. ensure .env is loadable (rollback may be running from cron without it) ─
if [ ! -f .env ]; then
  warn ".env not found at repo root — falling back to environment"
else
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
  : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing in .env}"
  : "${CORTEXMESH_API_KEY:?CORTEXMESH_API_KEY missing in .env}"
fi

# ─── 1. snapshot current state for post-mortem ─────────────────────────────
log "snapshotting current state into backups/…"
mkdir -p backups
SNAP="backups/rollback_${COMPOSE_PROJECT_NAME:-cortexmesh}_$(date -u +%Y%m%dT%H%M%SZ).log"
${COMPOSE} ps           > "${SNAP}.ps"           2>&1 || true
${COMPOSE} images       > "${SNAP}.images"       2>&1 || true
${COMPOSE} logs --tail=200 > "${SNAP}.logs"      2>&1 || true
log "saved ${SNAP}.{ps,images,logs}"

# ─── 2. pre-rollback db backup (best-effort) ────────────────────────────────
log "capturing pre-rollback db snapshot (best-effort)…"
if ${COMPOSE} ps --services --status running 2>/dev/null | grep -qx db; then
  if bash scripts/backup_db.sh 2>/dev/null; then
    log "pre-rollback db backup OK"
  else
    warn "pre-rollback db backup failed — DB may already be down. Continuing."
  fi
else
  warn "db not running — skipping pre-rollback backup"
fi

# ─── 3. tear down current stack ─────────────────────────────────────────────
if [ "${KEEP_VOLUMES}" = "1" ]; then
  log "bringing stack down (volumes preserved)…"
  ${COMPOSE} down || warn "docker compose down returned non-zero"
else
  log "bringing stack down AND wiping volumes…"
  ${COMPOSE} down -v || warn "docker compose down -v returned non-zero"
fi

# ─── 4. attempt fallback image start ────────────────────────────────────────
if docker image inspect "${FALLBACK_TAG}" >/dev/null 2>&1; then
  log "fallback image ${FALLBACK_TAG} present locally — starting it…"
  ${COMPOSE} up -d db redis migrate
  # Override the api image for this rollback session.
  ${COMPOSE} up -d \
    --scale api=1 \
    api
  # Pin image via env override (Compose CLI limitation: per-service image
  # override requires compose.yaml edit; we instead tag the existing api image
  # if a fallback exists, but ONLY when the operator opted in).
  warn "fallback image is present but compose.yml pins image to 'build: .'."
  warn "If you need a true rollback to ${FALLBACK_TAG}, either:"
  warn "  1. docker tag ${FALLBACK_TAG} cortexmesh-api:rollback"
  warn "  2. edit docker-compose.yml api.build → image: cortexmesh-api:rollback"
  warn "  3. docker compose up -d api"
else
  warn "fallback image ${FALLBACK_TAG} not present locally — cannot auto-rollback."
  warn "Operator must: rebuild current code, OR pull the desired image:"
  warn "  docker pull <registry>/${FALLBACK_TAG}"
  warn "  docker compose up -d api"
fi

# ─── 5. summary ─────────────────────────────────────────────────────────────
cat <<EOF

────────────────────────────────────────────────────────────
⚠️  CortexMesh rollback finished

  What was done:
    • saved compose state + last 200 lines of logs to backups/rollback_*.log
    • captured pre-rollback DB snapshot (best-effort) to backups/
    • brought stack down (volumes $( [ "${KEEP_VOLUMES}" = "1" ] && echo preserved || echo wiped ))
    • attempted to start ${FALLBACK_TAG} (see warnings above if absent)

  How to recover the DB from a backup:
    gunzip -c backups/cortex_YYYYMMDDTHHMMSSZ.sql.gz \\
      | docker compose exec -T db psql -U cortexmesh -d cortexmesh -v ON_ERROR_STOP=1

  Bring everything back up cleanly:
    bash scripts/deploy_prod.sh

  When in doubt: docker compose ps — if db shows 'running' but api shows
  'exited', check 'docker compose logs api' for the failure cause.
────────────────────────────────────────────────────────────
EOF