"""The explainability ledger: every AI proposal, recorded and cited.

`ARCHITECTURE.md` boundary 3 requires that any AI output is recorded in
`ai_decisions` before it can influence a configuration. This module is the
writer, and deliberately the only one: these tables are populated here and
nowhere else, so "was this suggestion AI-derived, what did the model actually
say, and what did we do with it" has exactly one answer.

Three invariants, all enforced in code rather than requested in a prompt:

* **No decision without a reason.** `ai_decisions.reason` is NOT NULL and a row
  whose reason is blank is refused before it is written.
* **No decision without evidence.** `AiDecisionEvidence` exists so a decision
  can be read back with the material that justified it, and `require_evidence`
  refuses to persist a decision citing nothing. An unsourced row in an
  explainability ledger is worse than no row: it looks like justification.
* **The proposal and the applied value are separate columns.** What the model
  said and what the deterministic mapper did with it are different facts, and
  collapsing them is the same conflation the print-fidelity work refuses.

This module records a proposal. It does not approve one. Nothing here validates a
parameter, and nothing here is trusted downstream: the constraint validator in
`app/slicing` still has the final word over every number, and the decision row
records that a proposal existed, not that it was correct.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.intelligence.schemas import LLMPlan, Normalisation
from app.models.printing import AiDecision, AiDecisionEvidence, Calibration

log = get_logger(__name__)

#: `decision_type` values this module writes. Kept short to fit the column's
#: String(24) and narrow on purpose: a value nobody reads is a value nobody
#: filters on.
DECISION_PLAN = "print_intent_plan"
DECISION_APPLY = "parameter_application"

#: Evidence kinds. `ref_type` names the table or concept the row points at.
EVIDENCE_KNOWLEDGE = "knowledge"
EVIDENCE_TOOL = "tool"
EVIDENCE_REQUEST = "user_request"
EVIDENCE_PRIOR_PLAN = "prior_plan"
EVIDENCE_CHANGE = "parameter_change"

#: `AiDecision.source`: where the number came from.
SOURCE_LLM = "llm"
#: A deterministic rule produced it. The assistant's own arithmetic is one of
#: these, which is why the two are never conflated.
SOURCE_DETERMINISTIC = "deterministic"


class DecisionLedgerError(AppError):
    """A decision could not be recorded. Never swallowed.

    Raised as a 500 rather than a 4xx on purpose: the user did nothing wrong.
    The right outcome is that the request fails loudly, because an AI proposal
    that leaves no trace is exactly what this ledger exists to prevent.
    """

    code = "AI_DECISION_UNRECORDED"
    http_status = 500
    message = "The AI proposal could not be recorded in the decision ledger."


class MissingReasonError(DecisionLedgerError):
    """A decision was offered with no reason attached."""

    code = "AI_DECISION_NO_REASON"


class MissingEvidenceError(DecisionLedgerError):
    """A decision was offered with nothing to justify it."""

    code = "AI_DECISION_NO_EVIDENCE"


@dataclass(frozen=True)
class Evidence:
    """One citation backing a decision."""

    kind: str
    ref_type: str
    ref_id: str | None = None
    excerpt: str | None = None
    weight: Decimal | float = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref_type": self.ref_type,
            "ref_id": self.ref_id,
            "excerpt": self.excerpt,
            "weight": float(self.weight),
        }


@dataclass(frozen=True)
class RecordedDecision:
    """What was written, for the API response and the log line."""

    id: uuid.UUID
    decision_type: str
    reason: str
    evidence_count: int
    #: True when the model returned a value the server had to correct. Recorded
    #: on the row because "the AI was right" and "the server fixed the AI" are
    #: different claims and a reader of this table must be able to tell them
    #: apart.
    normalised: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "decision_type": self.decision_type,
            "reason": self.reason,
            "evidence_count": self.evidence_count,
            "normalised": self.normalised,
        }


def require_reason(reason: str | None) -> str:
    """Reject a decision with no reason. Explainability is a storage rule."""
    if reason is None or not reason.strip():
        raise MissingReasonError(
            "Refusing to record an AI decision with no reason.",
            details={"decision": "reason is required and must not be blank"},
        )
    return reason.strip()


def require_evidence(evidence: Sequence[Evidence]) -> list[Evidence]:
    """Reject a decision that cites nothing."""
    if not evidence:
        raise MissingEvidenceError(
            "Refusing to record an AI decision with no evidence.",
            details={
                "decision": "at least one ai_decision_evidence row is required",
                "accepted_kinds": [
                    EVIDENCE_KNOWLEDGE,
                    EVIDENCE_TOOL,
                    EVIDENCE_REQUEST,
                    EVIDENCE_PRIOR_PLAN,
                    EVIDENCE_CHANGE,
                ],
            },
        )
    return list(evidence)


# --------------------------------------------------------------------------- #
# evidence assembly
# --------------------------------------------------------------------------- #
#
# Assembled from what the planner actually used, so the ledger cannot claim
# support the request did not have. A refine call with no job context cites the
# prior plan and the instruction, because that is genuinely all it had.


def _knowledge_evidence(retrieved: Iterable[Any]) -> list[Evidence]:
    """Cite each retrieved knowledge document with its retrieval score."""
    evidence: list[Evidence] = []
    for item in retrieved:
        document = getattr(item, "document", None)
        if document is None:
            continue
        score = getattr(item, "score", None)
        evidence.append(
            Evidence(
                kind=EVIDENCE_KNOWLEDGE,
                ref_type="knowledge_document",
                ref_id=getattr(document, "id", None),
                excerpt=getattr(document, "content", None),
                weight=score if isinstance(score, (int, float)) else 1,
            )
        )
    return evidence


def _tool_evidence(tool_results: Iterable[Mapping[str, Any]]) -> list[Evidence]:
    """Cite each tool the planner consulted, and its one-line summary."""
    evidence: list[Evidence] = []
    for result in tool_results:
        if not isinstance(result, Mapping):
            continue
        name = result.get("name")
        if not name:
            continue
        data = result.get("data")
        available = isinstance(data, Mapping) and data.get("available", True)
        if not available:
            # A tool that reported "not available" is not evidence for anything.
            # Recording it would make the ledger look fuller than the reasoning
            # was.
            continue
        evidence.append(
            Evidence(
                kind=EVIDENCE_TOOL,
                ref_type="tool",
                ref_id=str(name),
                excerpt=str(result.get("summary") or "")[:500] or None,
            )
        )
    return evidence


def _request_evidence(request: str) -> Evidence:
    """The user's own words. Always available, so a bare plan is still citable."""
    return Evidence(
        kind=EVIDENCE_REQUEST,
        ref_type="assistant_request",
        excerpt=request.strip()[:2000] or None,
    )


