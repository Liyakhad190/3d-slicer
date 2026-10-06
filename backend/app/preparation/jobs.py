"""Service layer for AI print preparation: propose, validate, re-plan.

This module is the only place that turns a stored model into a persisted
attempt. It owns no new tables and no new persistence model — every attempt is
one row of :class:`~app.models.preparation.PrintPreparationJob`, written by
:class:`PreparationStatus`'s rules and never updated afterwards.

The flow it sequences is deliberately one-directional:

    stored model blob
        -> materialise to a temp file (trimesh needs a path)
        -> MachineLimits.from_profile      (machine + material, one source)
        -> run_pre_analysis                (measure geometry against that machine)
        -> propose_print_plan              (model proposes, validators judge)
        -> persist attempt row             (prompt, raw answer, plan, verdict)

Nothing here slices. A plan that fails validation is stored as `rejected` with
the failing checks attached, so the caller can show *which* check refused it;
a plan that passes is stored as `completed`. A provider failure is stored as
`failed` rather than swallowed, so the audit trail has a row for it too.

The service never invents a fallback: no default parameters, no silent
substitution of a machine or material, no retry beyond the planner's own
:class:`~app.preparation.planner.MAX_ATTEMPTS` budget.
"""

from __future__ import annotations

import dataclasses
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.intelligence.providers.base import LLMError
from app.intelligence.schemas import IntentPriorities
from app.models.assets import Blob, Model, ModelVersion
from app.models.catalog import MachineProfile, MaterialProfile, Printer
from app.models.preparation import PreparationStatus, PrintPreparationJob
from app.preparation.analysis import PreAnalysis, run_pre_analysis
from app.preparation.planner import (
    MAX_ATTEMPTS,
    STATUS_APPROVED,
    PrintPlanResult,
    propose_print_plan,
)
from app.services import models_service, storage
from app.slicing.registry import get_registry
from app.slicing.validator import MachineLimits

log = get_logger(__name__)

__all__ = [
    "MAX_ATTEMPTS",
    "build_analysis",
    "resolve",
    "get_job",
    "list_jobs",
    "propose",
    "replan",
    "revalidate",
    "get_orientations",
    "apply_orientation",
    "limits_for",
    "material_context",
    "to_dict",
]


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #


def resolve(
    db: Session,
    *,
    user_id: uuid.UUID,
    model_version_id: uuid.UUID,
    printer_id: uuid.UUID,
    material_profile_id: uuid.UUID,
) -> tuple[ModelVersion, Printer, MaterialProfile, MachineProfile]:
    """Fetch the four inputs, refusing anything that is not this user's."""
    version = db.get(ModelVersion, model_version_id)
    if version is None:
        raise NotFoundError("The requested model version does not exist.")

    model = db.get(Model, version.model_id)
    if model is None or model.user_id != user_id:
        # The API layer has richer helpers; this is the service-level backstop
        # so a guessed id can never be analysed.
        raise NotFoundError("The requested model version does not exist.")

    printer = db.get(Printer, printer_id)
    if printer is None or printer.archived_at is not None:
        raise NotFoundError("The requested printer does not exist.")
    if getattr(printer, "user_id", None) not in (None, user_id):
        raise NotFoundError("The requested printer does not exist.")

    material_profile = db.get(MaterialProfile, material_profile_id)
    if material_profile is None:
        raise NotFoundError("The requested material profile does not exist.")

    machine_profile = db.get(MachineProfile, printer.machine_profile_id)
    if machine_profile is None:  # pragma: no cover - FK guarantees presence
        raise NotFoundError("The printer has no machine profile attached.")

    return version, printer, material_profile, machine_profile


def limits_for(
    printer: Printer,
    machine_profile: MachineProfile,
    material_profile: MaterialProfile,
) -> MachineLimits:
    """The one set of limits every step in this module measures against."""
    return MachineLimits.from_profile(
        machine_profile,
        nozzle_diameter_mm=float(printer.nozzle_diameter_mm),
        material_profile=material_profile,
    )


