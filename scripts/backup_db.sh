#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# CortexMesh — production DB backup script (Track A).
#
# Streams a gzip'd pg_dump of the live Postgres container into
# backups/cortex_YYYYMMDDTHHMMSSZ.sql.gz and prunes everything older than
# the last 7 dumps.
#
# Idempotent. Safe to run from cron / systemd timer / make backup-db.
#
# Usage:
#   bash scripts/backup_db.sh
#   RETAIN=14 bash ...                         # keep more
#   COMPOSE_CMD='docker compose -p cortexmesh' bash ...
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

log()  { printf '\033[1;34m[backup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[backup][warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[backup][fatal]\033[0m %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

COMPOSE="${COMPOSE_CMD:-docker compose}"
RETAIN="${RETAIN:-7}"

# Minimum free space (MB) needed in the backups dir BEFORE dumping.
# A full cortexmesh pg_dump gzip'd ≈ 20 MB today; we keep 7 = 140 MB.
# Threshold is generous so daily cron doesn't pile up on a forgotten host.
MIN_DISK_FREE_MB="${MIN_DISK_FREE_MB:-512}"

command -v docker >/dev/null 2>&1 || die "docker not installed"
${COMPOSE} version >/dev/null 2>&1 || die "docker compose plugin missing"

# Load POSTGRES_PASSWORD if .env exists (compose also needs it for the db container).
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing in environment / .env}"

# Sanity: db container must be running, otherwise pg_dump will hang or fail.
if ! ${COMPOSE} ps --services --status running 2>/dev/null | grep -qx db; then
  die "db container is not running — start the stack first"
fi

mkdir -p backups

# Disk gate — bail before we fill the volume with a half-dump + rotation backlog.
if command -v df >/dev/null 2>&1; then
  AVAIL_KB="$(df -Pk backups 2>/dev/null | awk 'NR==2 {print $4}')"
  AVAIL_MB="$((AVAIL_KB / 1024))"
  if [ -z "${AVAIL_KB}" ] || [ "${AVAIL_KB}" -eq 0 ]; then
    warn "could not determine free space at backups/"
  elif [ "${AVAIL_MB}" -lt "${MIN_DISK_FREE_MB}" ]; then
    die "only ${AVAIL_MB} MB free at backups/ (need ≥ ${MIN_DISK_FREE_MB} MB). Prune old backups or expand volume."
  else
    log "disk OK: ${AVAIL_MB} MB free at backups/ (threshold ${MIN_DISK_FREE_MB} MB)"
  fi
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="backups/cortex_${TS}.sql.gz"

log "dumping → ${OUT}"
# -T  : no TTY (works under cron / systemd)
# -Fc would be nicer, but plain SQL is portable across major versions.
if ! ${COMPOSE} exec -T db \
      pg_dump -U cortexmesh --no-owner --no-acl cortexmesh \
    | gzip -9 > "${OUT}"; then
  rm -f "${OUT}"
  die "pg_dump failed — partial output removed"
fi

# Validate the archive is not empty / corrupt.
SIZE=$(stat -c%s "${OUT}" 2>/dev/null || stat -f%z "${OUT}")
if [ "${SIZE}" -lt 200 ]; then
  warn "${OUT} is suspiciously small (${SIZE} bytes) — inspect manually"
fi

log "wrote ${OUT} (${SIZE} bytes)"

# ─── prune old backups ──────────────────────────────────────────────────────
# Sort by mtime (newest last), keep the last ${RETAIN}, delete the rest.
# This matches backups/cortex_YYYYMMDDTHHMMSSZ.sql.gz naming exactly.
PRUNED=0
if [ -d backups ]; then
  while IFS= read -r f; do
    PRUNED=$(( PRUNED + 1 ))
    rm -f -- "${f}"
    warn "pruned old backup: ${f}"
  done < <(
    find backups -maxdepth 1 -type f -name 'cortex_*.sql.gz' \
      -printf '%T@ %p\n' 2>/dev/null \
      | sort -n \
      | head -n -"${RETAIN}" \
      | awk '{ $1=""; sub(/^ /,""); print }'
  )
fi

log "retain=${RETAIN}, pruned=${PRUNED}"
echo "${OUT}"