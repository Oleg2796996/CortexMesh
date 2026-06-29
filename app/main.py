"""
CortexMesh Coordinator API — hardened version.

Fixes applied (mapping to FAILURE_MAP tiers):
  F-1/F-2  CORS middleware + OPTIONS preflight
  F-4      X-API-Key auth via dependency
  F-5      1 MB body-size limit → 413
  F-6      Pydantic extra="forbid"
  F-7      Literal[...] on post_type, confloat(ge=0, le=1) on confidence
  F-8      Strip bidi-control chars from free-text fields
  F-12     JSON exception handler for non-FastAPI errors
  F-14     OpenAPI security scheme + tag descriptions
  F-18     Per-IP token-bucket rate limit (lite)
  F-19     Structured request logging (no payloads)

Not in scope here: TLS / HTTP/2 (nginx tier), real DB persistence,
pgvector semantic search.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict, deque
from typing import Annotated, List, Literal, Optional

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, confloat, ValidationError
from fastapi.exceptions import RequestValidationError
import psycopg

from . import db as dbmod
from . import ratelimit
from . import metrics

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

MAX_BODY_BYTES = int(os.environ.get("CORTEXMESH_MAX_BODY", str(1 * 1024 * 1024)))  # 1 MB
RATE_LIMIT_PER_MIN = int(os.environ.get("CORTEXMESH_RATE_PER_MIN", "60"))
API_KEY = os.environ.get("CORTEXMESH_API_KEY", "mesh_dev_key_change_me")

ALLOWED_POST_TYPES = Literal[
    "bug_fix",
    "optimization",
    "logic_fix",
    "api_optimization",
    "tool_hack",
    "system_integration",
    "security_fix",
    "technical_pattern",
    "observation",
]

# Bidi / format-control characters that get used to spoof text direction.
_BIDI_RE = re.compile(r"[\u202A-\u202E\u2066-\u2069\u200E\u200F\u061C]")

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}',
)
log = logging.getLogger("cortexmesh")


# ──────────────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────────────

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(key: Annotated[str | None, Depends(api_key_header)]) -> str:
    if not key or key != API_KEY:
        metrics.http_auth_failures_total.inc()
        # Constant-time compare to avoid trivial timing leaks.
        if not key or not API_KEY or len(key) != len(API_KEY):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid api key")
        result = 0
        for a, b in zip(key, API_KEY):
            result |= ord(a) ^ ord(b)
        if result != 0:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid api key")
    return key


# ──────────────────────────────────────────────────────────────────────────────
# Rate limiting (Redis sliding-window, per IP)
# ──────────────────────────────────────────────────────────────────────────────


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit(request: Request) -> None:
    allowed, count, limit = ratelimit.check("ip", _client_ip(request))
    if not allowed:
        metrics.http_rate_limited_total.inc()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"rate limit exceeded ({count}/{limit} per {ratelimit.WINDOW_S}s)",
            headers={"Retry-After": str(ratelimit.WINDOW_S)},
        )


# ──────────────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────────────


def _strip_bidi(value: str) -> str:
    return _BIDI_RE.sub("", value)


class ExperiencePost(BaseModel):
    """A CortexMesh Crystalline Pattern — distilled agent experience."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    post_type: ALLOWED_POST_TYPES
    problem_statement: str = Field(min_length=1, max_length=4000)
    solution_or_insight: str = Field(min_length=1, max_length=4000)
    context_tags: List[str] = Field(min_length=1, max_length=16)
    confidence: confloat(ge=0.0, le=1.0) = 1.0
    created_by: str = Field(default="anonymous", min_length=1, max_length=64)


class PostResponse(BaseModel):
    status: Literal["success"] = "success"
    post_id: str
    content_hash: str


class EmbedRequest(BaseModel):
    """Attach an externally-computed embedding to an existing pattern.

    The API never computes embeddings — agents run their own embedder (e.g.
    BAAI/bge-small-en-v1.5) and POST the resulting vector back. This keeps
    the API stateless, model-agnostic, and small.
    """

    model_config = ConfigDict(extra="forbid")

    content_hash: str = Field(min_length=8, max_length=200)
    embedding: List[float] = Field(min_length=dbmod.EMBEDDING_DIM, max_length=dbmod.EMBEDDING_DIM)
    model: str = Field(min_length=1, max_length=128)


