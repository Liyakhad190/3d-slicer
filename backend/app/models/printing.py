"""Printing, telemetry, failure prediction and the AI decision ledger."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin, TZDateTime, utcnow, uuid_pk


class PrintStatus(str, enum.Enum):
    queued = "queued"
    sent = "sent"
    printing = "printing"
    paused = "paused"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class PrintOutcome(str, enum.Enum):
    success = "success"
    partial_success = "partial_success"
    failure = "failure"


class Calibration(str, enum.Enum):
    """How a number was produced. Shown in the UI next to every AI figure."""

    heuristic = "heuristic"
    historical = "historical"
    uncalibrated = "uncalibrated"


class PrintJob(Base, TimestampMixin):
    __tablename__ = "print_jobs"
    __table_args__ = (Index("ix_print_job_printer", "printer_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slice_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("slice_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    printer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("printers.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[PrintStatus] = mapped_column(
        SAEnum(PrintStatus, native_enum=False, length=16),
        nullable=False,
        default=PrintStatus.queued,
        index=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    progress_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    estimated_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    material_used_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    operator_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class PrintHistory(Base):
    """The learning substrate.

    A row with `observed_by = 'inferred'` (e.g. a print that merely ran to
    completion) is weighted at 0.2 when informing a recommendation, versus 1.0
    for an operator-confirmed outcome.
    """

    __tablename__ = "print_history"
    __table_args__ = (
        Index("ix_history_lookup", "printer_id", "material_profile_id", "outcome"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    print_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("print_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    printer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), nullable=False
    )
    machine_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machine_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    material_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("material_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    nozzle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nozzles.id", ondelete="RESTRICT"), nullable=False
    )
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    outcome: Mapped[PrintOutcome] = mapped_column(
        SAEnum(PrintOutcome, native_enum=False, length=20),
        nullable=False,
        index=True,
    )
    failure_mode: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    quality_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    observed_by: Mapped[str] = mapped_column(String(16), nullable=False, default="operator")
    observed_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)


class TelemetrySample(Base):
    """Append-only, high volume, time-partitioned."""

    __tablename__ = "telemetry_samples"
    __table_args__ = (Index("ix_telemetry_job_time", "print_job_id", "at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    print_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("print_jobs.id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    nozzle_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    bed_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    chamber_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    axis_position: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    motor_state: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    layer: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    flow_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    firmware_error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    camera_fault: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)


class FailurePrediction(Base):
    """A risk assessment.

    `was_realised` is filled in later by telemetry/outcome recording, which is
    what makes these auditable rather than decorative.
    """

    __tablename__ = "failure_predictions"
    __table_args__ = (Index("ix_prediction_job", "slice_job_id", "failure_mode"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    slice_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("slice_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    print_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("print_jobs.id", ondelete="SET NULL"), nullable=True
    )
    failure_mode: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    probability: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    calibration: Mapped[Calibration] = mapped_column(
        SAEnum(Calibration, native_enum=False, length=16),
        nullable=False,
        default=Calibration.uncalibrated,
    )
    severity: Mapped[str] = mapped_column(String(12), nullable=False, default="warning")
    affected_region: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    evidence: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    mitigations: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    predicted_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)
    was_realised: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    outcome_recorded_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class AiDecision(Base):
    """Every AI-derived or AI-influenced decision. The explainability ledger.

    `reason` is NOT NULL and the schema CHECK keeps it non-empty: explainability
    is a storage requirement, not a prompt instruction.
    """

    __tablename__ = "ai_decisions"
    __table_args__ = (Index("ix_decision_job", "slice_job_id", "decision_type"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slice_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("slice_jobs.id", ondelete="CASCADE"), nullable=True
    )
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=True
    )
    decision_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    proposed_value: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    applied_value: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    calibration: Mapped[Calibration] = mapped_column(
        SAEnum(Calibration, native_enum=False, length=16),
        nullable=False,
        default=Calibration.uncalibrated,
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="llm")
    model_identifier: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    overridden_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    overridden_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)

    evidence: Mapped[list[AiDecisionEvidence]] = relationship(
        back_populates="decision", cascade="all, delete-orphan", lazy="selectin"
    )


class AiDecisionEvidence(Base):
    """A decision with no evidence row is rejected by the decision gate."""

    __tablename__ = "ai_decision_evidence"

    id: Mapped[uuid.UUID] = uuid_pk()
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    ref_type: Mapped[str] = mapped_column(String(40), nullable=False)
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    weight: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False, default=Decimal("1"))

    decision: Mapped[AiDecision] = relationship(back_populates="evidence")
