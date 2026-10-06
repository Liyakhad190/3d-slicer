"""AI assistant endpoints.

Five endpoints, in increasing order of what they are allowed to do:

* `GET  /ai/health`          — probe the provider for real.
* `POST /ai/plan`            — natural language -> validated intent. No side effects.
* `POST /ai/apply`           — validated intent -> suggested parameters. Pure arithmetic.
* `POST /ai/refine`          — adjust a prior plan from a follow-up instruction.
* `GET  /ai/decisions`       — read back what was recorded, with its evidence.
* `POST /ai/decisions/{id}/override` — record that the user did not take it.

None of them slices, writes a machine profile, or touches a machine. Slicing
stays behind the existing, authenticated `/api/v1/slice` pipeline, which is the
only path that runs CuraEngine. That separation is deliberate: the AI can
produce a bad plan and the worst outcome is a rejected configuration, never a
ruined print.

The two write the only rows in `ai_decisions`. That is the whole point of the
ledger: a proposal the platform cannot point to afterwards did not really happen
here, and a suggestion the user ignored is exactly the one worth auditing.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUser, DbSession, SettingsDep, rate_limit
from app.core.config import Settings
from app.core.errors import LLMServiceError, NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.intelligence import ledger
from app.intelligence.api.schemas import (
    AiApplyRequest,
    AiApplyResponse,
    AiHealthResponse,
    AiOverrideRequest,
    AiPlanRequest,
    AiPlanResponse,
    AiRefineRequest,
)
from app.intelligence.intent_mapper import apply_intent
from app.intelligence.planner import PlannerContext, plan_from_request
from app.intelligence.providers.base import LLMError
from app.intelligence.providers.registry import get_llm_provider
from app.intelligence.schemas import coerce_plan

log = get_logger(__name__)
router = APIRouter(tags=["ai"])

#: Shown when a provider is unavailable, per failure code. Actionable rather
#: than diagnostic: the user can act on all of these.
LLM_HINTS = {
    "LLM_UNAVAILABLE": (
        "Start the local model server with `ollama serve`, or set OLLAMA_BASE_URL."
    ),
    "LLM_NOT_CONFIGURED": "Set the required environment variables and restart.",
    "LLM_MODEL_MISSING": "Pull the configured model, then retry.",
    "LLM_TIMEOUT": (
        "The local model took too long. Use a smaller model, or raise "
        "LLM_TIMEOUT_SECONDS."
    ),
    "LLM_INVALID_OUTPUT": "Try rephrasing the request, or use a larger model.",
    "LLM_PROVIDER_ERROR": "The model backend returned an error; check its logs.",
}

#: Provider failure code -> HTTP status. The split matters to the client: an
#: unreachable model is a retryable infrastructure problem (503), while output
#: the model produced and we refused is a problem with the request (422).
LLM_STATUS_BY_CODE = {
    "LLM_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "LLM_NOT_CONFIGURED": status.HTTP_503_SERVICE_UNAVAILABLE,
    "LLM_MODEL_MISSING": status.HTTP_503_SERVICE_UNAVAILABLE,
    "LLM_TIMEOUT": status.HTTP_504_GATEWAY_TIMEOUT,
    "LLM_INVALID_OUTPUT": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "LLM_PROVIDER_ERROR": status.HTTP_502_BAD_GATEWAY,
}


def _as_http_error(exc: LLMError, *, log_context: dict[str, Any] | None = None) -> LLMServiceError:
    """Convert a provider failure into an error the API envelope preserves."""
    log.warning("ai.request_failed", code=exc.code, **(log_context or {}))
    return LLMServiceError(
        exc.message,
        code=exc.code,
        details={
            **exc.details,
            "recoverable": exc.recoverable,
            "hint": LLM_HINTS.get(exc.code, ""),
        },
    )


def _with_status(error: LLMServiceError, code: str) -> LLMServiceError:
    error.http_status = LLM_STATUS_BY_CODE.get(code, error.http_status)
    return error


@router.get("/ai/health", response_model=AiHealthResponse, summary="Probe the LLM provider")
def ai_health(settings: SettingsDep, _user: CurrentUser) -> AiHealthResponse:
    """Report whether the model is actually reachable.

    This performs a live probe. It does not return the configured values and
    call that "healthy": a configured model that is not installed reports
    `available: false` with `LLM_MODEL_MISSING`.
    """
    provider = get_llm_provider(settings)
    health = provider.health()
    return AiHealthResponse(
        **health.to_dict(),
        hint="" if health.available else LLM_HINTS.get(health.code or "", ""),
    )


@router.post(
    "/ai/plan",
    response_model=AiPlanResponse,
    status_code=status.HTTP_200_OK,
    summary="Turn a natural-language request into a validated intent plan",
    dependencies=[Depends(rate_limit("ai"))],
)
def ai_plan(
    payload: AiPlanRequest,
    db: DbSession,
    user: CurrentUser,
    settings: SettingsDep,
) -> AiPlanResponse:
    """Natural language -> validated `PrintIntent`.

    Read-only for the print: no parameter is changed and no job is created. The
    user reviews the plan, then `/ai/apply` turns it into suggested parameters,
    then the existing `/slice` endpoint does the actual work.

    It does write one row, to `ai_decisions`. Boundary 3 in ARCHITECTURE.md
    requires every AI proposal to be recorded, and a plan the platform cannot
    show the user afterwards is not reviewable.
    """
    context = build_context(db, user.id, payload, settings)
    provider = get_llm_provider(settings)

    try:
        result = plan_from_request(
            payload.request,
            provider=provider,
            context=context,
            settings=settings,
            conversation=payload.conversation,
        )
    except LLMError as exc:
        # Structured and honest: the caller learns the code and the fix. There
        # is no fallback plan, because a fabricated "AI result" is worse than
        # an error the user can act on.
        raise _with_status(
            _as_http_error(exc, log_context={"user_id": str(user.id)}), exc.code
        ) from exc

    recorded = ledger.record_plan(
        db,
        user_id=user.id,
        plan=result.plan.plan,
        provider=result.provider,
        model=result.model,
        request=result.request,
        prompt_hash=result.prompt_hash,
        normalisations=result.plan.normalisations,
        retrieved=result.retrieved,
        tool_results=result.tool_context,
        model_version_id=payload.model_version_id,
    )
    return AiPlanResponse(
        **result.to_dict(), decision=recorded.to_dict(), decision_id=str(recorded.id)
    )


@router.post(
    "/ai/apply",
    response_model=AiApplyResponse,
    summary="Turn a validated plan into suggested parameters",
)
def ai_apply(
    payload: AiApplyRequest,
    db: DbSession,
    user: CurrentUser,
    settings: SettingsDep,
) -> AiApplyResponse:
    """Deterministic intent -> parameters.

    This endpoint makes no model call. It is pure arithmetic over an
    already-validated plan, which is why it is fast, free, and reproducible: the
    same plan always yields the same parameters.

    The result is recorded as its own decision row with `source =
    deterministic`. The model proposed, this code disposed, and the ledger says
    which of the two a reader is looking at.
    """
    validated = coerce_plan(payload.plan)
    if not validated.valid:
        raise ValidationFailedError(
            validated.rejected_reason or "The plan did not validate.",
            code="PLAN_INVALID",
        )

    limits = _layer_height_limits(db, user.id, payload.printer_id)
    parameters, changes, warnings = apply_intent(
        validated.plan, payload.parameters, limits=limits
    )
    recorded = ledger.record_application(
        db,
        user_id=user.id,
        plan=validated.plan,
        plan_decision_id=payload.plan_decision_id,
        parameters=parameters,
        changes=changes,
        warnings=warnings,
    )
    return AiApplyResponse(
        parameters=parameters,
        changes=[change.to_dict() for change in changes],
        warnings=warnings,
        dominant_priority=validated.plan.intent.dominant(),
        decision=recorded.to_dict(),
        decision_id=str(recorded.id),
    )


@router.post(
    "/ai/refine",
    response_model=AiPlanResponse,
    summary="Adjust an existing plan from a follow-up instruction",
    dependencies=[Depends(rate_limit("ai"))],
)
def ai_refine(
    payload: AiRefineRequest,
    db: DbSession,
    user: CurrentUser,
    settings: SettingsDep,
) -> AiPlanResponse:
    """Apply a follow-up instruction to an existing plan.

    The prior plan is quoted into the prompt so the model adjusts it rather than
    answering from scratch. It is validated exactly like a fresh plan, and it
    records its own decision row: a refinement is a new proposal rather than an
    edit to the old one, because the two can disagree.
    """
    provider = get_llm_provider(settings)
    merged = (
        f"Previous plan:\n{_compact(payload.previous_plan)}\n\n"
        f"Change requested: {payload.instruction}"
    )
    try:
        result = plan_from_request(
            merged, provider=provider, settings=settings
        )
    except LLMError as exc:
        raise _with_status(
            _as_http_error(exc, log_context={"user_id": str(user.id)}), exc.code
        ) from exc

    recorded = ledger.record_plan(
        db,
        user_id=user.id,
        plan=result.plan.plan,
        provider=result.provider,
        model=result.model,
        request=payload.instruction,
        prompt_hash=result.prompt_hash,
        normalisations=result.plan.normalisations,
        retrieved=result.retrieved,
        tool_results=result.tool_context,
        prior_decision_id=payload.previous_decision_id,
    )
    return AiPlanResponse(
        **result.to_dict(), decision=recorded.to_dict(), decision_id=str(recorded.id)
    )


# -- the ledger's read path ------------------------------------------------


@router.get("/ai/decisions", summary="Read back recorded AI decisions")
def ai_decisions(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
    decision_type: str | None = Query(default=None),
) -> dict[str, Any]:
    """This user's decision ledger, newest first, with the evidence each cites.

    Another user's decision is simply not in the list, and asking for one by id
    returns the same 404 as a nonexistent id. The ledger must not confirm that
    somebody else's plan exists.
    """
    return {
        "decisions": ledger.list_decisions(
            db, user_id=user.id, limit=limit, decision_type=decision_type
        )
    }


@router.post(
    "/ai/decisions/{decision_id}/override",
    summary="Record that the user did not take an AI suggestion",
    status_code=status.HTTP_200_OK,
)
def ai_decision_override(
    decision_id: uuid.UUID,
    payload: AiOverrideRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """Mark a decision as overridden, with the reason.

    The user not taking the advice is a result worth keeping. A ledger holding
    only the suggestions that were followed cannot be audited, and an assistant
    measured only on the prints it influenced tells you nothing about the ones
    it should not have influenced.
    """
    if not ledger.owns(db, decision_id, user_id=user.id):
        raise NotFoundError("The requested resource does not exist.")
    ledger.record_override(db, decision_id, user_id=user.id, reason=payload.reason)
    return {"id": str(decision_id), "overridden": True, "reason": payload.reason}


# -- context assembly -----------------------------------------------------


def build_context(
    db: Any,
    user_id: Any,
    payload: AiPlanRequest,
    settings: Settings,
) -> PlannerContext:
    """Load only what the model is allowed to see.

    Each block is optional. A missing printer or profile yields an empty dict,
    the corresponding tool reports `available: false`, and planning still works
    from the user's words alone — which is the common case for a first message.
    """
    context = PlannerContext()
    if db is None:
        return context

    try:
        if payload.model_version_id is not None:
            context.model_summary = _model_summary(db, user_id, payload.model_version_id)
        if payload.printer_id is not None:
            context.machine_limits = _limits(db, user_id, payload.printer_id)
        if payload.material_profile_id is not None:
            context.material_profile = _material(db, user_id, payload.material_profile_id)
    except Exception as exc:  # a missing row must not break planning
        log.warning("ai.context_incomplete", error=str(exc))
    return context


def _model_summary(db: Any, user_id: Any, version_id: Any) -> dict[str, Any]:
    from app.api.deps import owned_model_version

    version = owned_model_version(db, user_id, version_id)
    return {
        "name": getattr(version, "name", None),
        "file_type": getattr(version, "file_type", None),
        "triangle_count": getattr(version, "triangle_count", None),
    }


def _limits(db: Any, user_id: Any, printer_id: Any) -> dict[str, Any]:
    from app.api.deps import owned_printer

    printer = owned_printer(db, user_id, printer_id)
    return {
        "machine_name": getattr(printer, "name", None),
        "technology": str(getattr(printer, "technology", "") or ""),
    }


def _material(db: Any, user_id: Any, profile_id: Any) -> dict[str, Any]:
    from app.models.catalog import MaterialProfile

    profile = db.get(MaterialProfile, profile_id)
    return {
        "name": getattr(profile, "name", None),
        "filament_diameter_mm": getattr(profile, "filament_diameter_mm", None),
    }


def _layer_height_limits(
    db: Any, user_id: Any, printer_id: Any
) -> dict[str, float] | None:
    """Read the printer's real layer-height bounds, if a printer was supplied.

    These come from the stored profile, never from the model. Returning `None`
    when no printer is chosen lets `apply_intent` use its own conservative band
    rather than inventing a machine limit.
    """
    if db is None or printer_id is None:
        return None
    try:
        from app.api.deps import owned_printer

        printer = owned_printer(db, user_id, printer_id)
    except Exception as exc:
        log.warning("ai.limits_unavailable", error=str(exc))
        return None

    nozzle = getattr(printer, "nozzle_diameter_mm", None)
    if not isinstance(nozzle, (int, float)) or nozzle <= 0:
        return None
    # The same bounds the slicer profile enforces: 25% of the nozzle up to 75%.
    return {
        "min_layer_height_mm": round(float(nozzle) * 0.25, 3),
        "max_layer_height_mm": round(float(nozzle) * 0.75, 3),
    }


def _compact(payload: dict[str, Any]) -> str:
    """Flatten a plan dict to the few fields worth showing the model."""
    intent = payload.get("intent") or {}
    keep = {
        "intent": intent,
        "constraints": payload.get("constraints") or {},
        "support_preference": payload.get("support_preference"),
        "orientation_preference": payload.get("orientation_preference"),
    }
    return ", ".join(f"{key}={value}" for key, value in keep.items() if value)