def material_context(material_profile: MaterialProfile) -> dict[str, Any]:
    """Render a material profile as the fact block the prompt carries."""
    material = getattr(material_profile, "material", None)
    return {
        "key": getattr(material, "key", None) or str(material_profile.material_id),
        "name": getattr(material, "display_name", None) or getattr(material, "name", None),
        "vendor": material_profile.vendor,
        "brand_sku": material_profile.brand_sku,
        "diameter_mm": float(material_profile.material_diameter or 0) or None,
        "nozzle_temp": material_profile.nozzle_temp,
        "bed_temp": material_profile.bed_temp,
        "print_speed": material_profile.print_speed,
        "layer_height_range": material_profile.layer_height_range,
        "max_volumetric_flow_mm3s": (
            float(material_profile.max_volumetric_flow_mm3s)
            if material_profile.max_volumetric_flow_mm3s is not None
            else None
        ),
        "requires_enclosure": material_profile.requires_enclosure,
        "adhesion": material_profile.adhesion,
        "abrasiveness": getattr(material_profile.abrasiveness, "value", None),
    }


def _adapter() -> Any:
    """The engine that will actually slice this job.

    Slicer selection itself stays behind `SlicerRegistry.select()` in the
    pipeline, where a full `PrintConfiguration` exists. Preparation only needs
    *an* engine to probe capabilities against, and refusing when there is none
    is the correct outcome — a plan written for an engine that cannot run is
    worthless.
    """
    available = get_registry().available()
    if not available:
        from app.core.errors import InsufficientInformationError

        raise InsufficientInformationError(
            "No slicing engine is available, so engine capabilities cannot be "
            "determined and no print plan can be validated against one.",
            details={"engines": get_registry().describe()},
        )
    return available[0]


def _trimesh_file_type(blob: Blob) -> str | None:
    """Same mapping the mesh analyzer uses, so both read the file alike."""
    from app.services.formats import ModelFormat
    from app.services.models_service import _TRIMESH_FILE_TYPE

    try:
        return _TRIMESH_FILE_TYPE.get(ModelFormat(blob.detected_format))
    except ValueError:  # pragma: no cover - unknown stored format
        return None


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #


@contextmanager
def _materialised(blob: Blob) -> Iterator[Path]:
    """Hand the stored bytes to a real file for the duration of the block.

    trimesh needs a path for the format readers (STL/OBJ/3MF), and the blob
    store keeps bytes, not paths. The file is removed on exit; the in-memory
    mesh it produced is carried on the analysis instead.
    """
    suffix = Path(blob.storage_path).suffix or ".stl"
    with tempfile.TemporaryDirectory(prefix="prepare-") as tmp:
        path = Path(tmp) / f"model{suffix}"
        path.write_bytes(storage.read_blob(blob))
        yield path


def build_analysis(
    db: Session,
    *,
    version: ModelVersion,
    printer: Printer,
    machine_profile: MachineProfile,
    material_profile: MaterialProfile,
    no_opening_mm: float = 0.4,
    max_overhang_deg: float = 45.0,
    adapter: Any | None = None,
    machine_name: str | None = None,
) -> PreAnalysis:
    """Measure the model against *this* machine and *this* engine."""
    blob = models_service.active_blob(db, version)

    limits = limits_for(printer, machine_profile, material_profile)
    engine = adapter or _adapter()

    with _materialised(blob) as path:
        analysis = run_pre_analysis(
            path,
            limits=limits,
            adapter=engine,
            input_sha256=blob.sha256,
            no_opening_mm=no_opening_mm,
            max_overhang_deg=max_overhang_deg,
            file_type=_trimesh_file_type(blob),
        )

    machine = getattr(machine_profile, "machine", None)
    label = machine_name or (
        f"{machine.vendor} {machine.model}" if machine else "machine"
    )
    # PreAnalysis is immutable by design: the facts it carries are measured
    # once and must not drift, so the display name is attached by replacement.
    return dataclasses.replace(analysis, machine_name=label)


# --------------------------------------------------------------------------- #
# attempts
# --------------------------------------------------------------------------- #


def _new_job(
    db: Session,
    *,
    user_id: uuid.UUID,
    version: ModelVersion,
    printer: Printer,
    material_profile: MaterialProfile,
    attempt: int,
) -> PrintPreparationJob:
    job = PrintPreparationJob(
        user_id=user_id,
        model_version_id=version.id,
        printer_id=printer.id,
        material_profile_id=material_profile.id,
        attempt=attempt,
        status=PreparationStatus.queued,
    )
    db.add(job)
    db.flush()
    return job