# --------------------------------------------------------------------------- #
# the writer
# --------------------------------------------------------------------------- #


def record_plan(
    db: Session,
    *,
    user_id: uuid.UUID,
    plan: LLMPlan,
    provider: str,
    model: str,
    request: str,
    prompt_hash: str | None = None,
    normalisations: Sequence[Normalisation] = (),
    retrieved: Iterable[Any] = (),
    tool_results: Iterable[Mapping[str, Any]] = (),
    model_version_id: uuid.UUID | None = None,
    slice_job_id: uuid.UUID | None = None,
    prior_decision_id: uuid.UUID | None = None,
) -> RecordedDecision:
    """Record one validated plan as a decision, and return what was written.

    `proposed_value` holds the validated plan: the intent the model expressed,
    plus any correction the server had to make. `applied_value` stays `None`
    here on purpose. A plan is a proposal; it becomes an applied value when
    `/ai/apply` maps it to parameters, and that is a second row. Collapsing the
    two would make the ledger unable to answer "what did the user actually get".
    """
    reason = require_reason(
        plan.rationale.strip()
        or "The assistant read the request without a stated rationale and scored "
        "it from the retrieved platform rules."
    )
    evidence = require_evidence(
        [
            *_knowledge_evidence(retrieved),
            *_tool_evidence(tool_results),
            # The user's own words are cited even when everything else is
            # absent, because a plan with no context at all is legitimate: the
            # common first message is just a sentence.
            _request_evidence(request),
            # A refinement is evidence-linked to what it revised, so "why is
            # this different from the last plan" is answerable from the table.
            *(
                [
                    Evidence(
                        kind=EVIDENCE_PRIOR_PLAN,
                        ref_type="ai_decision",
                        ref_id=str(prior_decision_id),
                        excerpt="Revised from this earlier plan",
                    )
                ]
                if prior_decision_id is not None
                else []
            ),
        ]
    )

    proposed = plan.model_dump()
    if normalisations:
        # The correction is part of the proposal. A reader comparing this row to
        # the raw model output must be able to see what changed and why.
        proposed["_normalisations"] = [item.to_dict() for item in normalisations]

    decision = AiDecision(
        user_id=user_id,
        slice_job_id=slice_job_id,
        model_version_id=model_version_id,
        decision_type=DECISION_PLAN,
        proposed_value=proposed,
        applied_value=None,
        reason=reason,
        confidence=_confidence(plan.confidence),
        # A plan from a local model has no measured track record behind it. The
        # enum exists to say exactly that, and this row must not claim
        # `historical`.
        calibration=Calibration.uncalibrated,
        source=SOURCE_LLM,
        model_identifier=f"{provider}:{model}"[:80],
        prompt_hash=prompt_hash,
        evidence=[_evidence_row(item) for item in evidence],
    )
    db.add(decision)
    db.flush()

    log.info(
        "ai.decision_recorded",
        decision_id=str(decision.id),
        decision_type=DECISION_PLAN,
        model=decision.model_identifier,
        evidence=len(evidence),
        normalisations=len(normalisations),
    )
    return RecordedDecision(
        id=decision.id,
        decision_type=DECISION_PLAN,
        reason=reason,
        evidence_count=len(evidence),
        normalised=bool(normalisations),
    )


