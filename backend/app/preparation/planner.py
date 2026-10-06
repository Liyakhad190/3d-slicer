"""Propose, validate, and if necessary replan.

This is the only place the print-preparation language model is called, and the
loop it runs is the whole design in one function:

1. Assemble measured facts server-side.
2. Ask for a plan constrained to `PRINT_PLAN_JSON_SCHEMA`.
3. Validate every claim deterministically.
4. If a claim failed and a better answer could fix it, go back with the
   constraint the model violated and ask again — up to `MAX_ATTEMPTS`.
5. On the final attempt, accept the claims the platform is willing to correct
   (with the correction recorded) and refuse the rest.

Nothing here falls back to a default plan. If the model is unavailable, the
call fails with the provider's own error code; if the plan is rejected, the
caller receives the failing checks so the user can see *which* claim was
refused. There is no path from "the model said so" to machine code.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.intelligence.providers.base import (
    LLM_INVALID_OUTPUT,
    LLM_UNAVAILABLE,
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)
from app.intelligence.providers.registry import get_llm_provider
from app.intelligence.schemas import IntentPriorities, Normalisation
from app.preparation.analysis import PreAnalysis
from app.preparation.context import (
    SYSTEM_PROMPT,
    PreparationContext,
    collect_knowledge,
    feedback_from,
)
from app.preparation.schemas import (
    PRINT_PLAN_JSON_SCHEMA,
    AiPrintPlan,
    ValidatedPrintPlan,
    coerce_print_plan,
)
from app.preparation.validators import ValidationOutcome, validate_plan

log = get_logger(__name__)

#: Attempts over the whole loop: the first proposal plus two replans.
MAX_ATTEMPTS = 3

#: The plan schema is large. `settings.llm_num_predict` defaults to 512, which
#: truncates a 22-category JSON object mid-structure and guarantees a parse
#: failure; this raises the ceiling only for print-plan requests.
PRINT_PLAN_MAX_TOKENS = 2048

STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


@dataclass
class PrintPlanResult:
    """A proposed plan, its deterministic verdict, and how it got there."""

    analysis: PreAnalysis
    plan: AiPrintPlan
    outcome: ValidationOutcome
    status: str
    attempts: int
    provider: str = ""
    model: str = ""
    duration_ms: int = 0
    prompt_hash: str = ""
    #: Verbatim text of the prompt that produced `plan`, and the model's
    #: verbatim answer. Kept so the persisted attempt can be replayed without
    #: re-deriving what the model was actually shown.
    prompt_text: str = ""
    raw_response: str = ""
    #: Coercion results per attempt, kept so every correction is auditable.
    coercions: list[ValidatedPrintPlan] = field(default_factory=list)
    normalisations: list[Normalisation] = field(default_factory=list)
    #: Feedback sent on each replan. Empty when the first attempt passed.
    feedback_history: list[list[str]] = field(default_factory=list)
    rejected_reason: str | None = None

    @property
    def approved(self) -> bool:
        return self.status == STATUS_APPROVED

    @property
    def repaired(self) -> bool:
        """True when the plan only passed after at least one replan."""
        return self.approved and self.attempts > 1

    @property
    def parameters(self) -> dict[str, Any]:
        """The canonical parameters this plan is allowed to set.

        Merged view: optimizer `locked` keys plus direct overrides. The caller
        still has to run the optimizer and merge; this is the plan's contribution.
        """
        merged = dict(self.outcome.locked)
        merged.update(self.outcome.overrides)
        return merged

    @property
    def intent(self) -> IntentPriorities:
        return self.plan.objective.intent

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "approved": self.approved,
            "repaired": self.repaired,
            "attempts": self.attempts,
            "provider": self.provider,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "prompt_hash": self.prompt_hash,
            "plan": self.plan.model_dump(),
            "validation": self.outcome.to_dict(),
            "normalisations": [n.to_dict() for n in self.normalisations],
            "feedback_history": self.feedback_history,
            "rejected_reason": self.rejected_reason,
            "analysis_version": self.analysis.version,
            "input_sha256": self.analysis.input_sha256,
        }


def _max_tokens(settings: Settings) -> int:
    return max(int(settings.llm_num_predict), PRINT_PLAN_MAX_TOKENS)


def propose_print_plan(
    analysis: PreAnalysis,
    *,
    provider: LLMProvider | None = None,
    settings: Settings | None = None,
    intent: IntentPriorities | None = None,
    objective: str = "balanced",
    material: dict[str, Any] | None = None,
    user_request: str = "",
    knowledge: list[Any] | None = None,
) -> PrintPlanResult:
    """Run the propose/validate/replan loop. Raises `LLMError` only when the
    model itself failed; a plan the validator refuses is returned, not raised,
    because the caller must be able to show which check failed."""
    settings = settings or get_settings()
    provider = provider or get_llm_provider(settings)
    intent = intent or IntentPriorities()
    material = material or {}

    normalisations: list[Normalisation] = []
    coercions: list[ValidatedPrintPlan] = []
    feedback_history: list[list[str]] = []
    feedback: list[str] = []

    if knowledge is None:
        knowledge = collect_knowledge(user_request, settings=settings)

    total_duration_ms = 0
    last_error: LLMError | None = None
    attempt = 0
    outcome: ValidationOutcome | None = None
    plan: AiPrintPlan | None = None
    prompt_hash = ""
    prompt_text = ""
    raw_text = ""
    provider_name = ""
    model_name = ""

    while attempt < MAX_ATTEMPTS:
        attempt += 1
        context = PreparationContext(
            analysis=analysis,
            intent=intent,
            objective=objective,
            material=material,
            user_request=user_request,
            feedback=list(feedback),
            attempt=attempt,
            knowledge=list(knowledge),
        )
        prompt = context.build_prompt()
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        prompt_text = prompt

        request_kwargs: dict[str, Any] = dict(
            prompt=prompt,
            json_schema=PRINT_PLAN_JSON_SCHEMA,
            system=SYSTEM_PROMPT,
            temperature=settings.llm_temperature,
            max_tokens=_max_tokens(settings),
            purpose="ai_print_plan",
            timeout_seconds=settings.llm_timeout_seconds,
        )

        try:
            response: LLMResponse = provider.generate(LLMRequest(**request_kwargs))
        except LLMError as exc:
            last_error = exc
            if exc.code in (LLM_UNAVAILABLE, "LLM_TIMEOUT", "LLM_PROVIDER_ERROR") and attempt < MAX_ATTEMPTS:
                log.warning("print_plan.llm_retry", attempt=attempt, code=exc.code)
                continue
            raise

        total_duration_ms += response.duration_ms
        raw_text = response.raw_text
        provider_name = response.provider
        model_name = response.model

        validated = coerce_print_plan(response.parsed)
        coercions.append(validated)
        normalisations.extend(validated.normalisations)

        if not validated.valid:
            feedback = [f"[REJECTED] {validated.rejected_reason}"]
            feedback_history.append(list(feedback))
            log.warning(
                "print_plan.structure_rejected",
                attempt=attempt, reason=validated.rejected_reason,
            )
            if attempt >= MAX_ATTEMPTS:
                raise LLMError(
                    LLM_INVALID_OUTPUT,
                    "The model could not produce a structurally valid print plan.",
                    details={"reason": validated.rejected_reason, "attempts": attempt},
                    recoverable=False,
                )
            continue

        plan = validated.plan
        outcome = validate_plan(plan, analysis)

        if outcome.passed:
            log.info(
                "print_plan.approved",
                attempt=attempt,
                warnings=len(outcome.warning_checks),
                normalisations=len(outcome.normalisations),
            )
            return PrintPlanResult(
                analysis=analysis,
                plan=plan,
                outcome=outcome,
                status=STATUS_APPROVED,
                attempts=attempt,
                provider=provider_name,
                model=model_name,
                duration_ms=total_duration_ms,
                prompt_hash=prompt_hash,
                prompt_text=prompt_text,
                raw_response=raw_text,
                coercions=coercions,
                normalisations=normalisations,
                feedback_history=feedback_history,
            )

        log.warning(
            "print_plan.checks_failed",
            attempt=attempt,
            failed=[c.code for c in outcome.failed_checks],
            correctable=outcome.can_replan,
        )

        if outcome.can_replan and attempt < MAX_ATTEMPTS:
            feedback = feedback_from(outcome)
            feedback_history.append(list(feedback))
            continue

        # Either the attempts are exhausted or the failures are ones no better
        # answer could fix. Either way the loop ends and `finalise()` decides
        # what the platform is willing to correct.
        break

    # -- final attempt -----------------------------------------------------
    if plan is None or outcome is None:
        if last_error is not None:
            raise last_error
        raise LLMError(
            LLM_INVALID_OUTPUT,
            "Print planning produced no plan.",
            details={"attempts": attempt},
            recoverable=False,
        )

    final_outcome = outcome.finalise()
    normalisations.extend(
        n for n in final_outcome.normalisations if n not in normalisations
    )

    if final_outcome.passed:
        log.info(
            "print_plan.approved_after_repair",
            attempts=attempt,
            corrections=len(final_outcome.normalisations),
        )
        return PrintPlanResult(
            analysis=analysis,
            plan=plan,
            outcome=final_outcome,
            status=STATUS_APPROVED,
            attempts=attempt,
            provider=provider_name,
            model=model_name,
            duration_ms=total_duration_ms,
            prompt_hash=prompt_hash,
            prompt_text=prompt_text,
            raw_response=raw_text,
            coercions=coercions,
            normalisations=normalisations,
            feedback_history=feedback_history,
        )

    reasons = "; ".join(
        f"{c.category}: {c.reason}" for c in final_outcome.failed_checks[:4]
    )
    log.error(
        "print_plan.rejected",
        attempts=attempt,
        blocking=[c.code for c in final_outcome.failed_checks],
    )
    return PrintPlanResult(
        analysis=analysis,
        plan=plan,
        outcome=final_outcome,
        status=STATUS_REJECTED,
        attempts=attempt,
        provider=provider_name,
        model=model_name,
        duration_ms=total_duration_ms,
        prompt_hash=prompt_hash,
        prompt_text=prompt_text,
        raw_response=raw_text,
        coercions=coercions,
        normalisations=normalisations,
        feedback_history=feedback_history,
        rejected_reason=reasons,
    )
