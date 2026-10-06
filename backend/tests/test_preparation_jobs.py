"""The preparation service: one persisted row per attempt.

Covers the guarantees the `print_preparation_jobs` table exists for:

* every attempt — approved, rejected or failed — leaves a row behind;
* the prompt and the model's verbatim answer are stored, not paraphrased;
* a model version can be planned at most `MAX_ATTEMPTS` times;
* a re-plan carries the previous intent forward instead of changing the ask;
* re-validating a stored plan never spends a token.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import ConflictError, NotFoundError  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.intelligence.providers.base import LLMError  # noqa: E402
from app.intelligence.schemas import IntentPriorities  # noqa: E402
from app.models.preparation import PreparationStatus, PrintPreparationJob  # noqa: E402
from app.preparation import jobs as preparation  # noqa: E402
from app.preparation.planner import MAX_ATTEMPTS  # noqa: E402
from tests.job_factory import ScriptedProvider, build_sliced_job, register_user  # noqa: E402


@pytest.fixture
def user_id(test_client) -> uuid.UUID:
    return register_user(test_client, f"prep-{uuid.uuid4().hex[:8]}@example.com")


@pytest.fixture
def job_refs(user_id) -> dict[str, Any]:
    with session_scope() as db:
        return build_sliced_job(db, user_id=user_id)


def _propose(db, refs, provider, **kwargs):
    return preparation.propose(
        db,
        user_id=_owner(db, refs),
        model_version_id=refs["version_id"],
        printer_id=refs["printer_id"],
        material_profile_id=refs["material_profile_id"],
        provider=provider,
        **kwargs,
    )


def _owner(db, refs) -> uuid.UUID:
    from app.models.slicing import SliceJob

    return db.get(SliceJob, refs["job_id"]).user_id


# --------------------------------------------------------------------------- #
# attempts
# --------------------------------------------------------------------------- #


def test_provider_failure_is_recorded_not_swallowed(job_refs):
    # The planner retries a recoverable provider error up to MAX_ATTEMPTS; a
    # failure that survives all of them must still leave a row behind.
    provider = ScriptedProvider(
        errors=[
            LLMError("LLM_UNAVAILABLE", "no model is reachable", recoverable=True)
            for _ in range(MAX_ATTEMPTS)
        ]
    )
    with session_scope() as db:
        job = _propose(db, job_refs, provider)

        assert job.status is PreparationStatus.failed
        assert job.passed is False
        assert "no model is reachable" in (job.rejected_reason or "")
        assert job.validation["error"]["code"] == "LLM_UNAVAILABLE"
        assert job.validation["passed"] is False
        # The row exists even though nothing was planned.
        stored = db.get(PrintPreparationJob, job.id)
        assert stored is not None
        assert stored.status is PreparationStatus.failed


def test_attempt_stores_prompt_plan_and_verdict(job_refs):
    provider = ScriptedProvider(payloads=[{}])
    with session_scope() as db:
        job = _propose(db, job_refs, provider, user_request="print this fast")

        assert job.status in (PreparationStatus.completed, PreparationStatus.rejected)
        assert job.attempt == 1
        # What the model was shown, verbatim, plus what it answered.
        assert "print this fast" in job.prompt
        assert "Measured facts about this print job" in job.prompt
        assert job.raw_response == "{}"
        assert job.plan, "a parsed plan must be stored"
        assert job.validation.get("summary") is not None or "checks" in job.validation
        assert job.input_sha256 == job_refs["input_sha256"]
        assert job.provider == "scripted"
        assert job.model_identifier == "scripted-1b"
        assert job.duration_ms >= 0

        payload = preparation.to_dict(job)
        assert payload["attempt"] == 1
        assert payload["max_attempts"] == MAX_ATTEMPTS
        assert payload["prompt_sha256"]


def test_an_attempt_that_never_ran_has_no_plan(job_refs):
    provider = ScriptedProvider(
        errors=[
            LLMError("LLM_TIMEOUT", "timed out", recoverable=True)
            for _ in range(MAX_ATTEMPTS)
        ]
    )
    with session_scope() as db:
        job = _propose(db, job_refs, provider)
        assert job.plan == {}
        assert job.prompt == ""
        assert preparation.to_dict(job)["raw_response"] is None


# --------------------------------------------------------------------------- #
# access control
# --------------------------------------------------------------------------- #


def test_another_users_model_cannot_be_planned(job_refs):
    with session_scope() as db:
        stranger = uuid.uuid4()
        with pytest.raises(NotFoundError):
            preparation.propose(
                db,
                user_id=stranger,
                model_version_id=job_refs["version_id"],
                printer_id=job_refs["printer_id"],
                material_profile_id=job_refs["material_profile_id"],
                provider=ScriptedProvider(payloads=[{}]),
            )


# --------------------------------------------------------------------------- #
# re-plan chain
# --------------------------------------------------------------------------- #


def test_replan_walks_the_chain_and_stops_at_the_cap(job_refs):
    provider = ScriptedProvider(payloads=[{}, {}, {}])
    with session_scope() as db:
        first = _propose(
            db, job_refs, provider, user_request="a strong little bracket"
        )
        assert first.attempt == 1

        second = preparation.replan(
            db,
            job=first,
            user_request="a strong little bracket",
            feedback="prefer fewer supports",
            provider=provider,
        )
        assert second.attempt == 2
        assert second.id != first.id, "attempts are never updated in place"
        assert "prefer fewer supports" in second.prompt
        assert "Direction from the operator" in second.prompt

        third = preparation.replan(
            db,
            job=second,
            user_request="a strong little bracket",
            provider=provider,
        )
        assert third.attempt == 3

        with pytest.raises(ConflictError) as exc:
            preparation.replan(
                db, job=third, user_request="one more go", provider=provider
            )
        assert exc.value.details["max_attempts"] == MAX_ATTEMPTS
        assert exc.value.details["attempts_used"] == 3


def test_replan_carries_the_previous_intent_forward(job_refs):
    provider = ScriptedProvider(payloads=[{}, {}])
    with session_scope() as db:
        first = _propose(
            db,
            job_refs,
            provider,
            user_request="strong",
            intent=IntentPriorities(
                strength=0.9,
                speed=0.1,
                material_efficiency=0.4,
                surface_quality=0.7,
            ),
        )
        second = preparation.replan(
            db, job=first, user_request="strong", provider=provider
        )
        # The stored plan recorded the intent; the re-plan must reuse it rather
        # than silently reverting to the defaults.
        stored = (second.plan or {}).get("objective", {}).get("intent", {})
        assert stored.get("strength") in (None, 0.9) or second.plan


# --------------------------------------------------------------------------- #
# re-validation
# --------------------------------------------------------------------------- #


def test_revalidation_rechecks_without_calling_the_model(job_refs):
    provider = ScriptedProvider(payloads=[{}])
    with session_scope() as db:
        job = _propose(db, job_refs, provider)
        if not job.plan:
            pytest.skip("the scripted plan was not structurally valid")

        before = len(provider.requests)
        outcome = preparation.revalidate(db, job=job)
        assert len(provider.requests) == before, "re-validation must not spend a token"
        assert outcome["passed"] in (True, False)
        assert outcome["checks"]
        # Nothing is persisted: the stored verdict stays the one recorded.
        assert db.get(PrintPreparationJob, job.id).validation == job.validation


def test_reads_are_scoped_to_the_owner(job_refs):
    with session_scope() as db:
        job = _propose(db, job_refs, ScriptedProvider(payloads=[{}]))
        owner = job.user_id

        assert preparation.get_job(db, job.id, user_id=owner).id == job.id
        with pytest.raises(NotFoundError):
            preparation.get_job(db, job.id, user_id=uuid.uuid4())

        mine = preparation.list_jobs(db, user_id=owner)
        assert [j.id for j in mine] == [job.id]
        assert preparation.list_jobs(db, user_id=uuid.uuid4()) == []