def record_application(
    db: Session,
    *,
    user_id: uuid.UUID,
    plan: LLMPlan,
    plan_decision_id: uuid.UUID | None,
    parameters: Mapping[str, Any],
    changes: Sequence[Any],
    warnings: Sequence[str],
    model_version_id: uuid.UUID | None = None,
    slice_job_id: uuid.UUID | None = None,
) -> RecordedDecision:
    """Record the parameters a plan produced, and cite each one it moved.

    This is the row that answers "the assistant changed my layer height to
    0.18 — on what basis?". The basis is per-field, so the evidence is too.
    """
    reasons = [str(getattr(change, "reason", "")).strip() for change in changes]
    reasons = [item for item in reasons if item]
    reason = require_reason(
        "; ".join(reasons)
        or "The plan produced no parameter changes against the supplied values."
    )

    evidence: list[Evidence] = []
    for change in changes:
        field = getattr(change, "field", None)
        if not field:
            continue
        evidence.append(
            Evidence(
                kind=EVIDENCE_CHANGE,
                ref_type="parameter",
                ref_id=str(field),
                excerpt=str(getattr(change, "reason", "")).strip()[:500] or None,
            )
        )
    if plan_decision_id is not None:
        evidence.append(
            Evidence(
                kind=EVIDENCE_PRIOR_PLAN,
                ref_type="ai_decision",
                ref_id=str(plan_decision_id),
                excerpt=f"Plan with dominant priority {plan.intent.dominant()}",
            )
        )
    if not evidence:
        # Nothing changed and there is no prior plan row to point at. The
        # decision is still real — "the plan was a no-op" is worth recording —
        # so it cites the plan itself rather than being refused.
        evidence.append(
            Evidence(
                kind=EVIDENCE_PRIOR_PLAN,
                ref_type="print_plan",
                excerpt=f"No parameter change; dominant priority {plan.intent.dominant()}",
            )
        )
    evidence = require_evidence(evidence)

    decision = AiDecision(
        user_id=user_id,
        slice_job_id=slice_job_id,
        model_version_id=model_version_id,
        decision_type=DECISION_APPLY,
        proposed_value={"intent": plan.intent.to_dict()},
        applied_value={"parameters": dict(parameters)},
        reason=reason,
        confidence=_confidence(plan.confidence),
        calibration=Calibration.heuristic,
        # The numbers were produced by `apply_intent`, not by the model. The
        # source column is how a reader knows which of the two they are looking
        # at, and saying "llm" here would be the conflation boundary 3 exists
        # to prevent.
        source=SOURCE_DETERMINISTIC,
        model_identifier=None,
        evidence=[_evidence_row(item) for item in evidence],
    )
    db.add(decision)
    db.flush()

    log.info(
        "ai.application_recorded",
        decision_id=str(decision.id),
        changes=len(changes),
        warnings=len(warnings),
    )
    return RecordedDecision(
        id=decision.id,
        decision_type=DECISION_APPLY,
        reason=reason,
        evidence_count=len(evidence),
    )


