"""AI print preparation: one persisted row per attempt at a print plan.

The invariant this table exists to support is that *every* attempt is
reproducible after the fact. The language model proposes; nothing it proposes
reaches the slicer until the deterministic validators have passed it. So each
row stores the exact prompt, the raw model output, the plan that was parsed
from it, and the validation result that judged it — enough to reconstruct why
a plan was accepted or refused without re-running either side.

Attempts are never updated in place: a re-plan writes a new row with the next
`attempt` number. Nothing is edited retroactively, so a plan that later looks
wrong still reads exactly as it was produced.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TimestampMixin, uuid_pk


class PreparationStatus(str, enum.Enum):
    """Lifecycle of one attempt.

    `completed` means the attempt *ran to the end* and its plan passed every
    check. `rejected` also means it ran to the end, but validation still holds
    a FAILED check: the plan exists, is inspectable, and must not reach the
    slicer. `failed` is an error that prevented an attempt at all — no usable
    model output, no plan.
    """

    queued = "queued"
    analysing = "analysing"
    proposing = "proposing"
    validating = "validating"
    completed = "completed"
    rejected = "rejected"
    failed = "failed"


class PrintPreparationJob(Base, TimestampMixin):
    """One attempt at producing a validated AI print plan."""

    __tablename__ = "print_preparation_jobs"
    __table_args__ = (
        # History is always read for one user, newest first.
        Index("ix_preparation_job_user", "user_id", "created_at"),
        # Re-plan chains: every attempt for one version, in order.
        Index("ix_preparation_job_version", "model_version_id", "attempt"),
        # The content the plan was produced from, so a stale result is
        # recognisable rather than silently reused.
        Index("ix_preparation_job_sha", "input_sha256"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    printer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("printers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    material_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("material_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    #: 1 for the first attempt, incremented by every re-plan. Together with
    #: `model_version_id` it identifies one attempt in a chain.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[PreparationStatus] = mapped_column(
        SAEnum(PreparationStatus, native_enum=False, length=16),
        nullable=False,
        default=PreparationStatus.queued,
        index=True,
    )

    #: Exactly what was sent to the model. Prompt text only — no credentials,
    #: no connection details, nothing the model was not already shown.
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: The model's verbatim answer, unparsed. Null when the provider failed
    #: before returning anything.
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The plan as parsed and coerced (`AiPrintPlan.model_dump()`).
    plan: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    #: The full checklist result (`ValidationOutcome.to_dict()`).
    validation: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    #: What the deterministic side changed in the plan, and why. Separate from
    #: `validation` so the UI can show normalisations without re-deriving them.
    normalisations: Mapped[list[Any]] = mapped_column(
        JSONType, nullable=False, default=list
    )
    #: Why the attempt was refused, in one sentence. Null when it was not.
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Content hash of the model file the analysis ran against.
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: Identifiers only, never a key or a URL with credentials embedded.
    model_identifier: Mapped[str | None] = mapped_column(String(80), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    @property
    def passed(self) -> bool:
        """True only when the attempt finished and nothing failed validation."""
        if self.status is not PreparationStatus.completed:
            return False
        if "passed" in self.validation:
            return self.validation["passed"] is True
        # Fall back to the check counts for a payload written without the flag.
        summary = self.validation.get("summary") or {}
        return not summary.get("failed", 0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<PrintPreparationJob {self.id} attempt={self.attempt} "
            f"status={self.status.value if self.status else None}>"
        )


__all__ = ["PreparationStatus", "PrintPreparationJob"]
