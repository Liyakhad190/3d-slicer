"""Slicing pipeline: jobs, stages, parameters, artifacts, validation, simulation."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum as SAEnum,
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


class JobStatus(str, enum.Enum):
    queued = "QUEUED"
    analyzing = "ANALYZING"
    repairing = "REPAIRING"
    optimizing = "OPTIMIZING"
    slicing = "SLICING"
    validating = "VALIDATING"
    simulating = "SIMULATING"
    completed = "COMPLETED"
    failed = "FAILED"
    blocked = "BLOCKED"
    cancelled = "CANCELLED"


class StageStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    blocked = "blocked"


STAGES: tuple[str, ...] = (
    "analyzing",
    "repairing",
    "optimizing",
    "slicing",
    "validating",
    "simulating",
)


class SlicerEngine(Base, TimestampMixin):
    """A registered slicing engine, reached only as a subprocess."""

    __tablename__ = "slicer_engines"

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    executable_path: Mapped[str] = mapped_column(String(512), nullable=False)
    engine_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    license: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    integration_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="subprocess")
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_probed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class SlicerProfile(Base, TimestampMixin):
    """A named, reusable settings set for one engine."""

    __tablename__ = "slicer_profiles"
    __table_args__ = (UniqueConstraint("engine_id", "key", "profile_version", name="uq_slicer_profile"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    engine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("slicer_engines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class SliceJob(Base, TimestampMixin):
    """One pipeline run.

    `input_fingerprint` hashes every input including profile *versions*, so a
    re-run with the same inputs is idempotent and a profile change produces a
    new fingerprint rather than silently reusing a stale result.
    """

    __tablename__ = "slice_jobs"
    __table_args__ = (
        Index("ix_slice_job_claim", "status", "priority", "queued_at"),
        Index("ix_slice_job_user", "user_id", "created_at"),
        Index("ix_slice_job_fingerprint", "input_fingerprint"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    printer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("printers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Exact versions used. Never nullable-by-convenience: a job must always be
    # attributable to the profile it actually ran against.
    machine_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machine_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    material_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("material_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    nozzle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nozzles.id", ondelete="RESTRICT"), nullable=False
    )

    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus, native_enum=False, length=16),
        nullable=False,
        default=JobStatus.queued,
        index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stage_progress: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Hash over every input, including profile versions, so a re-run with the
    #: same inputs is idempotent and a profile change cannot reuse a stale
    #: result. Empty until the pipeline resolves a configuration: the job row is
    #: created before any configuration exists, and a placeholder that pretends
    #: to be a fingerprint would be worse than an honest blank.
    input_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", index=True
    )

    queued_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    lease_owner: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    user: Mapped[Any] = relationship("User")
    stage_runs: Mapped[list[JobStageRun]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobStageRun.started_at"
    )


class JobStageRun(Base):
    """One attempt at one stage. The source of truth for progress reporting.

    Progress is *derived* from these rows, never fabricated by the client.
    """

    __tablename__ = "job_stage_runs"
    __table_args__ = (Index("ix_stage_job", "slice_job_id", "stage", "attempt"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    slice_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("slice_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[StageStatus] = mapped_column(
        SAEnum(StageStatus, native_enum=False, length=16),
        nullable=False,
        default=StageStatus.pending,
    )
    started_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_blob_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("blobs.id", ondelete="SET NULL"), nullable=True
    )

    job: Mapped[SliceJob] = relationship(back_populates="stage_runs")


class JobEvent(Base):
    """Append-only, high volume. Backs the SSE stream and the audit trail."""

    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_event_stream", "slice_job_id", "at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    slice_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("slice_jobs.id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    level: Mapped[str] = mapped_column(String(12), nullable=False, default="info")
    stage: Mapped[str | None] = mapped_column(String(24), nullable=True)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)


class SliceParameterSet(Base):
    __tablename__ = "slice_parameter_sets"

    id: Mapped[uuid.UUID] = uuid_pk()
    slice_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("slice_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="auto")
    objective: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    resolved_by: Mapped[str] = mapped_column(String(80), nullable=False, default="heuristics")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    rationale: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    engine_settings: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)

    parameters: Mapped[list[SliceParameter]] = relationship(
        back_populates="parameter_set", cascade="all, delete-orphan"
    )


class SliceParameter(Base):
    """One row per parameter so it can be individually locked and overridden.

    A locked parameter is a hard contract: the optimiser must not touch it.
    """

    __tablename__ = "slice_parameters"

    id: Mapped[uuid.UUID] = uuid_pk()
    parameter_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("slice_parameter_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="auto")
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locked_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    constraint: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)

    parameter_set: Mapped[SliceParameterSet] = relationship(back_populates="parameters")


class GeneratedFile(Base):
    """A produced artifact.

    `is_blocked` is the single guard: `GET /generated-files` refuses to serve a
    file whose validator rejected it, so unsafe output can never be downloaded.
    """

    __tablename__ = "generated_files"
    __table_args__ = (Index("ix_generated_job", "slice_job_id", "kind"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    slice_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("slice_jobs.id", ondelete="CASCADE"), nullable=False
    )
    blob_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_blocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)


class ValidationResult(Base):
    __tablename__ = "validation_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    slice_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("slice_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    validator: Mapped[str] = mapped_column(String(40), nullable=False)
    validator_version: Mapped[str] = mapped_column(String(20), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    blocks_output: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    severity_counts: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    findings: Mapped[list[ValidationFinding]] = relationship(
        back_populates="result", cascade="all, delete-orphan"
    )


class ValidationFinding(Base):
    __tablename__ = "validation_findings"
    __table_args__ = (Index("ix_finding_severity", "validation_result_id", "severity"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    validation_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("validation_results.id", ondelete="CASCADE"), nullable=False
    )
    severity: Mapped[str] = mapped_column(String(12), nullable=False)
    code: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    line_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    axis: Mapped[str | None] = mapped_column(String(8), nullable=True)
    measured_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    limit_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    mitigation: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="deterministic")

    result: Mapped[ValidationResult] = relationship(back_populates="findings")


class SimulationResult(Base):
    __tablename__ = "simulation_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    slice_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("slice_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    blob_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("blobs.id", ondelete="SET NULL"), nullable=True
    )
    renderer: Mapped[str] = mapped_column(String(40), nullable=False)
    render_version: Mapped[str] = mapped_column(String(20), nullable=False)
    estimated_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    filament_used_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    filament_used_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    layer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    travel_moves: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extrusion_moves: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retraction_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_changes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    warnings: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
