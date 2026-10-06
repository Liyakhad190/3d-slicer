"""Slice job routes: create, run, inspect, download.

`POST /slice` runs the pipeline synchronously for a small model and returns the
full result. A job is created first, so progress and history exist even if the
process dies mid-slice, and every stage is recorded against that job.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession, owned_job, owned_printer
from app.core.errors import ConflictError, InsufficientInformationError, NotFoundError
from app.core.logging import get_logger
from app.models.assets import Blob, ModelVersion
from app.models.catalog import MachineProfile, MaterialProfile
from app.models.identity import AuditLog
from app.models.slicing import (
    GeneratedFile,
    JobEvent,
    JobStageRun,
    SliceJob,
    ValidationFinding,
    ValidationResult,
)
from app.schemas.api import (
    GeneratedFileResponse,
    JobEventResponse,
    SliceJobCreate,
    SliceJobResponse,
)
from app.services import models_service, storage
from app.slicing.configuration import canonical_parameter_overrides
from app.slicing.job_factory import create_slice_job
from app.slicing.pipeline import run_pipeline
from app.slicing.registry import get_registry, get_technology_registry
from app.slicing.toolpath import parse_toolpath
from app.slicing.validator import MachineLimits

log = get_logger(__name__)
router = APIRouter(tags=["slicing"])


@router.get(
    "/technologies",
    summary="Printing technologies and what is actually implemented",
)
def technologies(_: CurrentUser) -> list[dict[str, Any]]:
    return [spec.__dict__ | {"key": spec.key} for spec in get_technology_registry().all()]


@router.get("/slicers", summary="Registered slicing backends and their health")
def slicers(_: CurrentUser) -> list[dict[str, Any]]:
    return get_registry().describe()


@router.post(
    "/slice",
    response_model=SliceJobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Run the full pipeline: analyze, optimize, slice, validate",
)
def create_slice(
    payload: SliceJobCreate, db: DbSession, user: CurrentUser
) -> SliceJobResponse:
    version = models_service.get_version(db, user, payload.model_version_id)
    printer = owned_printer(db, user, payload.printer_id)
    material_profile = db.get(MaterialProfile, payload.material_profile_id)
    if material_profile is None:
        raise NotFoundError("The requested material profile does not exist.")

    machine_profile = db.get(MachineProfile, printer.machine_profile_id)
    if machine_profile is None:  # pragma: no cover - FK guarantees presence
        raise NotFoundError("The printer's machine profile does not exist.")

    job = create_slice_job(
        db,
        user_id=user.id,
        version=version,
        printer=printer,
        material_profile=material_profile,
        machine_profile=machine_profile,
    )

    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="slice.requested",
            object_type="slice_job",
            object_id=job.id,
            after={
                "objective": payload.objective,
                "machine_profile_version": machine_profile.profile_version,
                "material_profile": material_profile.brand_sku,
                "nozzle_mm": str(printer.nozzle_diameter_mm),
            },
        )
    )

    overrides = canonical_parameter_overrides(payload.engine_settings_overrides or {})
    locked = canonical_parameter_overrides(
        {
            key: value
            for key, value in (payload.engine_settings_overrides or {}).items()
            if key in set(payload.locked_parameters or [])
        }
    )
    run_pipeline(
        db,
        job=job,
        version=version,
        printer=printer,
        material_profile=material_profile,
        objective=payload.objective,
        locked=locked,
        overrides=overrides,
    )
    db.flush()
    return SliceJobResponse.model_validate(job)


@router.get("/slice", response_model=list[SliceJobResponse], summary="Your slice jobs")
def list_jobs(
    db: DbSession, user: CurrentUser, limit: int = Query(50, ge=1, le=200)
) -> list[SliceJobResponse]:
    rows = db.execute(
        select(SliceJob)
        .where(SliceJob.user_id == user.id)
        .order_by(SliceJob.queued_at.desc())
        .limit(limit)
    ).scalars()
    return [SliceJobResponse.model_validate(r) for r in rows]


@router.get("/slice/{job_id}", response_model=SliceJobResponse, summary="One job")
def get_job(job_id: uuid.UUID, db: DbSession, user: CurrentUser) -> SliceJobResponse:
    return SliceJobResponse.model_validate(owned_job(db, user, job_id))


@router.get(
    "/slice/{job_id}/stages",
    summary="Per-stage results. Progress is derived from these rows.",
)
def job_stages(job_id: uuid.UUID, db: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    job = owned_job(db, user, job_id)
    rows = db.execute(
        select(JobStageRun).where(JobStageRun.slice_job_id == job.id).order_by(JobStageRun.started_at)
    ).scalars()
    return [
        {
            "stage": row.stage,
            "attempt": row.attempt,
            "status": row.status.value,
            "progress": row.progress,
            "duration_ms": row.duration_ms,
            "detail": row.detail,
            "started_at": row.started_at,
            "ended_at": row.ended_at,
        }
        for row in rows
    ]


@router.get(
    "/slice/{job_id}/events",
    response_model=list[JobEventResponse],
    summary="Event log for the job",
)
def job_events(
    job_id: uuid.UUID, db: DbSession, user: CurrentUser, limit: int = Query(200, ge=1, le=1000)
) -> list[JobEventResponse]:
    job = owned_job(db, user, job_id)
    rows = db.execute(
        select(JobEvent)
        .where(JobEvent.slice_job_id == job.id)
        .order_by(JobEvent.at)
        .limit(limit)
    ).scalars()
    return [JobEventResponse.model_validate(r) for r in rows]


@router.get(
    "/slice/{job_id}/validation",
    summary="Validation verdict and every finding",
)
def job_validation(job_id: uuid.UUID, db: DbSession, user: CurrentUser) -> dict[str, Any]:
    job = owned_job(db, user, job_id)
    result = db.execute(
        select(ValidationResult)
        .where(ValidationResult.slice_job_id == job.id)
        .order_by(ValidationResult.checked_at.desc())
    ).scalars().first()
    if result is None:
        return {"validated": False, "reason": "This job has not reached the validation stage."}

    findings = db.execute(
        select(ValidationFinding).where(ValidationFinding.validation_result_id == result.id)
    ).scalars()
    return {
        "validated": result.passed,
        "passed": result.passed,
        "blocks_output": result.blocks_output,
        "summary": result.summary,
        "severity_counts": result.severity_counts,
        "validator": result.validator,
        "validator_version": result.validator_version,
        "findings": [
            {
                "severity": f.severity,
                "code": f.code,
                "message": f.message,
                "line_number": f.line_number,
                "axis": f.axis,
                "measured_value": f.measured_value,
                "limit_value": f.limit_value,
                "unit": f.unit,
                "mitigation": f.mitigation,
            }
            for f in findings
        ],
    }


@router.get(
    "/slice/{job_id}/files",
    response_model=list[GeneratedFileResponse],
    summary="Generated files, with blocked ones marked",
)
def job_files(job_id: uuid.UUID, db: DbSession, user: CurrentUser) -> list[GeneratedFileResponse]:
    job = owned_job(db, user, job_id)
    version = db.get(ModelVersion, job.model_version_id)
    if version is None or job.model_input_sha256 != models_service.active_blob(db, version).sha256:
        return []
    rows = db.execute(
        select(GeneratedFile).where(GeneratedFile.slice_job_id == job.id)
    ).scalars()
    return [GeneratedFileResponse.model_validate(r) for r in rows]


@router.get(
    "/slice/{job_id}/files/{file_id}/download",
    summary="Download a generated file",
    responses={200: {"content": {"text/x-gcode": {}}}},
)
def download_file(
    job_id: uuid.UUID, file_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> Response:
    job = owned_job(db, user, job_id)
    version = db.get(ModelVersion, job.model_version_id)
    if version is None or job.model_input_sha256 != models_service.active_blob(db, version).sha256:
        raise ConflictError(
            "The model changed after this slice was generated; its artifact is stale.",
            code="SLICE_ARTIFACT_STALE",
        )
    generated = db.execute(
        select(GeneratedFile).where(
            GeneratedFile.id == file_id, GeneratedFile.slice_job_id == job.id
        )
    ).scalar_one_or_none()
    if generated is None:
        raise NotFoundError("The requested resource does not exist.")

    # The single guard that makes a blocked toolpath undownloadable.
    if generated.is_blocked:
        raise ConflictError(
            "This file was blocked by a mandatory safety validation and cannot be "
            "downloaded.",
            details={"reason": generated.blocked_reason},
        )

    blob = db.get(Blob, generated.blob_id)
    if blob is None:  # pragma: no cover
        raise NotFoundError("The stored file is missing from the object store.")
    return Response(
        content=storage.read_blob(blob),
        media_type=blob.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{generated.filename}"',
            "X-Content-SHA256": generated.sha256,
        },
    )


@router.get(
    "/slice/{job_id}/toolpath",
    summary="Parsed toolpath for 3D visualisation",
)
def job_toolpath(
    job_id: uuid.UUID, db: DbSession, user: CurrentUser, max_segments: int = Query(60_000, ge=100, le=400_000)
) -> dict[str, Any]:
    job = owned_job(db, user, job_id)
    version = db.get(ModelVersion, job.model_version_id)
    if version is None or job.model_input_sha256 != models_service.active_blob(db, version).sha256:
        raise ConflictError(
            "The model changed after this slice was generated; its toolpath is stale.",
            code="SLICE_ARTIFACT_STALE",
        )
    generated = db.execute(
        select(GeneratedFile)
        .where(GeneratedFile.slice_job_id == job.id, GeneratedFile.kind == "gcode")
        .order_by(GeneratedFile.created_at)
    ).scalars().first()
    if generated is None:
        raise NotFoundError("This job has not produced any G-code yet.")

    machine_profile = db.get(MachineProfile, job.machine_profile_id)
    printer = owned_printer(db, user, job.printer_id, include_archived=True)
    limits = MachineLimits.from_profile(
        machine_profile, nozzle_diameter_mm=float(printer.nozzle_diameter_mm)
    )
    blob = db.get(Blob, generated.blob_id)
    text = storage.read_blob(blob).decode("utf-8", errors="replace")
    path = parse_toolpath(text, limits, max_segments=max_segments)
    return {
        "job_id": str(job.id),
        "blocked": generated.is_blocked,
        "build_volume": {"x": limits.build_x, "y": limits.build_y, "z": limits.build_z},
        **path.to_dict(),
    }
