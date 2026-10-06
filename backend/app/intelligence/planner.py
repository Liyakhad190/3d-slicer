"""The assistant's planning step: user request -> validated `LLMPlan`.

This is the only place a model is called, and it calls it for exactly one thing:
turning a natural-language request into the structured intent schema. It never
touches the slicer.

The order matters:

1. Build context deterministically (tools, then retrieval).
2. Ask the model, constrained to the plan schema.
3. Validate the JSON, and reject or record anything out of range.
4. Return a plan the existing optimizer can act on.

Failure at any step produces a structured `LLMError`. There is no fallback plan
and no heuristic guess dressed up as AI output.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.intelligence.providers.base import (
    LLM_INVALID_OUTPUT,
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)
from app.intelligence.rag.retriever import format_context, retrieve
from app.intelligence.schemas import (
    PLAN_JSON_SCHEMA,
    ValidatedPlan,
    coerce_plan,
)
from app.intelligence.tools import ToolRegistry, render_tool_context

log = get_logger(__name__)

SYSTEM_PROMPT = (
    "You translate a 3D printing request into a structured plan.\n"
    "Only interpret requests about preparing or slicing a 3D model. Never answer "
    "personal questions or unrelated general chat as print priorities. Preserve "
    "the user's stated goal; do not invent priorities they did not ask for.\n"
    "You never output G-code, temperatures, layer heights or speeds: you only "
    "express what the user wants and the limits they set. A separate deterministic "
    "system converts your plan into actual slicing parameters.\n"
    "Score each priority from 0 to 1, where 1 means maximally important.\n"
    "If the request is ambiguous, choose the most reasonable reading, set "
    "confidence below 0.6 and explain the ambiguity in the rationale."
)

MAX_USER_REQUEST_CHARS = 2000
_PRINT_INTENT_TERMS = re.compile(
    r"\b(3d|print\w*|slic\w*|model|part|object|strength|strong\w*|durab\w*|"
    r"sturd\w*|rigid\w*|speed|fast\w*|quick\w*|time|quality|smooth\w*|"
    r"surface|finish|material|lightweight|weight|support\w*|adhesion|reliab\w*|"
    r"functional|prototype|layer\w*|wall\w*|infill|nozzle|filament|temperature|"
    r"temp|bed|overhang|bridge|scale\w*|dimension\w*|accurac\w*|detail\w*)\b",
    re.IGNORECASE,
)


@dataclass
class PlannerContext:
    """Everything the model may see. Assembled server-side, never by the model."""

    model_summary: dict[str, Any] = field(default_factory=dict)
    machine_limits: dict[str, Any] = field(default_factory=dict)
    material_profile: dict[str, Any] = field(default_factory=dict)
    slicer_capabilities: dict[str, Any] = field(default_factory=dict)

    def to_tool_context(self) -> dict[str, Any]:
        return {
            "model_summary": self.model_summary,
            "machine_limits": self.machine_limits,
            "material_profile": self.material_profile,
            "slicer_capabilities": self.slicer_capabilities,
        }


@dataclass
class PlanResult:
    """A validated plan, plus the provenance the UI and audit log need."""

    plan: ValidatedPlan
    provider: str
    model: str
    duration_ms: int
    attempts: int
    #: The user's original words, echoed so the UI can show what was answered.
    request: str
    tool_context: list[dict[str, Any]] = field(default_factory=list)
    retrieved_ids: list[str] = field(default_factory=list)
    #: The retrieved documents themselves, kept so the decision ledger can cite
    #: what actually informed the plan. `retrieved_ids` is the API-facing
    #: summary; this is the evidence.
    retrieved: list[Any] = field(default_factory=list)
    #: SHA-256 of the exact prompt sent. Stored on the decision row so a plan
    #: can be traced to the text that produced it without keeping the text
    #: (which contains the user's own words) in the ledger.
    prompt_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "provider": self.provider,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "validation": self.plan.to_dict(),
            "context": {
                "tools": self.tool_context,
                "retrieved": self.retrieved_ids,
            },
        }


def plan_from_request(
    request: str,
    *,
    provider: LLMProvider,
    context: PlannerContext | None = None,
    settings: Settings | None = None,
    conversation: list[Mapping[str, str]] | None = None,
) -> PlanResult:
    """Ask the model for a plan and validate it. Raises `LLMError` on failure."""
    settings = settings or get_settings()
    context = context or PlannerContext()

    user_request = request.strip()[:MAX_USER_REQUEST_CHARS]
    if not user_request:
        raise LLMError(LLM_INVALID_OUTPUT, "The request is empty.", recoverable=False)
    if not _PRINT_INTENT_TERMS.search(user_request):
        raise LLMError(
            LLM_INVALID_OUTPUT,
            "This assistant only prepares 3D prints. Describe a print goal such as strength, surface finish, speed, material use, supports, or reliability.",
            details={"hint": "Enter a goal for this model's print."},
            recoverable=False,
        )

    # -- deterministic context, assembled server-side ----------------------
    registry = ToolRegistry(context.to_tool_context())
    # All four tools are always safe to attempt: a missing one returns
    # `available: false` rather than raising.
    tool_results = registry.collect(
        ("model_summary", "machine_limits", "material_profile", "slicer_capabilities")
    )
    tool_context = [result.to_dict() for result in tool_results]

    retrieved = retrieve(user_request, settings=settings)
    retrieved_ids = [item.document.id for item in retrieved]

    tool_text = render_tool_context(tool_results)
    prompt_sections = [f"User request:\n{user_request}"]
    if tool_text:
        prompt_sections.append(f"Known facts about this job:\n{tool_text}")
    if retrieved:
        prompt_sections.append(f"Platform knowledge:\n{format_context(retrieved)}")
    if conversation:
        turns = [
            f"{'User' if turn.get('role') == 'user' else 'Assistant'}: {turn.get('content', '')}"
            for turn in conversation[-4:]
        ]
        prompt_sections.append("Recent conversation:\n" + "\n".join(turns))
    prompt_sections.append("Respond with the plan JSON only.")
    prompt = "\n\n".join(prompt_sections)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    # -- generate, with the retry policy applied here -----------------------
    llm_request = LLMRequest(
        prompt=prompt,
        json_schema=PLAN_JSON_SCHEMA,
        system=SYSTEM_PROMPT,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_num_predict,
        purpose="print_intent_plan",
        timeout_seconds=settings.llm_timeout_seconds,
    )

    attempts = 0
    last_error: LLMError | None = None
    total_duration_ms = 0

    while attempts <= settings.llm_max_retries:
        attempts += 1
        try:
            response: LLMResponse = provider.generate(llm_request)
        except LLMError as exc:
            last_error = exc
            # Only transport-shaped failures are worth repeating. A refusal or a
            # bad request will fail identically on the next attempt, and paying
            # another 25 seconds to learn that is a bad trade.
            if exc.code not in ("LLM_UNAVAILABLE", "LLM_TIMEOUT", "LLM_PROVIDER_ERROR"):
                raise
            if attempts > settings.llm_max_retries:
                raise
            log.warning("llm.retry", attempt=attempts, code=exc.code)
            continue

        total_duration_ms += response.duration_ms
        validated = coerce_plan(response.parsed)

        if validated.valid:
            log.info(
                "llm.plan_validated",
                model=response.model,
                duration_ms=response.duration_ms,
                attempts=attempts,
                normalisations=len(validated.normalisations),
                dominant=validated.plan.intent.dominant(),
            )
            return PlanResult(
                plan=validated,
                provider=response.provider,
                model=response.model,
                duration_ms=total_duration_ms,
                attempts=attempts,
                request=user_request,
                tool_context=tool_context,
                retrieved_ids=retrieved_ids,
                retrieved=list(retrieved),
                prompt_hash=prompt_hash,
            )

        # Structurally valid JSON that we cannot use. Retrying with the same
        # prompt on a small model usually reproduces the same answer, so this
        # reports failure rather than burning another call.
        log.warning(
            "llm.plan_rejected",
            model=response.model,
            reason=validated.rejected_reason,
            normalisations=len(validated.normalisations),
        )
        raise LLMError(
            LLM_INVALID_OUTPUT,
            "The model returned a plan that could not be validated.",
            details={"reason": validated.rejected_reason, "attempts": attempts},
            recoverable=False,
        )

    if last_error is not None:
        raise last_error
    raise LLMError(LLM_INVALID_OUTPUT, "Planning failed for an unknown reason.")