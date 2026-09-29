"""Content-addressed blob store record and the model asset graph."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin, TZDateTime, utcnow, uuid_pk


class Blob(Base):
    """Immutable, content-addressed file.

    `sha256` is both the identity and the storage key
    (`blobs/<first2>/<sha256>`). Paths are computed by the storage layer only;
    no caller may supply one.
    """

    __tablename__ = "blobs"
    __table_args__ = (UniqueConstraint("sha256", name="uq_blob_sha256"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False, default="application/octet-stream")
    detected_format: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    storage_path: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, index=True, default=utcnow
    )


class Model(Base, TimestampMixin):
    """The logical asset as the user sees it in a list.

    `current_version_id` is set after the first version is flushed, because the
    two tables reference each other.
    """

    __tablename__ = "models"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    versions: Mapped[list[ModelVersion]] = relationship(
        back_populates="model", cascade="all, delete-orphan", order_by="ModelVersion.version"
    )


class ModelVersion(Base, TimestampMixin):
    """An immutable snapshot of an uploaded model.

    `blob_id` always points at the **original** upload, which is never mutated.
    A repaired variant is a separate blob referenced by `repaired_blob_id`.
    """

    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("model_id", "version", name="uq_model_version"),
        Index("ix_model_version_sha", "sha256"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    # Verbatim for display only. NEVER used to construct a filesystem path.
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    sanitized_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    blob_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    repaired_blob_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("blobs.id", ondelete="SET NULL"), nullable=True
    )

    format: Mapped[str] = mapped_column(String(32), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    analysis_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    repair_status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_required")

    # Denormalised from the analysis so model lists render without loading JSON.
    triangle_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_z: Mapped[float | None] = mapped_column(Float, nullable=True)

    model: Mapped[Model] = relationship(back_populates="versions")
    analyses: Mapped[list[ModelAnalysis]] = relationship(
        back_populates="model_version", cascade="all, delete-orphan"
    )


class ModelAnalysis(Base):
    """Immutable analysis result, keyed by input hash so it can be cached."""

    __tablename__ = "model_analyses"
    __table_args__ = (
        Index("ix_analysis_cache", "model_version_id", "input_sha256"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    engine: Mapped[str] = mapped_column(String(40), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(40), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    printability_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    findings_count_by_severity: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, index=True, default=utcnow
    )

    model_version: Mapped[ModelVersion] = relationship(back_populates="analyses")


class MeshRepair(Base):
    """A repair *proposal*. The result is a new blob; the original is untouched."""

    __tablename__ = "mesh_repairs"

    id: Mapped[uuid.UUID] = uuid_pk()
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    engine: Mapped[str] = mapped_column(String(40), nullable=False)
    from_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    to_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operations: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    source_blob_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    result_blob_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("blobs.id", ondelete="SET NULL"), nullable=True
    )
    user_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, index=True, default=utcnow
    )


class Orientation(Base):
    """One scored orientation candidate. Exactly one may be `selected`."""

    __tablename__ = "orientations"
    __table_args__ = (
        Index("ix_orientation_selected", "model_version_id", "selected"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    strategy: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    objective: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    candidate_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    euler_xyz_deg: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    quaternion_xyzw: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False, default=Decimal("0"))
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, index=True, default=utcnow
    )
