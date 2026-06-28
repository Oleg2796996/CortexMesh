#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# CortexMesh — production deploy script (Track A).
#
# Brings the full CortexMesh stack up in order:
#   1. preflight      — verify .env and TLS secrets are present
#   2. pull/build     — refresh base images and rebuild the API image
#   3. db + migrate   — bring Postgres up and apply schema.sql idempotently
#   4. api + nginx    — bring the coordinator and TLS edge online
#   5. smoke          — verify /healthz and /openapi.json through the edge
#
# Idempotent: re-running will re-apply schema (no-op), re-create containers
# that already exist with current config, and re-run the smoke check.
#
# Usage:
#   bash scripts/deploy_prod.sh                # full deploy
#   PROD_HOST=cortex.example.com bash ...      # override smoke host
#   TLS_PORT=8443 bash ...                     # local stack smoke on 8443
#
# Required env (set in .env at repo root):
#   POSTGRES_PASSWORD        Postgres user 'cortexmesh' password
#   CORTEXMESH_API_KEY       Coordinator API key (32+ random chars)
#   TLS_CERT_PATH            absolute path to TLS cert (PEM)
#   TLS_KEY_PATH             absolute path to TLS key  (PEM)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ─── helpers ────────────────────────────────────────────────────────────────
log()  { printf '\033[1;36m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy][warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[deploy][fatal]\033[0m %s\n' "$*" >&2; exit 1; }

# Repo root = parent of this script's directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Smoke target — defaults to local nginx TLS on 8443, or override via env.
PROD_HOST="${PROD_HOST:-127.0.0.1}"
TLS_PORT="${TLS_PORT:-8443}"
SMOKE_URL="https://${PROD_HOST}:${TLS_PORT}"

# Tunables.
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"   # seconds to wait for db / api
COMPOSE="${COMPOSE_CMD:-docker compose}"

# Minimum free space (MB) on the filesystem backing REPO_ROOT.
# First docker build + pull = ~700 MB; pgvector WAL grows ~50 MB / day.
MIN_DISK_FREE_MB="${MIN_DISK_FREE_MB:-2048}"

# ─── 0. shell sanity ────────────────────────────────────────────────────────
[ "${BASH_VERSINFO[0]}" -ge 4 ] || die "needs bash ≥ 4"

# ─── 1. preflight: docker present ───────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "docker not installed"
${COMPOSE} version >/dev/null 2>&1 \
  || die "docker compose plugin missing (install docker-compose-plugin)"

# ─── 1b. preflight: disk space ──────────────────────────────────────────────
if command -v df >/dev/null 2>&1; then
  AVAIL_KB="$(df -Pk "${REPO_ROOT}" 2>/dev/null | awk 'NR==2 {print $4}')"
  AVAIL_MB="$((AVAIL_KB / 1024))"
  if [ -z "${AVAIL_KB}" ] || [ "${AVAIL_KB}" -eq 0 ]; then
    warn "could not determine free space at ${REPO_ROOT}"
  elif [ "${AVAIL_MB}" -lt "${MIN_DISK_FREE_MB}" ]; then
    die "only ${AVAIL_MB} MB free at ${REPO_ROOT} (need ≥ ${MIN_DISK_FREE_MB} MB). Free space (docker system prune? rotate logs? expand volume?) and retry."
  else
    log "disk OK: ${AVAIL_MB} MB free at ${REPO_ROOT} (threshold ${MIN_DISK_FREE_MB} MB)"
  fi
fi

# ─── 2. preflight: .env ─────────────────────────────────────────────────────
[ -f .env ] || die ".env not found at repo root. Copy .env.example and edit it."

# shellcheck disable=SC1091
set -a; . ./.env; set +a

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing in .env}"
: "${CORTEXMESH_API_KEY:?CORTEXMESH_API_KEY missing in .env}"
: "${TLS_CERT_PATH:?TLS_CERT_PATH missing in .env}"
: "${TLS_KEY_PATH:?TLS_KEY_PATH missing in .env}"

# Cheap sanity — fail loud if operator left the example placeholder.
case "${POSTGRES_PASSWORD}" in
  change_me*) die "POSTGRES_PASSWORD is still a placeholder — edit .env" ;;
esac
case "${CORTEXMESH_API_KEY}" in
  change_me*|mesh_dev_key*) die "CORTEXMESH_API_KEY is still a placeholder — edit .env" ;;
esac

# ─── 3. preflight: TLS secrets ──────────────────────────────────────────────
mkdir -p secrets
TLS_OK=1
if [ ! -s "${TLS_CERT_PATH}" ]; then
  warn "TLS cert not found at ${TLS_CERT_PATH}"
  warn "  nginx will fail to start. To generate a self-signed test cert:"
  warn "    openssl req -x509 -nodes -newkey rsa:2048 \\"
  warn "      -keyout secrets/key.pem -out secrets/cert.pem \\"
  warn "      -days 365 -subj '/CN=cortex.local'"
  TLS_OK=0