def propose(
    db: Session,
    *,
    user_id: uuid.UUID,
    model_version_id: uuid.UUID,
    printer_id: uuid.UUID,
    material_profile_id: uuid.UUID,
    objective: str = "balanced",
    user_request: str = "",
    intent: IntentPriorities | None = None,
    provider: Any | None = None,
    settings: Any | None = None,
    attempt: int = 1,
    no_opening_mm: float = 0.4,
    max_overhang_deg: float = 45.0,
) -> PrintPreparationJob:
    """Run one full attempt and persist its result.

    Returns the attempt row in every case. When the provider itself fails the
    row is written with `status = failed` and the error in `rejected_reason`
    (plus `validation["error"]`), because an attempt that produced no plan is
    still an attempt worth auditing; the caller decides how to surface it.
    """
    version, printer, material_profile, machine_profile = resolve(
        db,
        user_id=user_id,
        model_version_id=model_version_id,
        printer_id=printer_id,
        material_profile_id=material_profile_id,
    )

    job = _new_job(
        db,
        user_id=user_id,
        version=version,
        printer=printer,
        material_profile=material_profile,
        attempt=attempt,
    )
    job.status = PreparationStatus.analysing
    db.flush()

    started = time.perf_counter()
    try:
        analysis = build_analysis(
            db,
            version=version,
            printer=printer,
            machine_profile=machine_profile,
            material_profile=material_profile,
            no_opening_mm=no_opening_mm,
            max_overhang_deg=max_overhang_deg,
        )
    except Exception as exc:
        _record_failure(db, job, exc, elapsed_ms=started)
        raise

    job.input_sha256 = analysis.input_sha256
    job.status = PreparationStatus.proposing
    db.flush()

    try:
        result = propose_print_plan(
            analysis,
            provider=provider,
            settings=settings,
            intent=intent,
            objective=objective,
            material=material_context(material_profile),
            user_request=user_request,
        )
    except LLMError as exc:
        _record_failure(db, job, exc, elapsed_ms=started)
        return job

    _record_result(db, job, result, elapsed_ms=started)
    return job


def replan(
    db: Session,
    *,
    job: PrintPreparationJob,
    user_request: str,
    feedback: str = "",
    objective: str = "balanced",
    intent: IntentPriorities | None = None,
    provider: Any | None = None,
    settings: Any | None = None,
    no_opening_mm: float = 0.4,
    max_overhang_deg: float = 45.0,
) -> PrintPreparationJob:
    """Start the next attempt in this job's chain.

    The chain is bounded by the same :data:`MAX_ATTEMPTS` that bounds the
    planner's internal loop, so a single model version can never be planned
    more than three times no matter how many times the operator asks.

    `user_request` is supplied by the caller rather than read back out of the
    stored prompt: the original request is a UI concern, and re-parsing our own
    rendered prompt would be a fragile way to recover it. When `intent` is not
    given it is carried forward from the previous plan, so a re-plan corrects
    what was refused instead of quietly changing what was asked for.
    """
    last = db.execute(
        select(PrintPreparationJob)
        .where(PrintPreparationJob.model_version_id == job.model_version_id)
        .order_by(PrintPreparationJob.attempt.desc())
    ).scalars().first()
    next_attempt = (last.attempt if last else job.attempt) + 1
    if next_attempt > MAX_ATTEMPTS:
        raise ConflictError(
            f"This print plan has already used all {MAX_ATTEMPTS} attempts.",
            details={
                "attempts_used": next_attempt - 1,
                "max_attempts": MAX_ATTEMPTS,
                "model_version_id": str(job.model_version_id),
            },
        )

    if intent is None:
        intent = _intent_from_plan(job.plan)
    if feedback:
        user_request = f"{user_request}\nDirection from the operator: {feedback}".strip()

    return propose(
        db,
        user_id=job.user_id,
        model_version_id=job.model_version_id,
        printer_id=job.printer_id,
        material_profile_id=job.material_profile_id,
        objective=objective,
        user_request=user_request,
        intent=intent,
        provider=provider,
        settings=settings,
        attempt=next_attempt,
        no_opening_mm=no_opening_mm,
        max_overhang_deg=max_overhang_deg,
    )


