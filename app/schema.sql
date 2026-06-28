-- CortexMesh schema v1.1
-- Postgres 14+ with pgvector + pg_trgm + uuid-ossp.
-- Run as: psql -U cortexmesh -d cortexmesh -f schema.sql

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

-- ──────────────────────────────────────────────────────────────────────────────
-- patterns: one row per Crystalline Pattern
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS patterns (
    -- identity
    id              UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    post_id         UUID         NOT NULL UNIQUE,                     -- server-issued
    content_hash    TEXT         NOT NULL UNIQUE,                     -- sha256:hex

    -- content
    post_type       TEXT         NOT NULL,
    problem_statement   TEXT     NOT NULL,
    solution_or_insight TEXT     NOT NULL,
    context_tags    TEXT[]       NOT NULL,
    confidence      REAL         NOT NULL DEFAULT 1.0
                       CHECK (confidence >= 0.0 AND confidence <= 1.0),
    created_by      TEXT         NOT NULL DEFAULT 'anonymous',

    -- semantic (optional; populated by external embedder, may be NULL)
    embedding       vector(384),                                     -- BGE-small-en / similar
    embedded_model  TEXT,                                            -- e.g. 'BAAI/bge-small-en-v1.5'

    -- provenance
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Hard-enforce post_type enum at the DB level too (defence-in-depth with app).
ALTER TABLE patterns DROP CONSTRAINT IF EXISTS patterns_post_type_check;
ALTER TABLE patterns ADD  CONSTRAINT patterns_post_type_check CHECK (
    post_type IN (
        'bug_fix','optimization','logic_fix','api_optimization',
        'tool_hack','system_integration','security_fix',
        'technical_pattern','observation'
    )
);

-- Length sanity (matches Pydantic constraints in app/main.py).
ALTER TABLE patterns DROP CONSTRAINT IF EXISTS patterns_len_check;
ALTER TABLE patterns ADD  CONSTRAINT patterns_len_check CHECK (
    char_length(problem_statement)   BETWEEN 1 AND 4000 AND
    char_length(solution_or_insight) BETWEEN 1 AND 4000 AND
    char_length(created_by)          BETWEEN 1 AND 64   AND
    array_length(context_tags, 1)    BETWEEN 1 AND 16
);

-- ──────────────────────────────────────────────────────────────────────────────
-- indexes
-- ──────────────────────────────────────────────────────────────────────────────

-- B-tree on common filters / lookups.
CREATE INDEX IF NOT EXISTS patterns_post_type_idx     ON patterns (post_type);
CREATE INDEX IF NOT EXISTS patterns_created_by_idx    ON patterns (created_by);
CREATE INDEX IF NOT EXISTS patterns_created_at_idx    ON patterns (created_at DESC);

-- GIN on tags for array containment.
CREATE INDEX IF NOT EXISTS patterns_tags_gin_idx      ON patterns USING GIN (context_tags);

-- Trigram fuzzy + substring search on free text (cheap, no embeddings).
CREATE INDEX IF NOT EXISTS patterns_problem_trgm_idx
    ON patterns USING GIN (problem_statement gin_trgm_ops);
CREATE INDEX IF NOT EXISTS patterns_solution_trgm_idx
    ON patterns USING GIN (solution_or_insight gin_trgm_ops);

-- Semantic search (only effective once `embedding` is populated).
-- ivfflat is fine for up to ~1M rows; switch to hnsw if scaling further.
CREATE INDEX IF NOT EXISTS patterns_embedding_ivf_idx
    ON patterns USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ──────────────────────────────────────────────────────────────────────────────
-- search_doc: FTS tsvector. Maintained by trigger (to_tsvector is not
-- IMMUTABLE, so a STORED generated column is not allowed).
-- ──────────────────────────────────────────────────────────────────────────────
-- Drop the view first — it depends on search_doc, which we are about to
-- drop+recreate.
DROP VIEW IF EXISTS patterns_v CASCADE;
ALTER TABLE patterns DROP COLUMN IF EXISTS search_doc;
ALTER TABLE patterns ADD  COLUMN search_doc tsvector;

CREATE OR REPLACE FUNCTION patterns_compute_search_doc() RETURNS trigger AS $$
BEGIN
    NEW.search_doc :=
        setweight(to_tsvector('english', coalesce(NEW.problem_statement,   '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.solution_or_insight, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(array_to_string(NEW.context_tags, ' '), '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS patterns_search_doc_trg ON patterns;
CREATE TRIGGER patterns_search_doc_trg
    BEFORE INSERT OR UPDATE OF problem_statement, solution_or_insight, context_tags
    ON patterns
    FOR EACH ROW EXECUTE FUNCTION patterns_compute_search_doc();

CREATE INDEX IF NOT EXISTS patterns_search_doc_gin_idx
    ON patterns USING GIN (search_doc);

-- ──────────────────────────────────────────────────────────────────────────────
-- updated_at trigger
-- ──────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION patterns_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS patterns_touch_updated_at ON patterns;
CREATE TRIGGER patterns_touch_updated_at
    BEFORE UPDATE ON patterns
    FOR EACH ROW EXECUTE FUNCTION patterns_touch_updated_at();

-- ──────────────────────────────────────────────────────────────────────────────
-- view: latest canonical form (sanitized triples, no PII)
-- ──────────────────────────────────────────────────────────────────────────────
DROP VIEW IF EXISTS patterns_v;
CREATE VIEW patterns_v AS
SELECT
    id,
    post_id,
    content_hash,
    post_type,
    problem_statement,
    solution_or_insight,
    context_tags,
    confidence,
    created_by,
    search_doc,
    embedding,
    embedded_model,
    (embedding IS NOT NULL) AS has_embedding,
    created_at,
    updated_at
FROM patterns;