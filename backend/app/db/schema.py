"""Schema bootstrap.

Alembic is the source of truth for schema changes. This module exists only for
the local SQLite convenience path, where running `alembic upgrade head` on every
fresh checkout is friction. It is never called in production, where migrations
are applied deliberately by a deploy step.
"""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.core.logging import get_logger
from app.db.base import Base
from app.db.session import get_engine

log = get_logger(__name__)


def import_all_models() -> None:
    """Import every model module so `Base.metadata` is complete."""
    import app.models  # noqa: F401  (imported for side effects)


def create_all_for_dev(engine: Engine | None = None) -> bool:
    """Create missing tables. Development and tests only.

    Returns True if anything was created. Never called in production: a deploy
    step must run `alembic upgrade head` so changes are reviewed, ordered and
    reversible.
    """
    import_all_models()
    engine = engine or get_engine()
    existing = set(inspect(engine).get_table_names())
    wanted = set(Base.metadata.tables)
    if wanted.issubset(existing):
        return False
    Base.metadata.create_all(engine, checkfirst=True)
    created = wanted - existing
    log.info("db.schema_created", tables=sorted(created), count=len(created))
    return True


def drop_all_for_tests(engine: Engine | None = None) -> None:
    """Drop and recreate everything. Test-only; not reachable from the API."""
    import_all_models()
    engine = engine or get_engine()
    Base.metadata.drop_all(engine, checkfirst=True)
    Base.metadata.create_all(engine, checkfirst=True)