def _intent_from_plan(plan: dict[str, Any] | None) -> IntentPriorities | None:
    """Recover the intent a stored plan recorded, if it recorded one."""
    if not plan:
        return None
    raw = ((plan.get("objective") or {}).get("intent")) or {}
    if not isinstance(raw, dict):
        return None
    fields = set(IntentPriorities.model_fields)
    known = {k: v for k, v in raw.items() if k in fields}
    if not known:
        return None
    try:
        return IntentPriorities(**known)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def revalidate(
    db: Session,
    *,
    job: PrintPreparationJob,
    no_opening_mm: float = 0.4,
    max_overhang_deg: float = 45.0,
) -> dict[str, Any]:
    """Re-run the checklist against a freshly measured analysis. No model.

    Used to answer "would this plan still pass?" after a profile change, and
    to let the API re-check without spending a token. Nothing is persisted —
    the stored verdict is the one recorded at the time the plan was produced.
    """
    from app.preparation.schemas import AiPrintPlan
    from app.preparation.validators import validate_plan

    if not job.plan:
        raise NotFoundError("This attempt has no plan to validate.")

    version, printer, material_profile, machine_profile = resolve(
        db,
        user_id=job.user_id,
        model_version_id=job.model_version_id,
        printer_id=job.printer_id,
        material_profile_id=job.material_profile_id,
    )
    analysis = build_analysis(
        db,
        version=version,
        printer=printer,
        machine_profile=machine_profile,
        material_profile=material_profile,
        no_opening_mm=no_opening_mm,
        max_overhang_deg=max_overhang_deg,
    )
    plan = AiPrintPlan.model_validate(job.plan)
    return validate_plan(plan, analysis).to_dict()


def get_orientations(
    db: Session,
    *,
    job: PrintPreparationJob,
    no_opening_mm: float = 0.4,
    max_overhang_deg: float = 45.0,
) -> dict[str, Any]:
    """Return all deterministic orientation candidates for this preparation job."""
    version, printer, material_profile, machine_profile = resolve(
        db,
        user_id=job.user_id,
        model_version_id=job.model_version_id,
        printer_id=job.printer_id,
        material_profile_id=job.material_profile_id,
    )
    analysis = build_analysis(
        db,
        version=version,
        printer=printer,
        machine_profile=machine_profile,
        material_profile=material_profile,
        no_opening_mm=no_opening_mm,
        max_overhang_deg=max_overhang_deg,
    )
    return {
        "job_id": str(job.id),
        "recommended_candidate_id": analysis.recommended_candidate_id,
        "candidates": [c.to_dict() for c in analysis.candidates],
    }


def apply_orientation(
    db: Session,
    *,
    job: PrintPreparationJob,
    candidate_id: str,
    no_opening_mm: float = 0.4,
    max_overhang_deg: float = 45.0,
) -> tuple[PrintPreparationJob, Any]:
    """Select a deterministic candidate orientation, update the plan, and re-validate."""
    from app.preparation.orientation import verify_candidate
    from app.preparation.schemas import AiPrintPlan
    from app.preparation.validators import validate_plan

    version, printer, material_profile, machine_profile = resolve(
        db,
        user_id=job.user_id,
        model_version_id=job.model_version_id,
        printer_id=job.printer_id,
        material_profile_id=job.material_profile_id,
    )
    analysis = build_analysis(
        db,
        version=version,
        printer=printer,
        machine_profile=machine_profile,
        material_profile=material_profile,
        no_opening_mm=no_opening_mm,
        max_overhang_deg=max_overhang_deg,
    )

    candidate = verify_candidate(candidate_id, analysis.candidates)

    plan_data = dict(job.plan or {})
    orient_data = dict(plan_data.get("orientation") or {})
    orient_data["candidate_id"] = candidate.id
    orient_data["strategy"] = candidate.strategy
    orient_data["reason"] = f"Orientation selected: {candidate.id} ({candidate.strategy})"
    orient_data["evidence"] = candidate.evidence
    plan_data["orientation"] = orient_data

    job.plan = plan_data

    # Re-validate with the updated plan
    plan = AiPrintPlan.model_validate(plan_data)
    outcome = validate_plan(plan, analysis)
    job.validation = outcome.to_dict()
    job.status = (
        PreparationStatus.completed if outcome.passed else PreparationStatus.rejected
    )
    db.flush()
    return job, candidate


# --------------------------------------------------------------------------- #
# persistence helpers
# --------------------------------------------------------------------------- #