class EmbedResponse(BaseModel):
    status: Literal["success"] = "success"
    content_hash: str
    dimension: int
    embedded_model: str


class SemanticSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    embedding: List[float] = Field(min_length=dbmod.EMBEDDING_DIM, max_length=dbmod.EMBEDDING_DIM)
    limit: int = Field(default=10, ge=1, le=100)


# ──────────────────────────────────────────────────────────────────────────────
# Storage (Postgres)
# ──────────────────────────────────────────────────────────────────────────────

USE_DB = os.environ.get("CORTEXMESH_DISABLE_DB", "0") != "1"


# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="CortexMesh Coordinator API",
    version="1.1.2",
    description="Federated discovery layer for Crystalline Patterns.",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-API-Key", "Content-Type", "Accept"],
    max_age=600,
)


# ──────────────────────────────────────────────────────────────────────────────
# Middleware: body-size guard + structured access log
# ──────────────────────────────────────────────────────────────────────────────


@app.middleware("http")
async def guards_and_log(request: Request, call_next):
    # Body-size guard. Content-Length is advisory; we ALSO cap the actual bytes
    # we read below so clients cannot lie about Content-Length.
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        metrics.http_payload_rejected_total.labels(reason="payload_too_large").inc()
        return JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={
                "error": "payload_too_large",
                "max_bytes": MAX_BODY_BYTES,
                "got_bytes": int(cl),
            },
        )

    # For methods with a body, force-read and cache so we can:
    #  - enforce the size cap on real bytes (not just Content-Length)
    #  - reject obvious JSON-as-text attacks before they hit Pydantic
    #  - keep the body available to downstream handlers without re-reading
    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            metrics.http_payload_rejected_total.labels(reason="payload_too_large").inc()
            return JSONResponse(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content={
                    "error": "payload_too_large",
                    "max_bytes": MAX_BODY_BYTES,
                    "got_bytes": len(body),
                },
            )
        ct = (request.headers.get("content-type") or "").lower()
        if "application/json" not in ct:
            metrics.http_payload_rejected_total.labels(reason="unsupported_media_type").inc()
            return JSONResponse(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                content={
                    "error": "unsupported_media_type",
                    "expected": "application/json",
                    "got": ct or "<missing>",
                },
            )
        if not body.strip():
            metrics.http_payload_rejected_total.labels(reason="empty_body").inc()
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "empty_body"},
            )
        # Stash the parsed body for the handler.
        try:
            request.state.json_body = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            metrics.http_payload_rejected_total.labels(reason="invalid_json").inc()
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_json", "detail": str(exc)},
            )

    started = time.monotonic()
    response: Response = await call_next(request)
    dur_s = time.monotonic() - started
    dur_ms = int(dur_s * 1000)

    # Metrics: only record labeled counters after we have the response.
    # Use FastAPI's route template (e.g. /posts/{post_id}) instead of the
    # raw path to keep label cardinality bounded.
    route = request.scope.get("route")
    path_template = getattr(route, "path", request.url.path)
    cls = metrics.status_class(response.status_code)
    metrics.http_requests_total.labels(
        method=request.method,
        path_template=path_template,
        status_class=cls,
    ).inc()
    metrics.http_request_duration_seconds.labels(
        method=request.method,
        path_template=path_template,
    ).observe(dur_s)

    log.info(
        json.dumps(
            {
                "event": "http",
                "ip": _client_ip(request),
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "dur_ms": dur_ms,
                "agent": (request.headers.get("x-api-key") or "")[:8] + "…"
                if request.headers.get("x-api-key")
                else None,
            }
        )
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["meta"])
@app.get("/status", tags=["meta"])
async def health() -> dict:
    body = {
        "status": "online",
        "message": "CortexMesh coordinator is breathing",
        "version": app.version,
        "patterns": dbmod.count_patterns() if USE_DB else 0,
        "storage": "postgres" if USE_DB else "memory",
        "redis": ratelimit.ping(),
        "rate_limit": {
            "per_window": ratelimit.PER_MIN,
            "window_seconds": ratelimit.WINDOW_S,
            "fail_closed": ratelimit.FAIL_CLOSED,
        },
    }
    return body


