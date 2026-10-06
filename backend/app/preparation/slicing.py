"""From a validated print plan to a downloadable, validated artifact.

    preparation attempt
        -> validate_plan        fresh, deterministic, no tokens spent
        -> locked + overrides   what the plan is allowed to change
        -> run_pipeline         PrintConfiguration -> SlicerEngineAdapter -> G-code
        -> OutputFormatterRegistry   gcode / gx, only what the profile declares
        -> output validation    bytes that fail are never stored
        -> artifact

This module owns the decisions; the router owns authentication and response
mapping, and the preparation engine owns analysis/planning/validation. Nothing
here re-implements slicing: the toolpath comes from `run_pipeline`, the same
function `POST /slice` calls, and the file format comes from the output
registry, the same one every other download path uses.

Three rules it enforces on behalf of the API:

* an attempt that is not `completed` and passing never reaches a slicer;
* the checklist is re-run against a freshly measured analysis first, so a
  profile or geometry change between plan and slice cannot be sailed through;
* a requested output format is checked for declaration *and* availability
  before any slicing time is spent, and a format that cannot be produced fails
  the request instead of quietly degrading to G-code.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.assets import ModelVersion
from app.models.catalog import MachineProfile, MaterialProfile, Printer
from app.models.identity import AuditLog
from app.models.preparation import PreparationStatus, PrintPreparationJob
from app.models.slicing import GeneratedFile, JobStatus, SliceJob
from app.preparation import jobs as preparation
from app.preparation.schemas import AiPrintPlan
from app.preparation.validators import ValidationOutcome, validate_plan
from app.slicing.job_factory import create_slice_job
from app.slicing.output import artifacts as output_artifacts
from app.slicing.output import service as output_service
from app.slicing.pipeline import run_pipeline

log = get_logger(__name__)

__all__ = [
    "artifact_for_id",
    "artifacts_for",
    "assert_ready",
    "downloadable",
    "fresh_outcome",
    "resolve_inputs",
    "slice_job_for",
    "slice_prepared",
]


# --------------------------------------------------------------------------- #
# readiness
# --------------------------------------------------------------------------- #


def assert_ready(prep_job: PrintPreparationJob) -> None:
    """Refuse any attempt that is not an approved plan, by name.

    Each refusal carries its own code because the client's remedy differs: a
    rejected plan is re-planned, a failed attempt is retried, a still-running
    attempt is waited for.
    """
    status = prep_job.status

    if status is PreparationStatus.completed and prep_job.passed:
        return

    if status is PreparationStatus.rejected or (
        status is PreparationStatus.completed and not prep_job.passed
    ):
        failed = [
            check.get("code")
            for check in (prep_job.validation or {}).get("checks", [])
            if check.get("status") == "FAILED"
        ]
        raise ConflictError(
            "This preparation was rejected by deterministic validation and "
            "cannot be sliced. Re-plan it with the failing checks addressed.",
            code="PREPARATION_REJECTED",
            details={
                "job_id": str(prep_job.id),
                "attempt": prep_job.attempt,
                "rejected_reason": prep_job.rejected_reason,
                "failed_checks": failed,
                "can_replan": (prep_job.validation or {}).get("can_replan", False),
            },
        )

    if status is PreparationStatus.failed:
        raise ConflictError(
            "This preparation attempt failed before a plan was produced.",
            code="PREPARATION_FAILED",
            details={
                "job_id": str(prep_job.id),
                "attempt": prep_job.attempt,
                "error": (prep_job.validation or {}).get("error", {}),
            },
        )

    raise ConflictError(
        "This preparation job has not finished yet.",
        code="PREPARATION_NOT_READY",
        details={
            "job_id": str(prep_job.id),
            "status": status.value if status else None,
        },
    )


def resolve_inputs(
    db: Session, prep_job: PrintPreparationJob
) -> tuple[ModelVersion, Printer, MaterialProfile, MachineProfile]:
    """The four rows this attempt was planned against, re-resolved now."""
    return preparation.resolve(
        db,
        user_id=prep_job.user_id,
        model_version_id=prep_job.model_version_id,
        printer_id=prep_job.printer_id,
        material_profile_id=prep_job.material_profile_id,
    )


def _model_is_current(db: Session, prep_job: PrintPreparationJob) -> bool:
    version = db.get(ModelVersion, prep_job.model_version_id)
    if version is None:
        return False
    from app.services.models_service import active_blob

    blob = active_blob(db, version)
    return prep_job.input_sha256 == blob.sha256


def fresh_outcome(
    db: Session,
    prep_job: PrintPreparationJob,
    *,
    version: ModelVersion,
    printer: Printer,
    material_profile: MaterialProfile,
    machine_profile: MachineProfile,
) -> ValidationOutcome:
    """Re-run the checklist against current measurements. No model is called.

    This is the same `validate_plan` the planner ran, plus `finalise()` — the
    planner's own last-attempt rule — so what passes here is exactly what
    would have passed at plan time, measured again against today's profiles.
    """
    if not prep_job.plan:
        raise ConflictError(
            "This preparation has no plan to slice.",
            code="PREPARATION_NOT_READY",
            details={"job_id": str(prep_job.id)},
        )

    analysis = preparation.build_analysis(
        db,
        version=version,
        printer=printer,
        machine_profile=machine_profile,
        material_profile=material_profile,
    )
    outcome = validate_plan(AiPrintPlan.model_validate(prep_job.plan), analysis).finalise()

    if not outcome.passed:
        raise ConflictError(
            "The plan no longer passes deterministic validation against the "
            "current profiles, so it will not be sliced.",
            code="PREPARATION_REJECTED",
            details={
                "job_id": str(prep_job.id),
                "failed_checks": [
                    {"category": c.category, "code": c.code, "reason": c.reason}
                    for c in outcome.failed_checks
                ],
            },
        )
    return outcome


# --------------------------------------------------------------------------- #
# slicing
# --------------------------------------------------------------------------- #


def slice_prepared(
    db: Session,
    *,
    prep_job: PrintPreparationJob,
    objective: str = "balanced",
    output_format: str | None = None,
) -> dict[str, Any]:
    """Slice an approved attempt end to end and return what was produced."""
    assert_ready(prep_job)
    version, printer, material_profile, machine_profile = resolve_inputs(db, prep_job)
    if not _model_is_current(db, prep_job):
        raise ConflictError(
            "The model changed after this preparation. Prepare again against the transformed geometry.",
            code="PREPARATION_STALE",
            details={"job_id": str(prep_job.id), "model_version_id": str(version.id)},
        )

    # Checked before a single measurement or slice happens: a request this
    # deployment cannot satisfy must fail on the request, not on the result.
    requested_key = None
    if output_format:
        requested_key = output_service.require_usable(
            machine_profile, output_format
        ).key

    outcome = fresh_outcome(
        db,
        prep_job,
        version=version,
        printer=printer,
        material_profile=material_profile,
        machine_profile=machine_profile,
    )

    job = create_slice_job(
        db,
        user_id=prep_job.user_id,
        version=version,
        printer=printer,
        material_profile=material_profile,
        machine_profile=machine_profile,
        preparation_job_id=prep_job.id,
    )
    db.add(
        AuditLog(
            actor_user_id=prep_job.user_id,
            action="preparation.sliced",
            object_type="slice_job",
            object_id=job.id,
            after={
                "preparation_job_id": str(prep_job.id),
                "attempt": prep_job.attempt,
                "objective": objective,
                "requested_format": requested_key,
            },
        )
    )

    result = run_pipeline(
        db,
        job=job,
        version=version,
        printer=printer,
        material_profile=material_profile,
        objective=objective,
        locked=outcome.locked,
        overrides=outcome.overrides,
    )

    # A blocked toolpath is *reported*, not thrown: the request-scoped session
    # rolls back on any raised error, which would erase the very audit trail
    # that explains why the bytes were refused. The job stays queryable, the
    # file stays blocked, `outputs` is empty because no blocked file may be
    # served, and no format is derived from a toolpath that failed validation.
    if result.status is not JobStatus.blocked and requested_key:
        output_service.generate_output(
            db,
            job=job,
            machine_profile=machine_profile,
            format_key=requested_key,
            limits=preparation.limits_for(printer, machine_profile, material_profile),
        )

    log.info(
        "preparation.sliced",
        preparation_job_id=str(prep_job.id),
        slice_job_id=str(job.id),
        format=requested_key,
        objective=objective,
    )
    return {
        "slice_job": job,
        "version": version,
        "printer": printer,
        "material_profile": material_profile,
        "machine_profile": machine_profile,
        "objective": objective,
        "requested_format": requested_key,
        "configuration": (
            result.configuration.to_dict() if result.configuration else None
        ),
        "engine": result.engine,
        "engine_version": result.engine_version,
        "outputs": artifacts_for(db, prep_job),
    }


# --------------------------------------------------------------------------- #
# artifacts
# --------------------------------------------------------------------------- #


def slice_job_for(db: Session, prep_job: PrintPreparationJob) -> SliceJob | None:
    """The newest slice job produced from this attempt, if it has been sliced.

    The link is the `preparation_job_id` written when the slice was requested,
    so an attempt can never be shown another attempt's toolpath.
    """
    return db.execute(
        select(SliceJob)
        .where(SliceJob.preparation_job_id == prep_job.id)
        .order_by(SliceJob.queued_at.desc())
    ).scalars().first()


def artifacts_for(
    db: Session, prep_job: PrintPreparationJob
) -> list[tuple[GeneratedFile, output_artifacts.OutputArtifact]]:
    """Every artifact of this attempt that is stored *and* validated.

    Blocked files are skipped entirely: a listing that offered a file the
    download endpoint would refuse is a lie about what the operator can use.
    """
    if not _model_is_current(db, prep_job):
        return []
    job = slice_job_for(db, prep_job)
    if job is None:
        return []

    machine_profile = db.get(MachineProfile, job.machine_profile_id)
    rows = db.execute(
        select(GeneratedFile)
        .where(
            GeneratedFile.slice_job_id == job.id,
            GeneratedFile.is_blocked.is_(False),
        )
        .order_by(GeneratedFile.created_at)
    ).scalars()

    collected: list[tuple[GeneratedFile, output_artifacts.OutputArtifact]] = []
    for generated in rows:
        artifact = output_service.artifact_for(
            db, generated, job=job, machine_profile=machine_profile
        )
        if (artifact.validation or {}).get("status") == "passed":
            collected.append((generated, artifact))
    return collected


def downloadable(
    generated: GeneratedFile, artifact: output_artifacts.OutputArtifact
) -> None:
    """Raise unless this exact file may leave the server."""
    if generated.is_blocked:
        raise ConflictError(
            "This file was blocked by a mandatory safety validation and cannot "
            "be downloaded.",
            code="OUTPUT_NOT_VALIDATED",
            details={"reason": generated.blocked_reason},
        )
    if (artifact.validation or {}).get("status") != "passed":
        raise ConflictError(
            "This file has not passed output validation and cannot be "
            "downloaded.",
            code="OUTPUT_NOT_VALIDATED",
            details={"validation": (artifact.validation or {}).get("status")},
        )


def artifact_for_id(
    db: Session, prep_job: PrintPreparationJob, artifact_id: Any
) -> tuple[GeneratedFile, output_artifacts.OutputArtifact]:
    """Resolve an artifact *through* the preparation job, never by id alone."""
    if not _model_is_current(db, prep_job):
        raise ConflictError(
            "The model changed after this preparation; its artifacts are stale.",
            code="PREPARATION_STALE",
            details={"job_id": str(prep_job.id)},
        )
    job = slice_job_for(db, prep_job)
    if job is None:
        raise NotFoundError("The requested resource does not exist.")

    generated = db.execute(
        select(GeneratedFile).where(
            GeneratedFile.id == artifact_id,
            GeneratedFile.slice_job_id == job.id,
        )
    ).scalar_one_or_none()
    if generated is None:
        raise NotFoundError("The requested resource does not exist.")

    machine_profile = db.get(MachineProfile, job.machine_profile_id)
    artifact = output_service.artifact_for(
        db, generated, job=job, machine_profile=machine_profile
    )
    downloadable(generated, artifact)
    return generated, artifact
