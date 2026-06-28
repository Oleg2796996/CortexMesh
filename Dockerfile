# CortexMesh API — coordinator image (v1.1.1).
# Multi-stage build: builder installs deps into a venv, runtime copies the venv.

# ─── stage 1: builder ────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# System deps needed to compile psycopg's optional extras. The runtime stage
# does NOT need libpq — psycopg[binary] ships its own libpq inside the wheel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install deps into a venv that we copy to runtime in the second stage.
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip wheel \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ─── stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Create non-root user for the API process (defence-in-depth).
RUN groupadd --system cortexmesh \
 && useradd  --system --gid cortexmesh --create-home --shell /usr/sbin/nologin cortexmesh

COPY --from=builder /opt/venv /opt/venv
COPY app/ /app/app/

# Drop privileges. uvicorn does NOT need root.
USER cortexmesh

ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CORTEXMESH_USE_DB=1 \
    CORTEXMESH_API_KEY=mesh_change_me_in_prod \
    CORTEXMESH_DB_DSN=postgresql://cortexmesh:cortexmesh@db:5432/cortexmesh \
    CORTEXMESH_REDIS_URL=redis://redis:6379/0

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# Two workers give us a tiny bit of parallelism without doubling memory.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]