fi
if [ ! -s "${TLS_KEY_PATH}" ]; then
  warn "TLS key not found at ${TLS_KEY_PATH}"
  TLS_OK=0
fi
if [ "${TLS_OK}" -eq 0 ]; then
  warn "Continuing WITHOUT TLS — stack will start but nginx will be unhealthy."
  warn "Set TLS_CERT_PATH and TLS_KEY_PATH in .env, then re-run."
fi

# The docker-compose.yml mounts ./secrets → /etc/ssl/cortexmesh and nginx
# expects cert.pem + key.pem there. Sync the operator-provided paths so the
# container can actually find them (no-op if names already match).
case "${TLS_CERT_PATH}" in
  "${REPO_ROOT}/secrets/cert.pem") : ;;
  *) cp -f "${TLS_CERT_PATH}" secrets/cert.pem ;;
esac
case "${TLS_KEY_PATH}" in
  "${REPO_ROOT}/secrets/key.pem") : ;;
  *) cp -f "${TLS_KEY_PATH}"  secrets/key.pem  ;;
esac

# ─── 4. pull + build ────────────────────────────────────────────────────────
log "pulling base images (best-effort)…"
${COMPOSE} pull --ignore-pull-failures || warn "pull failed for at least one image — continuing"

log "building api image…"
${COMPOSE} build api

# ─── 5. db + redis + migrate ────────────────────────────────────────────────
log "starting db + redis…"
${COMPOSE} up -d db redis

log "waiting for db healthcheck (timeout ${HEALTH_TIMEOUT}s)…"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
until ${COMPOSE} ps --format json db 2>/dev/null \
      | grep -q '"Health":"healthy"' 2>/dev/null; do
  if [ "$(date +%s)" -ge "${deadline}" ]; then
    ${COMPOSE} ps db || true
    die "db did not become healthy within ${HEALTH_TIMEOUT}s"
  fi
  sleep 2
done
log "db is healthy"

log "running migrate…"
${COMPOSE} up --abort-on-container-exit --exit-code-from migrate migrate
log "migrate exited cleanly"

# ─── 6. api + nginx ─────────────────────────────────────────────────────────
log "starting api + nginx…"
${COMPOSE} up -d api nginx

log "waiting for api healthcheck (timeout ${HEALTH_TIMEOUT}s)…"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
until ${COMPOSE} ps --format json api 2>/dev/null \
      | grep -q '"Health":"healthy"' 2>/dev/null; do
  if [ "$(date +%s)" -ge "${deadline}" ]; then
    ${COMPOSE} ps api || true
    die "api did not become healthy within ${HEALTH_TIMEOUT}s"
  fi
  sleep 2
done
log "api is healthy"

# ─── 7. smoke through nginx ─────────────────────────────────────────────────
if [ "${TLS_OK}" -eq 1 ]; then
  log "smoke through ${SMOKE_URL}…"
  # /healthz returns 200 even unauthenticated; /openapi.json too.
  curl -fsSk --max-time 10 "${SMOKE_URL}/healthz"   >/dev/null \
    || die "smoke FAILED: ${SMOKE_URL}/healthz"
  curl -fsSk --max-time 10 "${SMOKE_URL}/openapi.json" >/dev/null \
    || die "smoke FAILED: ${SMOKE_URL}/openapi.json"
  log "smoke OK"
else
  warn "skipping TLS smoke — cert/key missing (see warnings above)"
fi

# ─── 8. summary ─────────────────────────────────────────────────────────────
cat <<EOF

────────────────────────────────────────────────────────────
✅ CortexMesh deploy complete

  Stack:    $( ${COMPOSE} ps --services | tr '\n' ' ' )
  Image:    $( ${COMPOSE} images api --format '{{.Repository}}:{{.Tag}}' 2>/dev/null || echo 'n/a' )
  DB vol:   cortexmesh-pgdata
  API key:  set in .env (CORTEXMESH_API_KEY)
  PG pwd:   set in .env (POSTGRES_PASSWORD)

  Smoke:    curl ${SMOKE_URL}/healthz
            curl -H 'X-API-Key: \$CORTEXMESH_API_KEY' ${SMOKE_URL}/v1/agents

  Logs:     make logs-prod       (or: ${COMPOSE} logs -f --tail=100 api nginx)
  Backup:   make backup-db       (writes backups/cortex_YYYYMMDDTHHMMSSZ.sql.gz)
  Rollback: make rollback-prod

  Tear down: ${COMPOSE} down          (keep volumes)
             ${COMPOSE} down -v       (also wipe pgdata — DESTRUCTIVE)
────────────────────────────────────────────────────────────
EOF