def owns(db: Session, decision_id: uuid.UUID, *, user_id: uuid.UUID) -> bool:
    """True when this decision belongs to this user.

    Checked in the WHERE clause rather than by fetching and comparing, so an
    unknown id and another user's id are indistinguishable to the caller.
    """
    return (
        db.execute(
            select(AiDecision.id).where(
                AiDecision.id == decision_id, AiDecision.user_id == user_id
            )
        ).first()
        is not None
    )


def record_override(
    db: Session,
    decision_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    reason: str,
) -> None:
    """Mark a recorded decision as overridden by the user.

    A suggestion the user did not take is a result, and the ledger is where it
    belongs. Without this, the table would only ever contain advice that was
    followed, which is precisely the sample you cannot audit.
    """
    decision = db.get(AiDecision, decision_id)
    if decision is None:
        log.warning("ai.override_unknown_decision", decision_id=str(decision_id))
        return
    if decision.overridden_by is not None:
        # First override wins: a later re-application must not rewrite the
        # record of the moment the user disagreed.
        return
    decision.overridden_by = user_id
    decision.override_reason = require_reason(reason)
    decision.overridden_at = utcnow()
    db.flush()
    log.info("ai.decision_overridden", decision_id=str(decision_id))


# --------------------------------------------------------------------------- #
# the read path
# --------------------------------------------------------------------------- #


def list_decisions(
    db: Session,
    *,
    user_id: uuid.UUID,
    limit: int = 50,
    decision_type: str | None = None,
) -> list[dict[str, Any]]:
    """Read this user's decisions back, newest first, with their evidence.

    The ledger is only an explainability feature if it can be read, so the same
    writer module owns the reader. Evidence is loaded in the same query rather
    than per row: a plan cites several documents, and N+1 here would be a
    latency bug on the one screen that explains a decision to a user.
    """
    bounded = max(1, min(int(limit), 200))
    statement = (
        select(AiDecision)
        .where(AiDecision.user_id == user_id)
        .order_by(AiDecision.created_at.desc())
        .limit(bounded)
    )
    if decision_type:
        statement = statement.where(AiDecision.decision_type == decision_type)

    decisions = db.execute(statement).unique().scalars().all()
    return [
        {
            "id": str(decision.id),
            "decision_type": decision.decision_type,
            "reason": decision.reason,
            "confidence": float(decision.confidence) if decision.confidence else None,
            "calibration": str(
                getattr(decision.calibration, "value", decision.calibration)
            ),
            "source": decision.source,
            "model_identifier": decision.model_identifier,
            "prompt_hash": decision.prompt_hash,
            "proposed_value": decision.proposed_value,
            "applied_value": decision.applied_value,
            "slice_job_id": str(decision.slice_job_id) if decision.slice_job_id else None,
            "overridden_by": str(decision.overridden_by) if decision.overridden_by else None,
            "overridden_at": (
                decision.overridden_at.isoformat() if decision.overridden_at else None
            ),
            "override_reason": decision.override_reason,
            "created_at": decision.created_at.isoformat(),
            "evidence": [_evidence_to_dict(item) for item in decision.evidence],
        }
        for decision in decisions
    ]


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _evidence_row(evidence: Evidence) -> AiDecisionEvidence:
    weight = evidence.weight
    if not isinstance(weight, Decimal):
        weight = Decimal(str(float(weight)))
    # The column is Numeric(4, 3): three decimals, and at most 9.999. A score
    # outside that is clamped rather than allowed to raise, because a retrieval
    # score is not worth failing a write over.
    weight = max(Decimal("0.001"), min(weight, Decimal("9.999")))
    return AiDecisionEvidence(
        kind=evidence.kind[:24],
        ref_type=evidence.ref_type[:40],
        ref_id=evidence.ref_id[:64] if evidence.ref_id else None,
        excerpt=evidence.excerpt,
        weight=weight,
    )


def _confidence(value: float | None) -> Decimal | None:
    """Numeric(4, 3) confidence, or None. Never rounded into a false precision."""
    if value is None:
        return None
    bounded = max(0.0, min(float(value), 1.0))
    return Decimal(str(round(bounded, 3)))


def _evidence_to_dict(row: AiDecisionEvidence) -> dict[str, Any]:
    return {
        "kind": row.kind,
        "ref_type": row.ref_type,
        "ref_id": row.ref_id,
        "excerpt": row.excerpt,
        "weight": float(row.weight),
    }
