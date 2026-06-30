"""Postgres data layer for CortexMesh.

Uses psycopg 3 with a connection pool. All functions are sync; FastAPI's
threadpool runs sync deps without blocking async endpoints.

The layer is intentionally tiny: schema.sql owns the truth (enums,
constraints, generated columns, indexes). This file just translates
the Pydantic `ExperiencePost` model to SQL and back.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("cortexmesh.db")

# Read DSN from env. Two names are accepted for historical reasons:
#   - CORTEXMESH_DB_DSN        (docker-compose, prod convention)
#   - CORTEXMESH_DATABASE_URL  (early dev convention, kept for back-compat)
DB_DSN = (
    os.environ.get("CORTEXMESH_DB_DSN")
    or os.environ.get("CORTEXMESH_DATABASE_URL")
    or "postgresql://cortexmesh:cortexmesh_dev_password@127.0.0.1:5432/cortexmesh"
)

# Small pool — the API is per-IP rate-limited anyway, and an under-provisioned
# pool is healthier than a runaway one. Tune via env.
POOL_MIN = int(os.environ.get("CORTEXMESH_DB_POOL_MIN", "1"))
POOL_MAX = int(os.environ.get("CORTEXMESH_DB_POOL_MAX", "10"))
POOL_TIMEOUT = float(os.environ.get("CORTEXMESH_DB_POOL_TIMEOUT", "10"))

_pool: Optional[ConnectionPool] = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=DB_DSN,
            min_size=POOL_MIN,
            max_size=POOL_MAX,
            timeout=POOL_TIMEOUT,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
        log.info("postgres pool opened (min=%d, max=%d)", POOL_MIN, POOL_MAX)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Context-managed connection from the pool. Commits on clean exit,
    rolls back on exception, always returns the conn to the pool."""
    pool = get_pool()
    with pool.connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ──────────────────────────────────────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────────────────────────────────────


def insert_pattern(record: dict) -> dict:
    """Insert a sanitized pattern. Returns the stored row as a dict.

    Raises psycopg.errors.UniqueViolation on duplicate content_hash (caller
    should map to 409 Conflict).
    """
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO patterns (
                post_id, content_hash, post_type, problem_statement,
                solution_or_insight, context_tags, confidence, created_by
            )
            VALUES (
                %(post_id)s, %(content_hash)s, %(post_type)s, %(problem_statement)s,
                %(solution_or_insight)s, %(context_tags)s, %(confidence)s, %(created_by)s
            )
            RETURNING post_id, content_hash, post_type, problem_statement,
                      solution_or_insight, context_tags, confidence,
                      created_by, created_at, updated_at
            """,
            record,
        )
        row = cur.fetchone()
    assert row is not None
    return row


def list_patterns(
    limit: int = 200,
    offset: int = 0,
    post_type: Optional[str] = None,
    tag: Optional[str] = None,
    since_id: Optional[str] = None,
    since_ts: Optional[str] = None,
) -> list[dict]:
    """Return up to `limit` patterns, optionally filtered.

    `tag` is matched as array containment (`tag = ANY(context_tags)`).
    `since_id` returns posts with post_id > since_id (lexicographic UUID compare).
    `since_ts` returns posts with created_at > since_ts (ISO-8601 timestamp).
    Both are inclusive-exclusive cursors for incremental polling.
    """
    with connection() as conn, conn.cursor() as cur:
        where_clauses = ["(%(ptype)s::text IS NULL OR post_type = %(ptype)s)"]
        if tag:
            where_clauses.append("(%(tag)s::text = ANY(context_tags))")
        if since_id:
            where_clauses.append("(post_id > %(since_id)s::uuid)")
        if since_ts:
            where_clauses.append("(created_at > %(since_ts)s::timestamptz)")

        where_sql = " AND ".join(where_clauses)
        sql = f"""
            SELECT post_id, content_hash, post_type, problem_statement,
                   solution_or_insight, context_tags, confidence,
                   created_by, has_embedding, created_at, updated_at
            FROM patterns_v
            WHERE {where_sql}
            ORDER BY created_at DESC
            LIMIT %(limit)s OFFSET %(offset)s
        """
        params = {
            "ptype": post_type,
            "tag": tag,
            "since_id": since_id,
            "since_ts": since_ts,
            "limit": limit,
            "offset": offset,
        }
        cur.execute(sql, params)
        return list(cur.fetchall())


def get_pattern_by_hash(content_hash: str) -> Optional[dict]:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT post_id, content_hash, post_type, problem_statement,
                   solution_or_insight, context_tags, confidence,
                   created_by, has_embedding, created_at, updated_at
            FROM patterns_v WHERE content_hash = %(h)s
            """,
            {"h": content_hash},
        )
        return cur.fetchone()


