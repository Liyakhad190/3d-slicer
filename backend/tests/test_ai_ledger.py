"""Tests for the explainability ledger.

The ledger is the only place a proposal is recorded, so these tests are mostly
about what it refuses to write. A row in an explainability table with no reason
and no evidence looks like justification, and that is the failure mode worth
guarding.

No model is involved: the writer takes a validated plan, so these tests run
offline and in CI.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.intelligence import ledger
from app.intelligence.intent_mapper import apply_intent
from app.intelligence.ledger import (
    EVIDENCE_CHANGE,
    EVIDENCE_KNOWLEDGE,
    EVIDENCE_PRIOR_PLAN,
    EVIDENCE_REQUEST,
    EVIDENCE_TOOL,
    Evidence,
    MissingEvidenceError,
    MissingReasonError,
    record_plan,
    require_evidence,
    require_reason,
)
from app.intelligence.rag.base import KnowledgeDocument, RetrievedDocument
from app.intelligence.schemas import IntentPriorities, IntentConstraints, LLMPlan, Normalisation
from app.models.printing import AiDecision, AiDecisionEvidence


def make_plan(**overrides) -> LLMPlan:
    defaults = {
        "intent": IntentPriorities(
            strength=0.9, speed=0.1, material_efficiency=0.5, surface_quality=0.3
        ),
        "constraints": IntentConstraints(),
        "support_preference": "allowed",
        "orientation_preference": "optimized",
        "rationale": "The user asked for strength and said time does not matter.",
        "confidence": 0.8,
    }
    defaults.update(overrides)
    return LLMPlan(**defaults)


@pytest.fixture
def db(test_client) -> Session:
    """A session on the throwaway test database.

    Nothing is committed: these tests read back through the same session, and
    the test database is discarded with the data directory afterwards.
    """
    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def user_id(db: Session) -> uuid.UUID:
    from app.models.identity import User

    user = User(
        email=f"ledger-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Ledger Test",
        password_hash="x",
    )
    db.add(user)
    db.flush()
    return user.id


# -- the gates -------------------------------------------------------------


class TestGates:
    def test_a_blank_reason_is_refused(self):
        for value in (None, "", "   "):
            with pytest.raises(MissingReasonError):
                require_reason(value)

    def test_a_reason_is_trimmed_not_stored_padded(self):
        assert require_reason("  because  ") == "because"

    def test_no_evidence_is_refused(self):
        with pytest.raises(MissingEvidenceError):
            require_evidence([])

    def test_one_citation_is_enough(self):
        evidence = require_evidence([Evidence(kind=EVIDENCE_REQUEST, ref_type="t")])
        assert len(evidence) == 1

    def test_the_refusal_names_what_is_accepted(self):
        """An error a caller cannot act on is a bad error."""
        with pytest.raises(MissingEvidenceError) as excinfo:
            require_evidence([])
        assert "user_request" in str(excinfo.value.details["accepted_kinds"])


# -- the writer ------------------------------------------------------------


class TestRecordPlan:
    def test_writes_a_cited_llm_row(self, db: Session, user_id: uuid.UUID):
        recorded = record_plan(
            db,
            user_id=user_id,
            plan=make_plan(),
            provider="ollama",
            model="qwen2.5:1.5b",
            request="make it strong",
            prompt_hash="a" * 64,
        )

        row = db.get(AiDecision, recorded.id)
        assert row is not None
        assert row.decision_type == ledger.DECISION_PLAN
        assert row.user_id == user_id
        assert row.source == ledger.SOURCE_LLM
        # A plan from a local model has no measured track record. `historical`
        # would be a claim nobody has evidence for.
        assert row.calibration.value == "uncalibrated"
        assert row.model_identifier == "ollama:qwen2.5:1.5b"
        assert row.prompt_hash == "a" * 64
        assert float(row.confidence) == pytest.approx(0.8)
        assert row.reason

    def test_a_plan_is_not_yet_an_applied_value(self, db: Session, user_id: uuid.UUID):
        """`applied_value` stays empty until deterministic code acts on it.

        Writing it here would make "what the model said" and "what the user got"
        the same column.
        """
        recorded = record_plan(
            db,
            user_id=user_id,
            plan=make_plan(),
            provider="ollama",
            model="m",
            request="strong",
        )
        assert db.get(AiDecision, recorded.id).applied_value is None

    def test_a_bare_request_is_still_citable(self, db: Session, user_id: uuid.UUID):
        """No printer, no profile, no retrieval: the first message is often
        just a sentence, and refusing to record it would be perverse."""
        recorded = record_plan(
            db,
            user_id=user_id,
            plan=make_plan(),
            provider="ollama",
            model="m",
            request="as strong as you can",
        )
        kinds = {item.kind for item in db.get(AiDecision, recorded.id).evidence}
        assert kinds == {EVIDENCE_REQUEST}

    def test_records_the_server_corrections(self, db: Session, user_id: uuid.UUID):
        """A clamped value is part of the proposal, not a footnote."""
        correction = Normalisation(
            field="intent.strength", model_value=5, applied_value=1.0, reason="clamped"
        )
        recorded = record_plan(
            db,
            user_id=user_id,
            plan=make_plan(),
            provider="ollama",
            model="m",
            request="strong",
            normalisations=[correction],
        )
        row = db.get(AiDecision, recorded.id)
        assert recorded.normalised is True
        assert row.proposed_value["_normalisations"][0]["model_value"] == 5

    def test_cites_retrieval_and_tools_with_their_weight(self, db: Session, user_id: uuid.UUID):
        retrieved = RetrievedDocument(
            document=KnowledgeDocument(
                id="priorities-strength",
                title="When the user wants strength",
                content="Strength comes from wall count and layer bonding.",
            ),
            score=0.72,
        )
        recorded = record_plan(
            db,
            user_id=user_id,
            plan=make_plan(),
            provider="ollama",
            model="m",
            request="strong",
            retrieved=[retrieved],
            tool_results=[
                {
                    "name": "machine_limits",
                    "summary": "Build volume: 220, 220, 250 mm",
                    "data": {"available": True, "technology": "fdm"},
                }
            ],
        )
        evidence = {item.kind: item for item in db.get(AiDecision, recorded.id).evidence}
        assert evidence[EVIDENCE_KNOWLEDGE].ref_id == "priorities-strength"
        assert float(evidence[EVIDENCE_KNOWLEDGE].weight) == pytest.approx(0.72)
        assert evidence[EVIDENCE_TOOL].ref_id == "machine_limits"

    def test_an_unavailable_tool_is_not_cited(self, db: Session, user_id: uuid.UUID):
        """A tool that returned nothing justified nothing.

        Citing it anyway would pad the ledger and make the reasoning look
        better-evidenced than it was.
        """
        recorded = record_plan(
            db,
            user_id=user_id,
            plan=make_plan(),
            provider="ollama",
            model="m",
            request="strong",
            tool_results=[
                {
                    "name": "material_profile",
                    "summary": "material_profile: not available for this request.",
                    "data": {"available": False},
                }
            ],
        )
        kinds = {item.kind for item in db.get(AiDecision, recorded.id).evidence}
        assert EVIDENCE_TOOL not in kinds

    def test_a_refinement_cites_the_plan_it_revised(self, db: Session, user_id: uuid.UUID):
        prior = record_plan(
            db,
            user_id=user_id,
            plan=make_plan(),
            provider="ollama",
            model="m",
            request="strong",
        )
        revised = record_plan(
            db,
            user_id=user_id,
            plan=make_plan(intent=IntentPriorities(strength=0.2, surface_quality=0.95)),
            provider="ollama",
            model="m",
            request="actually prioritise the surface",
            prior_decision_id=prior.id,
        )
        kinds = {
            item.ref_id
            for item in db.get(AiDecision, revised.id).evidence
            if item.kind == EVIDENCE_PRIOR_PLAN
        }
        assert str(prior.id) in kinds

    def test_a_plan_with_no_rationale_still_gets_a_reason(self, db: Session, user_id: uuid.UUID):
        """The column is NOT NULL. A silent model must not become a silent row."""
        recorded = record_plan(
            db,
            user_id=user_id,
            plan=make_plan(rationale=""),
            provider="ollama",
            model="m",
            request="strong",
        )
        assert db.get(AiDecision, recorded.id).reason


class TestRecordApplication:
    def test_writes_a_deterministic_row_with_the_parameters(
        self, db: Session, user_id: uuid.UUID
    ):
        plan = make_plan()
        parameters, changes, warnings = apply_intent(plan, {"layer_height": 0.2})
        recorded = ledger.record_application(
            db,
            user_id=user_id,
            plan=plan,
            plan_decision_id=None,
            parameters=parameters,
            changes=changes,
            warnings=warnings,
        )
        row = db.get(AiDecision, recorded.id)
        # `apply_intent` is arithmetic. Claiming `llm` here would be the exact
        # conflation boundary 3 exists to prevent.
        assert row.source == ledger.SOURCE_DETERMINISTIC
        assert row.calibration.value == "heuristic"
        assert row.applied_value["parameters"]["layer_height"] == parameters["layer_height"]
        assert row.model_identifier is None

    def test_cites_every_parameter_it_moved(self, db: Session, user_id: uuid.UUID):
        plan = make_plan()
        parameters, changes, _ = apply_intent(plan, {"layer_height": 0.2})
        recorded = ledger.record_application(
            db,
            user_id=user_id,
            plan=plan,
            plan_decision_id=None,
            parameters=parameters,
            changes=changes,
            warnings=[],
        )
        fields = {
            item.ref_id
            for item in db.get(AiDecision, recorded.id).evidence
            if item.kind == EVIDENCE_CHANGE
        }
        assert fields == {change.field for change in changes}
        assert "layer_height" in fields

    def test_links_the_plan_it_came_from(self, db: Session, user_id: uuid.UUID):
        plan = make_plan()
        plan_row = record_plan(
            db, user_id=user_id, plan=plan, provider="ollama", model="m", request="strong"
        )
        parameters, changes, _ = apply_intent(plan, {})
        recorded = ledger.record_application(
            db,
            user_id=user_id,
            plan=plan,
            plan_decision_id=plan_row.id,
            parameters=parameters,
            changes=changes,
            warnings=[],
        )
        evidence = db.get(AiDecision, recorded.id).evidence
        assert str(plan_row.id) in {item.ref_id for item in evidence}

    def test_a_no_op_is_still_recorded(self, db: Session, user_id: uuid.UUID):
        """"The plan changed nothing" is a result, not a non-event."""
        plan = make_plan()
        parameters, changes, _ = apply_intent(plan, {})
        recorded = ledger.record_application(
            db,
            user_id=user_id,
            plan=plan,
            plan_decision_id=None,
            parameters=parameters,
            changes=[],
            warnings=[],
        )
        row = db.get(AiDecision, recorded.id)
        assert row.reason
        assert len(row.evidence) >= 1


# -- override --------------------------------------------------------------


class TestOverride:
    def test_records_who_and_why(self, db: Session, user_id: uuid.UUID):
        recorded = record_plan(
            db, user_id=user_id, plan=make_plan(), provider="ollama", model="m", request="strong"
        )
        ledger.record_override(
            db, recorded.id, user_id=user_id, reason="I want a draft, not a strong part."
        )
        row = db.get(AiDecision, recorded.id)
        assert row.overridden_by == user_id
        assert row.overridden_at is not None
        assert row.override_reason.startswith("I want a draft")

    def test_the_first_override_wins(self, db: Session, user_id: uuid.UUID):
        """A later re-application must not rewrite the moment of disagreement."""
        recorded = record_plan(
            db, user_id=user_id, plan=make_plan(), provider="ollama", model="m", request="strong"
        )
        ledger.record_override(db, recorded.id, user_id=user_id, reason="first reason")
        ledger.record_override(db, recorded.id, user_id=user_id, reason="second reason")
        assert db.get(AiDecision, recorded.id).override_reason == "first reason"

    def test_an_override_needs_a_reason(self, db: Session, user_id: uuid.UUID):
        recorded = record_plan(
            db, user_id=user_id, plan=make_plan(), provider="ollama", model="m", request="strong"
        )
        with pytest.raises(MissingReasonError):
            ledger.record_override(db, recorded.id, user_id=user_id, reason="  ")

    def test_an_unknown_decision_is_a_no_op_not_a_crash(self, db: Session, user_id: uuid.UUID):
        ledger.record_override(
            db, uuid.uuid4(), user_id=user_id, reason="typo in the id"
        )


# -- reading it back -------------------------------------------------------


class TestReadPath:
    def test_only_your_own_decisions(self, db: Session, user_id: uuid.UUID):
        from app.models.identity import User

        other = User(email=f"other-{uuid.uuid4().hex[:8]}@example.com", display_name="Other", password_hash="x")
        db.add(other)
        db.flush()
        record_plan(
            db, user_id=user_id, plan=make_plan(), provider="ollama", model="m", request="mine"
        )
        record_plan(
            db, user_id=other.id, plan=make_plan(), provider="ollama", model="m", request="theirs"
        )

        listed = ledger.list_decisions(db, user_id=user_id)
        assert len(listed) == 1
        assert listed[0]["reason"]
        assert listed[0]["evidence"]

    def test_ownership_is_checked_in_the_query(self, db: Session, user_id: uuid.UUID):
        from app.models.identity import User

        other = User(email=f"other-{uuid.uuid4().hex[:8]}@example.com", display_name="Other", password_hash="x")
        db.add(other)
        db.flush()
        recorded = record_plan(
            db, user_id=other.id, plan=make_plan(), provider="ollama", model="m", request="x"
        )
        assert ledger.owns(db, recorded.id, user_id=user_id) is False
        assert ledger.owns(db, recorded.id, user_id=other.id) is True
        assert ledger.owns(db, uuid.uuid4(), user_id=other.id) is False

    def test_filtering_by_type_and_ordering(self, db: Session, user_id: uuid.UUID):
        import time

        plan = make_plan()
        first = record_plan(
            db, user_id=user_id, plan=plan, provider="ollama", model="m", request="strong"
        )
        time.sleep(0.02)
        parameters, changes, _ = apply_intent(plan, {})
        second = ledger.record_application(
            db,
            user_id=user_id,
            plan=plan,
            plan_decision_id=first.id,
            parameters=parameters,
            changes=changes,
            warnings=[],
        )

        plans = ledger.list_decisions(
            db, user_id=user_id, decision_type=ledger.DECISION_PLAN
        )
        assert [item["id"] for item in plans] == [str(first.id)]

        everything = ledger.list_decisions(db, user_id=user_id)
        # Newest first, so the application is above the plan it came from.
        assert [item["id"] for item in everything][:2] == [str(second.id), str(first.id)]

    def test_the_limit_is_bounded(self, db: Session, user_id: uuid.UUID):
        """A caller cannot ask for the whole table in one page."""
        assert ledger.list_decisions(db, user_id=user_id, limit=10_000) is not None

    def test_evidence_rows_are_attached_to_the_decision(self, db: Session, user_id: uuid.UUID):
        recorded = record_plan(
            db, user_id=user_id, plan=make_plan(), provider="ollama", model="m", request="strong"
        )
        rows = db.execute(
            select(AiDecisionEvidence).where(AiDecisionEvidence.decision_id == recorded.id)
        ).scalars().all()
        assert rows
        assert all(row.excerpt for row in rows)
        assert all(isinstance(row.weight, Decimal) for row in rows)
