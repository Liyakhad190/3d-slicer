"""Alembic environment.

Supports both online (live connection) and offline (SQL script) modes, and
imports the full model registry so autogenerate sees the complete metadata.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the `app` package importable when alembic is invoked from the backend dir.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.schema import import_all_models  # noqa: E402

import_all_models()  # populates Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The application's DATABASE_URL wins over alembic.ini, so migrations and the
# app can never silently target different databases.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = Base.metadata


_CUSTOM_TYPES = {"UUIDType", "JSONType", "CitextType", "TZDateTime"}


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render our custom column types as bare names.

    Alembic's default emits `<defining module>.<TypeName>(...)`, which would
    write `app.db.base.UUIDType(length=36)` and make every migration file
    depend on the `app` package. The migration template imports these four
    types by name, so emit just the name.

    Returning False for anything else is essential: a string returned from this
    hook *replaces* Alembic's rendering entirely, so an unhandled type would be
    emitted without its import.
    """
    if type_ != "type":
        return False
    if type(obj).__name__ in _CUSTOM_TYPES and type(obj).__module__ == "app.db.base":
        return repr(obj)
    return False


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=settings.is_sqlite,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # SQLite cannot ALTER most things in place; batch mode rebuilds the
            # table instead, so one migration works on both backends.
            render_as_batch=connection.dialect.name == "sqlite",
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
