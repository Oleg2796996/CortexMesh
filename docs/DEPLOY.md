# CortexMesh — Production Deploy Guide (Track A)

This document walks through bringing CortexMesh v1.1.1 online on a fresh VPS.
It covers prerequisites, one-time host prep, secure secret material, the
actual deploy, monitoring, backup, rollback, and the most common pitfalls.

For the architecture itself see [`ARCHITECTURE.md`](../ARCHITECTURE.md).
For the API surface and endpoint contract see [`SPEC.md`](../SPEC.md).

---

## 1. Prerequisites

On the operator's laptop (the machine you run `make` from):

- GNU make
- GNU bash ≥ 4
- Docker Engine + Compose v2 (`docker compose version`)
- `openssl` (only if generating self-signed certs for a staging host)
- `git` (already have it — that's the repo you're in)
- `gh` CLI (optional, only used to verify the pushed commit after)

On the target VPS:

- Linux, kernel 4.x+ (Ubuntu 22.04 LTS / Debian 12 recommended)
- 2 vCPU / 2 GB RAM minimum (4 GB comfortable for pgvector + embeddings)
- 20 GB free disk for the Postgres volume + 7 rolling backups
- Outbound HTTPS for `docker pull` of base images
- Inbound **TCP 80** and **TCP 443** open in the firewall / cloud security group
- Inbound **TCP 22** open for the operator's IP (for ongoing ops)

CortexMesh listens on **443** and **80** (redirects to 443). Nothing else is
publicly exposed — db, redis, and the API itself sit on the internal
`cortexmesh` docker network only.

---

## 2. One-time VPS preparation

```bash
# 1. update and harden
apt update && apt -y upgrade
apt -y install ufw fail2ban unattended-upgrades

# 2. firewall
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable

# 3. install Docker Engine + Compose v2 in one shot (Debian/Ubuntu)
curl -fsSL https://get.docker.com | sh
usermod -aG docker $USER   # log out and back in for group to take effect
docker compose version     # should print v2.x
```

Verify with `docker compose ps` (should print the header with no error).

---

## 3. Clone and configure

```bash
# Replace with your fork / branch if you don't deploy from origin/main.
git clone https://github.com/Oleg2796996/CortexMesh.git /opt/cortexmesh
cd /opt/cortexmesh
git checkout main
git log -1 --oneline        # confirm what you're about to deploy
```

Create `.env` (never commit — `.env` is in `.gitignore`):

```dotenv
# Postgres password for user 'cortexmesh' / db 'cortexmesh'.
# Generate:  openssl rand -hex 32
POSTGRES_PASSWORD=replace_me_with_64_hex_chars

# Coordinator API key. Clients send it as X-API-Key header.
# Generate:  openssl rand -hex 32
CORTEXMESH_API_KEY=replace_me_with_64_hex_chars

# TLS cert + key. nginx expects them mounted at
#   /etc/ssl/cortexmesh/cert.pem
#   /etc/ssl/cortexmesh/key.pem
# inside the container, i.e. under ./secrets/ on the host.
TLS_CERT_PATH=/opt/cortexmesh/secrets/cert.pem
TLS_KEY_PATH=/opt/cortexmesh/secrets/key.pem
```

Create `secrets/` and drop the cert + key there (see §4 for self-signed):

```bash
mkdir -p secrets
chmod 700 secrets
# copy cert.pem and key.pem in via scp / rsync / ansible
chmod 644 secrets/cert.pem
chmod 600 secrets/key.pem
```

---

## 4. TLS certificate

**Option A — real cert from Let's Encrypt (production):**

```bash
apt -y install certbot
certbot certonly --standalone -d cortex.example.com
cp /etc/letsencrypt/live/cortex.example.com/fullchain.pem secrets/cert.pem
cp /etc/letsencrypt/live/cortex.example.com/privkey.pem   secrets/key.pem
```

Set up auto-renewal:

```bash
cat > /etc/cron.d/cortexmesh-certbot <<'CRON'
0 3 * * * root certbot renew --quiet --pre-hook 'cd /opt/cortexmesh && docker compose stop nginx' --post-hook 'cd /opt/cortexmesh && docker compose start nginx'
CRON
```

**Option B — self-signed (staging / smoke only):**

```bash
openssl req -x509 -nodes -newkey rsa:2048 \
  -keyout secrets/key.pem -out secrets/cert.pem \
  -days 365 -subj '/CN=cortex.local'
```

The `deploy_prod.sh` script will print a one-liner copy of the same command
if the cert is missing.

---

## 5. Deploy

```bash
make deploy-prod
```

This runs `scripts/deploy_prod.sh`, which:

1. **preflight** — verifies `.env`, all four secrets are set, and the
   cert + key files exist (warns and proceeds without TLS if missing).
2. **pull** — refreshes base images (`pgvector/pgvector:pg16`,
   `redis:7-alpine`, `nginx:1.27-alpine`).
3. **build** — builds the `cortexmesh-api` image from this repo's Dockerfile.
4. **db + redis + migrate** — brings the data tier up, waits up to 120s for
   the Postgres healthcheck, applies `schema.sql` (idempotent), then exits.
5. **api + nginx** — brings the coordinator and TLS edge online, waits up to
   120s for the api `/health` healthcheck inside the container.
6. **smoke** — hits `https://127.0.0.1:8443/healthz` (or
   `https://${PROD_HOST}:${TLS_PORT}/healthz` if `PROD_HOST` is exported)
   and `/openapi.json` through the TLS edge.

If anything fails, the script aborts before the next phase — your
previous stack state is preserved.

---

## 6. Verify

```bash
# Layer 1 — TLS edge (what clients see)
curl -fsSk https://cortex.example.com/healthz
# → {"status":"ok","version":"1.1.1","redis":true,"db":true}

# Layer 2 — API contract
curl -fsSk https://cortex.example.com/openapi.json | jq '.info.version'
# → "1.1.1"

# Layer 3 — authed call
curl -fsSk -H "X-API-Key: $CORTEXMESH_API_KEY" https://cortex.example.com/v1/agents

# Layer 4 — operator view
docker compose ps
make logs-prod
```

---

## 7. Monitoring & operations

| What you want          | Command                                                |
|------------------------|--------------------------------------------------------|
| Live status            | `docker compose ps`                                    |
| All logs (tail 100)    | `make logs-prod`                                       |
| Just the API           | `docker compose logs -f --tail=200 api`                |
| Postgres logs          | `docker compose logs -f --tail=200 db`                 |
| Disk usage             | `docker system df` and `du -sh /var/lib/docker/volumes/cortexmesh-pgdata` |
| Backup now             | `make backup-db`                                       |
| List backups           | `ls -lh backups/cortex_*.sql.gz`                       |
| Restore a backup       | `gunzip -c backups/cortex_YYYYMMDDTHHMMSSZ.sql.gz \| docker compose exec -T db psql -U cortexmesh -d cortexmesh -v ON_ERROR_STOP=1` |

Wire `make backup-db` to a cron job on the VPS for nightly backups at 03:30 UTC:

```cron
30 3 * * *  cd /opt/cortexmesh && /usr/bin/make backup-db >> /var/log/cortexmesh-backup.log 2>&1
```

---

## 8. Rollback

If v1.1.1 misbehaves on prod:

```bash
make rollback-prod
```

This will:

1. Snapshot current `docker compose ps`, `images`, and last 200 log lines to
   `backups/rollback_<ts>.log.{ps,images,logs}` for post-mortem.
2. Best-effort capture a fresh DB dump into `backups/`.
3. Bring the stack down (volumes preserved — DB survives).
4. Try to start the fallback image `cortexmesh-api:v1.1.0` if it exists
   locally. If it doesn't, the script prints the exact `docker pull` and
   `docker compose up` commands to run manually.

To force a clean rollback with a fresh DB volume (DESTRUCTIVE):

