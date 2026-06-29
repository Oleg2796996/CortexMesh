"""initial v1.1 schema (baseline)

Revision ID: 0001_initial_v1_1
Revises:
Create Date: 2026-06-28 10:55:00

This migration captures the v1.1 schema as it existed in app/schema.sql.
Future changes go into separate revision files; this one is the baseline
and must not be edited once shipped.
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_initial_v1_1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Extensions (idempotent in Postgres).
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # patterns table.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS patterns (
            id                  UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
            post_id             UUID         NOT NULL UNIQUE,
            content_hash        TEXT         NOT NULL UNIQUE,
            post_type           TEXT         NOT NULL,
            problem_statement   TEXT         NOT NULL,
            solution_or_insight TEXT         NOT NULL,
            context_tags        TEXT[]       NOT NULL,
            confidence          REAL         NOT NULL DEFAULT 1.0
                                   CHECK (confidence >= 0.0 AND confidence <= 1.0),
            created_by          TEXT         NOT NULL DEFAULT 'anonymous',
            embedding           vector(384),
            embedded_model      TEXT,
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        ALTER TABLE patterns DROP CONSTRAINT IF EXISTS patterns_post_type_check;
        ALTER TABLE patterns ADD  CONSTRAINT patterns_post_type_check CHECK (
            post_type IN (
                'bug_fix','optimization','logic_fix','api_optimization',
                'tool_hack','system_integration','security_fix',
                'technical_pattern','observation'
            )
        )
        """
    )

    op.execute(
        """
        ALTER TABLE patterns DROP CONSTRAINT IF EXISTS patterns_len_check;
        ALTER TABLE patterns ADD  CONSTRAINT patterns_len_check CHECK (
            char_length(problem_statement)   BETWEEN 1 AND 4000 AND
            char_length(solution_or_insight) BETWEEN 1 AND 4000 AND
            char_length(created_by)          BETWEEN 1 AND 64   AND
            array_length(context_tags, 1)    BETWEEN 1 AND 16
        )
        """
    )

    # indexes (use IF NOT EXISTS via raw SQL because op.create_index lacks
    # the conditional in older Alembic versions).
    op.execute("CREATE INDEX IF NOT EXISTS patterns_post_type_idx   ON patterns (post_type)")
    op.execute("CREATE INDEX IF NOT EXISTS patterns_created_by_idx  ON patterns (created_by)")
    op.execute("CREATE INDEX IF NOT EXISTS patterns_created_at_idx  ON patterns (created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS patterns_tags_gin_idx    ON patterns USING GIN (context_tags)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS patterns_problem_trgm_idx "
        "ON patterns USING GIN (problem_statement gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS patterns_solution_trgm_idx "
        "ON patterns USING GIN (solution_or_insight gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS patterns_embedding_ivf_idx "
        "ON patterns USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

    # search_doc + trigger.
    # CASCADE because the v patterns_v view depends on it (bootstrap only —
    # we re-create the view after re-adding the column).
    op.execute("ALTER TABLE patterns DROP COLUMN IF EXISTS search_doc CASCADE")
    op.execute("ALTER TABLE patterns ADD  COLUMN search_doc tsvector")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION patterns_compute_search_doc() RETURNS trigger AS $$
        BEGIN
            NEW.search_doc :=
                setweight(to_tsvector('english', coalesce(NEW.problem_statement,   '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.solution_or_insight, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(array_to_string(NEW.context_tags, ' '), '')), 'C');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    op.execute("DROP TRIGGER IF EXISTS patterns_search_doc_trg ON patterns")
    op.execute(
        "CREATE TRIGGER patterns_search_doc_trg "
        "BEFORE INSERT OR UPDATE OF problem_statement, solution_or_insight, context_tags "
        "ON patterns FOR EACH ROW EXECUTE FUNCTION patterns_compute_search_doc()"
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS patterns_search_doc_gin_idx "
        "ON patterns USING GIN (search_doc)"
    )

    # updated_at trigger.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION patterns_touch_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP TRIGGER IF EXISTS patterns_touch_updated_at ON patterns")
    op.execute(
        "CREATE TRIGGER patterns_touch_updated_at "
        "BEFORE UPDATE ON patterns FOR EACH ROW EXECUTE FUNCTION patterns_touch_updated_at()"
    )

    # patterns_v view.
    op.execute("DROP VIEW IF EXISTS patterns_v")
    op.execute(
        """
        CREATE VIEW patterns_v AS
        SELECT
            id, post_id, content_hash, post_type, problem_statement,
            solution_or_insight, context_tags, confidence, created_by,
            search_doc, embedding, embedded_model,
            (embedding IS NOT NULL) AS has_embedding,
            created_at, updated_at
        FROM patterns
        """
    )


def downgrade() -> None:
    # Drop view first (depends on table + columns).
    op.execute("DROP VIEW IF EXISTS patterns_v")
    # Triggers.
    op.execute("DROP TRIGGER IF EXISTS patterns_search_doc_trg ON patterns")
    op.execute("DROP TRIGGER IF EXISTS patterns_touch_updated_at ON patterns")
    op.execute("DROP FUNCTION IF EXISTS patterns_compute_search_doc()")
    op.execute("DROP FUNCTION IF EXISTS patterns_touch_updated_at()")
    # Drop column then table (cascades indexes + constraints).
    op.execute("ALTER TABLE patterns DROP COLUMN IF EXISTS search_doc")
    op.execute("DROP TABLE IF EXISTS patterns")
    # Note: extensions left in place — they may be used by other DBs.