def count_patterns() -> int:
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*)::bigint AS n FROM patterns")
        row = cur.fetchone()
        return int(row["n"]) if row else 0


# ──────────────────────────────────────────────────────────────────────────────
# Search
# ──────────────────────────────────────────────────────────────────────────────


def search_patterns_lexical(
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Hybrid lexical: FTS tsvector ranking + trigram similarity as tiebreaker.

    Falls back to ILIKE substring when the query is too short to be tokenized.
    """
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH q AS (SELECT plainto_tsquery('english', %(q)s) AS tsq)
            SELECT
                post_id, content_hash, post_type, problem_statement,
                solution_or_insight, context_tags, confidence,
                created_by, has_embedding, created_at, updated_at,
                ts_rank(search_doc, (SELECT tsq FROM q)) AS lex_rank,
                greatest(
                    similarity(problem_statement,   %(q)s),
                    similarity(solution_or_insight, %(q)s)
                ) AS tri_sim
            FROM patterns_v
            WHERE search_doc @@ (SELECT tsq FROM q)
               OR problem_statement   ILIKE '%%' || %(q)s || '%%'
               OR solution_or_insight ILIKE '%%' || %(q)s || '%%'
            ORDER BY
                (ts_rank(search_doc, (SELECT tsq FROM q)) * 2.0
                 + greatest(
                       similarity(problem_statement,   %(q)s),
                       similarity(solution_or_insight, %(q)s)
                   )) DESC,
                created_at DESC
            LIMIT %(limit)s
            """,
            {"q": query, "limit": limit},
        )
        return list(cur.fetchall())


def search_patterns_semantic(
    embedding: list[float],
    limit: int = 20,
) -> list[dict]:
    """Cosine-distance search over `embedding vector(384)`.

    Returns empty if no rows have embeddings populated (most deployments
    until someone wires an embedder in).
    """
    if not embedding:
        return []
    vec_str = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                post_id, content_hash, post_type, problem_statement,
                solution_or_insight, context_tags, confidence,
                created_by, has_embedding, created_at, updated_at,
                1 - (embedding <=> %(vec)s::vector) AS cosine_sim
            FROM patterns_v
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %(vec)s::vector
            LIMIT %(limit)s
            """,
            {"vec": vec_str, "limit": limit},
        )
        return list(cur.fetchall())


# ──────────────────────────────────────────────────────────────────────────────
# Embeddings
# ──────────────────────────────────────────────────────────────────────────────


EMBEDDING_DIM = 384


def update_pattern_embedding(
    content_hash: str,
    embedding: list[float],
    model: str,
) -> bool:
    """Attach an external-computed embedding to an existing pattern.

    Returns True if a row was updated, False if no pattern has that content_hash.
    """
    if len(embedding) != EMBEDDING_DIM:
        raise ValueError(
            f"embedding must be {EMBEDDING_DIM}-dim float vector, got {len(embedding)}"
        )
    vec_str = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE patterns
               SET embedding = %(vec)s::vector,
                   embedded_model = %(model)s
             WHERE content_hash = %(h)s
            """,
            {"vec": vec_str, "model": model, "h": content_hash},
        )
        return cur.rowcount > 0