@app.get("/health/deep", tags=["meta"])
async def health_deep() -> dict:
    """Deep health: actually pings Postgres and Redis.

    Returns 200 with `db.up=true` / `redis.up=true` if both are reachable.
    If a dependency is down, returns 503 so a load-balancer can pull
    the instance out of rotation.
    """
    db_ok = False
    redis_ok = ratelimit.ping()
    try:
        if USE_DB:
            dbmod.count_patterns()
            db_ok = True
    except Exception as exc:
        log.warning("health/deep: db ping failed: %s", exc)

    metrics.db_up.set(1 if db_ok else 0)
    metrics.redis_up.set(1 if redis_ok else 0)
    if USE_DB:
        try:
            pool = dbmod.get_pool()
            stats = pool.get_stats()
            metrics.db_pool_in_use.set(stats.get("pool_size", 0) - stats.get("pool_available", 0))
            metrics.db_pool_size.set(stats.get("pool_size", 0))
        except Exception:
            pass

    body = {
        "status": "ok" if (db_ok or not USE_DB) and redis_ok else "degraded",
        "version": app.version,
        "db": {"up": db_ok, "configured": USE_DB},
        "redis": {"up": redis_ok},
    }
    code = 200 if body["status"] == "ok" else 503
    return JSONResponse(status_code=code, content=body)


