"""Request and response models for the AI endpoints.

Kept in their own module because `app/schemas/api.py` is the shared contract for
the rest of the platform, and the AI surface is additive.

The response shapes are the interesting part. They carry `validation`,
`normalisations` and `provider` so the frontend can always show the user what
the model actually said, how long it took, and what had to be corrected — rather
than presenting an opaque "AI settings" verdict.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AiHealthResponse(BaseModel):
    """Real probe results. Never a configuration echo."""

    model_config = ConfigDict(from_attributes=True)

    provider: str
    model: str
    available: bool
    status: str
    code: str | None = None
    message: str = ""
    latency_ms: int | None = None
    models_available: list[str] = Field(default_factory=list)
    #: What to do about it, if anything.
    hint: str = ""


class AiPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=1, max_length=2000)
    model_version_id: uuid.UUID | None = None
    printer_id: uuid.UUID | None = None
    material_profile_id: uuid.UUID | None = None
    #: Prior turns, most recent last. Bounded by the planner to the last few.
    conversation: list[dict[str, str]] = Field(
        default_factory=list, max_length=12
    )


class AiDecisionRef(BaseModel):
    """The ledger row a request produced, so the client can read it back."""

    id: str
    decision_type: str
    reason: str
    evidence_count: int
    #: True when the server had to bring a model value into range.
    normalised: bool = False


class AiPlanResponse(BaseModel):
    request: str
    provider: str
    model: str
    duration_ms: int
    attempts: int
    validation: dict[str, Any]
    context: dict[str, Any]
    #: The `ai_decisions` row for this plan. Present whenever planning
    #: succeeded, so a plan the user cannot trace does not exist.
    decision: dict[str, Any] | None = None
    decision_id: str | None = None


class AiRefineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The prior validated plan, so the model adjusts rather than restarts.
    previous_plan: dict[str, Any]
    instruction: str = Field(min_length=1, max_length=1000)
    #: The plan row this refines, so the new decision can cite what it revised.
    previous_decision_id: uuid.UUID | None = None


class AiApplyRequest(BaseModel):
    """Turn a validated plan into concrete parameters."""

    model_config = ConfigDict(extra="forbid")

    plan: dict[str, Any]
    parameters: dict[str, Any] = Field(default_factory=dict)
    printer_id: uuid.UUID | None = None
    #: The plan's ledger row. Optional because a plan can be applied more than
    #: once, and because a client holding a plan from an earlier session has no
    #: id to send.
    plan_decision_id: uuid.UUID | None = None


class AiApplyResponse(BaseModel):
    parameters: dict[str, Any]
    changes: list[dict[str, Any]]
    warnings: list[str] = Field(default_factory=list)
    #: The single strongest priority, useful as a UI headline.
    dominant_priority: Literal[
        "strength", "speed", "material_efficiency", "surface_quality", "balanced"
    ]
    decision: dict[str, Any] | None = None
    decision_id: str | None = None


class AiOverrideRequest(BaseModel):
    """Why the user did not take the suggestion. Not optional: an unexplained
    override records that the advice was declined and nothing more."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        # `min_length=1` admits "   ", which is not a reason. Rejecting it here
        # makes it the caller's 422 rather than a 500 from the ledger's gate: the
        # blank value is their mistake, not a server fault.
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class AiErrorResponse(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    recoverable: bool = True