"""SQLAlchemy declarative base, portable column types and shared mixins.

The goal is that one set of models runs unmodified on PostgreSQL (production)
and SQLite (local development). The two places the databases genuinely differ —
native UUID, JSONB and CITEXT — are handled here by `TypeDecorator`s rather
than by branching in application code.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, String, TypeDecorator
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UUIDType(TypeDecorator[Any]):
    """Native `uuid` on PostgreSQL, 36-char text on SQLite."""

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            value = uuid.UUID(value)
        return value if dialect.name == "postgresql" else str(value)

    def process_result_value(self, value: Any, dialect: Any) -> uuid.UUID | None:
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class JSONType(TypeDecorator[Any]):
    """`JSONB` on PostgreSQL, `JSON` text elsewhere.

    Used for wide analytical payloads that are read whole and never queried
    (analysis reports, validation findings, AI decisions). Anything that needs
    an index or a `WHERE` clause gets real columns instead.
    """

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.JSONB())
        return dialect.type_descriptor(postgresql.JSON())

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        return value


class CitextType(TypeDecorator[str]):
    """Case-insensitive text. `CITEXT` on PostgreSQL, `NOCASE`-collated on SQLite."""

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.dialects.postgresql.CITEXT())
        return dialect.type_descriptor(String(255, collation="NOCASE"))


class TZDateTime(TypeDecorator[datetime]):
    """Timezone-aware timestamps that survive SQLite's naive storage."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""

    type_annotation_map = {dict[str, Any]: JSONType, list[Any]: JSONType}


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


__all__ = [
    "Base",
    "CitextType",
    "JSONType",
    "TimestampMixin",
    "TZDateTime",
    "UUIDType",
    "utcnow",
    "uuid_pk",
]
