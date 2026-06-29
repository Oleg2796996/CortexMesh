"""Prometheus metrics for CortexMesh.

Exposes a /metrics endpoint with HTTP, business, and dependency health
counters/histograms. All metrics use a custom registry so the default
Go-style process metrics (which prometheus_client pulls in automatically)
don't leak into the endpoint and bloat scrapes.

Convention:
  - Counters end in `_total` (Prometheus norm).
  - Histograms use seconds (norm) and `_seconds` suffix.
  - Labels are low-cardinality: method, path-template (not raw path),
    status_class (2xx/4xx/5xx), endpoint (post/embed/search/etc).
"""
from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)


REGISTRY = CollectorRegistry()


# HTTP layer
http_requests_total = Counter(
    "cortexmesh_http_requests_total",
    "Total HTTP requests handled by the API.",
    labelnames=("method", "path_template", "status_class"),
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "cortexmesh_http_request_duration_seconds",
    "End-to-end HTTP request latency in seconds.",
    labelnames=("method", "path_template"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

http_payload_rejected_total = Counter(
    "cortexmesh_http_payload_rejected_total",
    "Requests rejected before reaching a handler (body cap, bad CT, bad JSON).",
    labelnames=("reason",),
    registry=REGISTRY,
)

http_auth_failures_total = Counter(
    "cortexmesh_http_auth_failures_total",
    "Requests rejected with 401 (bad/missing X-API-Key).",
    registry=REGISTRY,
)

http_rate_limited_total = Counter(
    "cortexmesh_http_rate_limited_total",
    "Requests rejected with 429 (per-IP rate limit).",
    registry=REGISTRY,
)


# Business layer
patterns_created_total = Counter(
    "cortexmesh_patterns_created_total",
    "Patterns successfully inserted into Postgres.",
    registry=REGISTRY,
)

patterns_dedup_hits_total = Counter(
    "cortexmesh_patterns_dedup_hits_total",
    "Pattern inserts that resolved to an existing row via content_hash.",
    registry=REGISTRY,
)

patterns_search_total = Counter(
    "cortexmesh_patterns_search_total",
    "Search calls split by mode.",
    labelnames=("mode",),
    registry=REGISTRY,
)

embeddings_attached_total = Counter(
    "cortexmesh_embeddings_attached_total",
    "Embeddings successfully written via /embed.",
    registry=REGISTRY,
)


# Dependencies
db_pool_in_use = Gauge(
    "cortexmesh_db_pool_in_use",
    "Postgres pool connections currently checked out.",
    registry=REGISTRY,
)

db_pool_size = Gauge(
    "cortexmesh_db_pool_size",
    "Postgres pool total size (min..max).",
    registry=REGISTRY,
)

db_up = Gauge(
    "cortexmesh_db_up",
    "1 if last Postgres ping succeeded, 0 otherwise.",
    registry=REGISTRY,
)

redis_up = Gauge(
    "cortexmesh_redis_up",
    "1 if last Redis ping succeeded, 0 otherwise.",
    registry=REGISTRY,
)


def status_class(code: int) -> str:
    if code < 200:
        return "1xx"
    if code < 300:
        return "2xx"
    if code < 400:
        return "3xx"
    if code < 500:
        return "4xx"
    return "5xx"


def render() -> tuple[bytes, str]:
    """Render the current registry to the Prometheus exposition format."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