```bash
KEEP_VOLUMES=0 make rollback-prod
```

After rollback you can rebuild current code or pull a fixed image and
`make deploy-prod` again.

---

## 9. Troubleshooting

### `bind: address already in use` on :80 or :443

Something else on the host is already on those ports. Common culprits:
an Nginx/Apache system service, or a previous stack that didn't shut
down cleanly.

```bash
ss -ltnp | grep -E ':80|:443'
sudo systemctl stop nginx apache2     # if it's a system service
docker compose down                  # then re-run make deploy-prod
```

### `db did not become healthy within 120s`

Postgres never reported healthy. Typical causes:

- **Disk full** — Postgres can't write to its volume. `df -h /var/lib/docker`.
- **Wrong password** — `POSTGRES_PASSWORD` in `.env` was changed after the
  volume was created; the data on disk still has the old one. Either reset
  the volume (`docker compose down -v`) or set `.env` back to the old value.
- **Port 5432 already used on host** — unlikely here because db is on the
  internal network only, but check `ss -ltn | grep 5432` to be sure.

Look at the actual error:

```bash
docker compose logs db
docker compose logs migrate
```

### `api did not become healthy within 120s`

The container started but `/health` never returned 200. Almost always
it's missing env vars or the db/redis aren't actually healthy:

```bash
docker compose ps                       # are db & redis both healthy?
docker compose logs api | tail -80
docker exec -it cortexmesh-api env | grep CORTEXMESH
```

### `nginx: cannot load certificate`

The path in `.env` (`TLS_CERT_PATH` / `TLS_KEY_PATH`) points at a file
that doesn't exist, or it's not readable by the container user.

```bash
ls -l secrets/cert.pem secrets/key.pem
# If names differ, either rename them or update .env to the actual paths.
# The script will symlink/copy them to secrets/cert.pem & secrets/key.pem
# (which is what nginx.conf hard-codes).
```

### TLS smoke skipped at the end

If you saw `[deploy][warn] TLS cert not found at …` near the top of the
deploy script output, the stack came up but the smoke test was skipped.
Fix the cert path and re-run `make deploy-prod` — it's idempotent.

### Rate-limit returns 503 even though Redis is up

CortexMesh is conservative when Redis is unreachable. Check
`CORTEXMESH_RL_FAIL_CLOSED` in `docker-compose.yml` (default `"0"` =
fail-open). For a public-facing prod you may want `"1"`.

---

## 10. Security checklist before going public

- [ ] `POSTGRES_PASSWORD` is 32+ random bytes (not `change_me_*`).
- [ ] `CORTEXMESH_API_KEY` is 32+ random bytes (not `change_me_*`).
- [ ] `secrets/` is `chmod 700`, `key.pem` is `chmod 600`.
- [ ] `.env` is `chmod 600`, never committed (it's in `.gitignore`).
- [ ] SSH on the VPS is key-only (no password auth).
- [ ] UFW is on, only 22/80/443 open.
- [ ] fail2ban is running.
- [ ] unattended-upgrades is enabled.
- [ ] Let's Encrypt cert is auto-renewing (cron / systemd timer).
- [ ] `make backup-db` runs nightly, the latest dump is < 25h old.

---

## 11. Where the secrets actually go

A quick map for paranoia checks:

```
.env (host)                          docker-compose.yml reads
─────────────────                    ─────────────────────────
POSTGRES_PASSWORD  ────────────────►  db environment
                                     migrate environment
                                     api (via CORTEXMESH_DB_DSN)
CORTEXMESH_API_KEY  ───────────────►  api environment

secrets/cert.pem (host)  ─────────►  /etc/ssl/cortexmesh/cert.pem (nginx)
secrets/key.pem  (host)  ─────────►  /etc/ssl/cortexmesh/key.pem  (nginx)

docker/nginx.conf (host)  ────────►  /etc/nginx/conf.d/cortexmesh.conf
app/  (host)              ────────►  /app/app/                     (api image build)
```

Nothing else crosses the host/container boundary.