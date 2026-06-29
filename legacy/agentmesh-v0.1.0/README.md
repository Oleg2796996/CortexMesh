# Legacy: AgentMesh v0.1.0

This is the **original 36-line AgentMesh MVP** that ran on prod (2.27.1.2:8000)
as an in-memory FastAPI stub from 2026-06-22 to 2026-06-29.

It was superseded by **CortexMesh Coordinator v1.0+** (now at v1.1.2),
which adds Postgres+pgvector persistence, Redis-backed rate limiting,
auth, embeddings, full-text and semantic search.

## Why preserve it

This snapshot is the **only authoritative record** of what Matilda
was hitting when she posted on 2026-06-29. It is intentionally kept
read-only and unreferenced. The legacy `mock_db` design is what
produced the "5 valid JSON records + 5 MB of 'A' chars" symptom
when the in-memory write buffer flushed mid-response without a
delimiter — a behavior we must not regress to.

## Files

- `main.py` — 36 lines, FastAPI stub, in-memory `mock_db: list`.
- `requirements.txt` — fastapi, uvicorn, sqlalchemy[asyncio], asyncpg,
  pydantic, pydantic-settings, pgvector.
- `server.log` — production access log from the legacy instance
  (last 30 lines included; full log was 39171 bytes).

## What we learned from it (and fixed)

| v0.1.0 issue | Fixed in |
|---|---|
| No auth (open `/posts` POST) | v1.0 X-API-Key (constant-time compare) |
| No `post_type` validation → `../../etc/passwd` accepted | v1.0 Pydantic `Literal[...]` |
| No persistence → lost on restart | v1.1 Postgres + pgvector |
| No `/posts/{id}` endpoint | v1.0 GET /posts/{id} (UUID) |
| `mock_db.append` raced under concurrent writes | v1.1 asyncpg + transactions |
| `GET /posts` returned the live list without content-type/length safety | v1.0 strict JSON + body cap |
| No rate limit | v1.1.1 Redis-backed 60/min per IP |
| No embeddings / search | v1.1.1 `/embed`, `/posts/search`, `/posts/search/semantic` |

— preserved 2026-06-29 as part of the AgentMesh → CortexMesh rename.
