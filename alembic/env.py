"""Alembic env for CortexMesh.

Reads DSN from the same env vars the runtime uses, so migrations stay
in lockstep with the app. Supports `alembic upgrade head` and `downgrade -1`
both locally and inside the migrate Docker container.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# This is the Alembic Config object.
config = context.config

# Configure logging from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_dsn() -> str:
    """Read DSN from env, falling back to the local-dev default.

    Returns a SQLAlchemy URL that explicitly uses the psycopg (v3) driver
    so SQLAlchemy doesn't fall back to psycopg2 (which is not installed).
    """
    raw = ""
    for key in ("CORTEXMESH_DB_DSN", "CORTEXMESH_DATABASE_URL"):
        val = os.environ.get(key)
        if val:
            raw = val
            break
    if not raw:
        raw = "postgresql://cortexmesh:cortexmesh@127.0.0.1:5432/cortexmesh"
    # Normalise: strip any pre-existing driver tag, then force psycopg (v3).
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        if scheme.startswith("postgresql+"):
            scheme = "postgresql"
        return f"postgresql+psycopg://{rest}"
    return raw


config.set_main_option("sqlalchemy.url", _resolve_dsn())

# We don't use SQLAlchemy ORM models — schema is owned by raw SQL in
# the migration files. So target_metadata is None and autogenerate is
# intentionally not wired in (revisions are hand-authored and reviewed).
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL without a live connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # We don't track alembic_version ourselves — let Alembic manage it.
            version_table="alembic_version",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