def _record_result(
    db: Session,
    job: PrintPreparationJob,
    result: PrintPlanResult,
    *,
    elapsed_ms: float,
) -> None:
    job.status = PreparationStatus.proposing
    job.prompt = result.prompt_text
    job.raw_response = result.raw_response
    job.plan = result.plan.model_dump()
    job.validation = result.outcome.to_dict()
    job.normalisations = [n.to_dict() for n in result.normalisations]
    job.rejected_reason = result.rejected_reason
    job.model_identifier = result.model or None
    job.provider = result.provider or None
    job.duration_ms = int(
        result.duration_ms or ((time.perf_counter() - elapsed_ms) * 1000)
    )
    job.status = (
        PreparationStatus.completed
        if result.approved
        else PreparationStatus.rejected
    )
    if result.approved:
        try:
            from app.intelligence import ledger
            from app.intelligence.schemas import LLMPlan

            llm_plan = LLMPlan(
                intent=result.plan.objective.intent,
                support_preference="required" if result.plan.supports.enabled else "allowed",
                orientation_preference="optimized",
                rationale=result.plan.summary or "Print preparation plan",
                confidence=result.plan.confidence,
            )
            ledger.record_plan(
                db,
                user_id=job.user_id,
                plan=llm_plan,
                provider=job.provider or "unknown",
                model=job.model_identifier or "unknown",
                request=job.prompt[:2000] if job.prompt else "AI print preparation request",
                prompt_hash=result.prompt_hash or None,
                normalisations=result.normalisations,
                model_version_id=job.model_version_id,
            )
        except Exception as exc:
            log.warning("preparation.ledger_record_failed", error=str(exc))
    db.flush()
    log.info(
        "preparation.attempt_recorded",
        job_id=str(job.id),
        attempt=job.attempt,
        status=job.status.value,
        approved=result.approved,
        attempts=result.attempts,
    )


def _record_failure(
    db: Session,
    job: PrintPreparationJob,
    exc: Exception,
    *,
    elapsed_ms: float,
) -> None:
    """Persist a failed attempt. The error is data, not an exception here."""
    job.status = PreparationStatus.failed
    job.rejected_reason = str(exc)
    code = getattr(exc, "code", None)
    details = getattr(exc, "details", None)
    job.validation = {
        "passed": False,
        "error": {
            "type": type(exc).__name__,
            "code": code,
            "message": str(exc),
            "details": details or {},
        },
    }
    job.duration_ms = int((time.perf_counter() - elapsed_ms) * 1000)
    db.flush()
    log.warning(
        "preparation.attempt_failed",
        job_id=str(job.id),
        attempt=job.attempt,
        error=type(exc).__name__,
        code=code,
    )


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


def get_job(db: Session, job_id: uuid.UUID, *, user_id: uuid.UUID) -> PrintPreparationJob:
    job = db.get(PrintPreparationJob, job_id)
    if job is None or job.user_id != user_id:
        raise NotFoundError("The requested preparation job does not exist.")
    return job


def list_jobs(
    db: Session,
    *,
    user_id: uuid.UUID,
    model_version_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[PrintPreparationJob]:
    stmt = select(PrintPreparationJob).where(PrintPreparationJob.user_id == user_id)
    if model_version_id is not None:
        stmt = stmt.where(PrintPreparationJob.model_version_id == model_version_id)
    if status:
        stmt = stmt.where(PrintPreparationJob.status == PreparationStatus(status))
    stmt = stmt.order_by(PrintPreparationJob.created_at.desc()).limit(limit)
    return list(db.execute(stmt).scalars())


def to_dict(job: PrintPreparationJob) -> dict[str, Any]:
    """API-shaped payload. Everything the row recorded, nothing derived."""
    return {
        "id": str(job.id),
        "model_version_id": str(job.model_version_id),
        "printer_id": str(job.printer_id),
        "material_profile_id": str(job.material_profile_id),
        "attempt": job.attempt,
        "status": job.status.value if job.status else None,
        "passed": job.passed,
        "prompt": job.prompt,
        "prompt_sha256": _sha(job.prompt),
        "raw_response": job.raw_response,
        "plan": job.plan,
        "validation": job.validation,
        "normalisations": job.normalisations,
        "rejected_reason": job.rejected_reason,
        "input_sha256": job.input_sha256,
        "model_identifier": job.model_identifier,
        "provider": job.provider,
        "duration_ms": job.duration_ms,
        "max_attempts": MAX_ATTEMPTS,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def _sha(text: str) -> str:
    import hashlib

    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