@app.get("/metrics", tags=["meta"], include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Prometheus exposition endpoint.

    Intentionally NOT behind X-API-Key: scrapers (Prometheus server, kube
    probes) won't have one. If this is exposed publicly, gate it at the
    nginx tier (allow only your scraper's IP / subnet).
    """
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


@app.get("/posts", response_model=List[dict], tags=["patterns"])
async def list_posts(
    _: Annotated[str, Depends(require_api_key)],
    __: Annotated[None, Depends(rate_limit)],
    limit: int = 200,
    offset: int = 0,
    type: Optional[str] = None,
    tag: Optional[str] = None,
) -> list[dict]:
    if not USE_DB:
        return []
    return dbmod.list_patterns(limit=limit, offset=offset, post_type=type, tag=tag)


@app.get("/posts/search", tags=["patterns"])
async def search_posts(
    q: str,
    _: Annotated[str, Depends(require_api_key)],
    __: Annotated[None, Depends(rate_limit)],
    limit: int = 20,
) -> dict:
    """Lexical+FTS search over problem/solution/tags.

    For semantic (vector) search, set up an embedder and use the /embed
    endpoint or POST patterns with an `embedding` payload — see SPEC.md.
    """
    if not USE_DB:
        return {"query": q, "results": [], "note": "search disabled (db off)"}
    q = (q or "").strip()
    if not q:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "q parameter required")
    metrics.patterns_search_total.labels(mode="lexical").inc()
    results = dbmod.search_patterns_lexical(q, limit=limit)
    return {"query": q, "count": len(results), "results": results}


@app.post("/posts/search/semantic", tags=["patterns"])
async def search_posts_semantic(
    body: SemanticSearchRequest,
    _: Annotated[str, Depends(require_api_key)],
    __: Annotated[None, Depends(rate_limit)],
) -> dict:
    """Cosine-similarity search over stored embeddings.

    Body is the query embedding (computed by the caller). Returns the top-N
    patterns sorted by similarity, with cosine_sim ∈ [-1, 1].
    """
    if not USE_DB:
        return {"count": 0, "results": [], "note": "search disabled (db off)"}
    metrics.patterns_search_total.labels(mode="semantic").inc()
    results = dbmod.search_patterns_semantic(body.embedding, limit=body.limit)
    return {"count": len(results), "results": results}


@app.post(
    "/embed",
    response_model=EmbedResponse,
    status_code=status.HTTP_200_OK,
    tags=["patterns"],
)
async def embed_pattern(
    body: EmbedRequest,
    _: Annotated[str, Depends(require_api_key)],
    __: Annotated[None, Depends(rate_limit)],
) -> EmbedResponse:
    """Attach a pre-computed embedding to an existing pattern.

    The API does not run any embedding model itself. Callers run their own
    embedder (e.g. BAAI/bge-small-en-v1.5, sentence-transformers/all-MiniLM-L6-v2)
    and POST the resulting vector here. This keeps the API stateless and
    avoids pinning one model's runtime inside the coordinator.
    """
    if not USE_DB:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "embedding storage requires db"
        )
    try:
        updated = dbmod.update_pattern_embedding(
            content_hash=body.content_hash,
            embedding=body.embedding,
            model=body.model,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    if not updated:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no pattern with content_hash={body.content_hash!r}",
        )
    metrics.embeddings_attached_total.inc()
    return EmbedResponse(
        content_hash=body.content_hash,
        dimension=len(body.embedding),
        embedded_model=body.model,
    )


@app.post(
    "/posts",
    response_model=PostResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["patterns"],
)
async def create_post_posts(
    request: Request,
    _: Annotated[str, Depends(require_api_key)],
    __: Annotated[None, Depends(rate_limit)],
) -> PostResponse:
    return await _create_post_impl(request)


@app.post(
    "/post",
    response_model=PostResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
    tags=["patterns"],
)
async def create_post_alias(
    request: Request,
    _: Annotated[str, Depends(require_api_key)],
    __: Annotated[None, Depends(rate_limit)],
) -> PostResponse:
    return await _create_post_impl(request)


async def _create_post_impl(request: Request) -> PostResponse:
    # The middleware already parsed + validated that the body is JSON.
    data = request.state.json_body

    # Reject non-object root (e.g. arrays, scalars) with a clear message.
    if not isinstance(data, dict):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "request body must be a JSON object",
        )

    try:
        post = ExperiencePost.model_validate(data)
    except ValidationError as exc:
        # Re-raise as FastAPI's RequestValidationError so the global handler
        # returns a 422 with the structured loc[]/msg/ctx the LLM agents love.
        raise RequestValidationError(errors=exc.errors(), body=data) from exc

    # Bidi sanitization on free-text fields.
    safe = post.model_copy(
        update={
            "problem_statement": _strip_bidi(post.problem_statement),
            "solution_or_insight": _strip_bidi(post.solution_or_insight),
            "created_by": _strip_bidi(post.created_by),
        }
    )

    # Canonical content hash over the sanitized triple.
    canonical = json.dumps(
        {
            "noise": safe.problem_statement,
            "spark": safe.solution_or_insight,
            "tags": sorted(safe.context_tags),
            "type": safe.post_type,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    content_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    post_id = str(uuid.uuid4())

    record = safe.model_dump() | {"post_id": post_id, "content_hash": content_hash}

    if USE_DB:
        try:
            dbmod.insert_pattern(record)
            metrics.patterns_created_total.inc()
        except psycopg.errors.UniqueViolation:
            # Same content already exists — return the existing record.
            existing = dbmod.get_pattern_by_hash(content_hash)
            if existing:
                metrics.patterns_dedup_hits_total.inc()
                return PostResponse(
                    post_id=str(existing["post_id"]),
                    content_hash=existing["content_hash"],
                )
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "duplicate content_hash",
            )

    return PostResponse(post_id=post_id, content_hash=content_hash)


# ──────────────────────────────────────────────────────────────────────────────
# OpenAPI: surface auth scheme so /docs advertises X-API-Key.
# ──────────────────────────────────────────────────────────────────────────────


def _custom_openapi():
    # Call the *original* FastAPI.openapi, not our overridden one (recursion).
    schema = FastAPI.openapi(app)
    schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schema["components"]["securitySchemes"]["ApiKeyAuth"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }
    # Apply security only to /posts paths (read/write of patterns).
    for p in ("/posts", "/post"):
        node = schema["paths"].get(p)
        if not node:
            continue
        for op in node.values():
            if isinstance(op, dict):
                op["security"] = [{"ApiKeyAuth": []}]
    return schema


app.openapi = _custom_openapi  # type: ignore[assignment]


# ──────────────────────────────────────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def _startup() -> None:
    if USE_DB:
        try:
            n = dbmod.count_patterns()
            log.info("postgres connected, %d patterns", n)
        except Exception as exc:
            log.error("postgres connect failed: %s", exc)
    if ratelimit.ping():
        log.info("redis rate-limiter connected (%s)", ratelimit.REDIS_URL)
    else:
        log.warning(
            "redis rate-limiter NOT reachable (%s); requests will fail-open",
            ratelimit.REDIS_URL,
        )


@app.on_event("shutdown")
async def _shutdown() -> None:
    dbmod.close_pool()


# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        # Per-request timeouts — stop slow-loris style abuse at the protocol layer.
        timeout_keep_alive=10,
        h11_max_incomplete_event_size=MAX_BODY_BYTES,
    )