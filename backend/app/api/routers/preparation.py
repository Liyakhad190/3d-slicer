"""AI print preparation: propose, review, slice, download.

    POST  /preparation                             one attempt, end to end
    GET   /preparation                             your attempts (summary)
    GET   /preparation/{job_id}                    one attempt, in full
    POST  /preparation/{job_id}/replan             next attempt in the chain
    POST  /preparation/{job_id}/validate           re-check, no model call
    POST  /preparation/{job_id}/slice              slice it, produce deliverables
    GET   /preparation/{job_id}/outputs            what may be downloaded
    GET   /preparation/{job_id}/outputs/{id}/download

The router is the boundary: authentication, ownership, request shapes, and a
scrub of anything host-specific before it reaches a client. Every judgement -
what the analysis says, whether the plan passes, which bytes are safe - belongs
to the preparation engine and the slicer, never to this file.

Status codes follow the output-format contract: 409 when the printer profile
does not declare the requested format, 503 when it declares one this
deployment cannot produce, 422 when produced bytes fail validation.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, SettingsDep, owned_printer, rate_limit
from app.core.errors import ConflictError, LLMServiceError, NotFoundError
from app.core.logging import get_logger
from app.intelligence.api.router import LLM_HINTS, LLM_STATUS_BY_CODE
from app.intelligence.providers.registry import get_llm_provider
from app.models.assets import Blob, Model, ModelVersion
from app.models.catalog import Machine, MachineProfile, Material, MaterialProfile, Printer
from app.models.preparation import PreparationStatus, PrintPreparationJob
from app.models.slicing import GeneratedFile
from app.preparation import jobs as preparation
from app.preparation import slicing as preparation_slice
from app.schemas.api import (
    OutputArtifactResponse,
    PreparationCreate,
    PreparationJobResponse,
    PreparationJobSummary,
    PreparationOutputFormat,
    PreparationReplanRequest,
    PreparationOrientRequest,
    PreparationSliceRef,
    PreparationSliceRequest,
)
from app.slicing.output import service as output_service
from app.slicing.output.base import OutputFormatUnavailable
from app.services import models_service, storage

log = get_logger(__name__)
router = APIRouter(tags=["preparation"])


# --------------------------------------------------------------------------- #
# the API boundary: no host paths cross it
# --------------------------------------------------------------------------- #

#: Where diagnostics record things that only make sense on this machine. Dropped
#: at every level, not just the top: availability details arrive already nested.
_SENSITIVE_KEYS = frozenset(
    {"searched", "executable", "flashprint_engine", "path", "storage_path", "blob_path"}
)

#: A Windows or POSIX path. Deliberately greedy: an over-redacted reason string
#: costs the client some prose, an un-redacted one leaks where we live.
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)(?:[^\"'\n]+?[\\/])+[^\s\"']*")


def _redact(match: re.Match[str]) -> str:
    """Keep a URL, redact a host path.

    A scheme immediately before the match means this is a link the client can
    use - `install` points at where to get the encoder - not a directory on
    this machine.
    """
    start = match.start()
    if start and match.string[start - 1] in ":/":
        return match.group(0)
    return "[path]"


def _scrub(value: Any) -> Any:
    """Recursively redact host paths and drop machine-specific keys."""
    if isinstance(value, str):
        return _PATH_RE.sub(_redact, value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in _SENSITIVE_KEYS}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _safe_details(details: Any) -> dict[str, Any]:
    """Diagnostic details, minus everything machine-specific."""
    return _scrub(details) if isinstance(details, dict) else {}


def _require_format(machine_profile: MachineProfile, format_key: str) -> Any:
    """409 when undeclared, 503 when unproducible, both with scrubbed detail."""
    try:
        return output_service.require_usable(machine_profile, format_key)
    except OutputFormatUnavailable as exc:
        raise OutputFormatUnavailable(
            exc.message, details=_safe_details(exc.details)
        ) from exc


def _raise_llm_failure(db: Session, job: PrintPreparationJob) -> NoReturn:
    """Turn a stored provider failure into the API's LLM error envelope.

    Two deliberate departures from the ordinary request flow. The status comes
    from the provider's own code - unreachable is 503, output we refused is
    422, a timeout is 504 - so the client can tell a retryable outage from a
    problem with its request. And the session is committed first: an exception
    leaving the request rolls the session back, which would erase the very
    attempt `details.job_id` points at. The evidence has to survive the error
    that reports it.
    """
    error = (job.validation or {}).get("error") or {}
    code = error.get("code") or "LLM_PROVIDER_ERROR"
    details = _safe_details(error.get("details"))
    details.update(
        {
            "job_id": str(job.id),
            "attempt": job.attempt,
            "status": job.status.value if job.status else None,
            "hint": LLM_HINTS.get(code, ""),
        }
    )
    failure = LLMServiceError(
        error.get("message") or "The model could not produce a print plan.",
        code=code,
        details=details,
    )
    failure.http_status = LLM_STATUS_BY_CODE.get(code, failure.http_status)
    db.commit()
    raise failure


# --------------------------------------------------------------------------- #
# response shaping
# --------------------------------------------------------------------------- #


def _formats(machine_profile: MachineProfile | None) -> list[PreparationOutputFormat]:
    if machine_profile is None:
        return []
    rows: list[PreparationOutputFormat] = []
    for row in output_service.formats_for(machine_profile):
        details = _safe_details(row.get("details"))
        rows.append(
            PreparationOutputFormat(
                key=row["key"],
                format=row["format"],
                extension=row["extension"],
                declared_by_profile=bool(row.get("declared_by_profile")),
                advertised=bool(row.get("advertised")),
                available=bool(row.get("available")),
                reason=_scrub(row.get("reason") or ""),
                remedy=str(details.get("install") or ""),
            )
        )
    return rows


def _summary(job: PrintPreparationJob) -> PreparationJobSummary:
    return PreparationJobSummary(**preparation.to_dict(job))


def _artifact(
    generated: GeneratedFile, artifact: Any
) -> OutputArtifactResponse:
    return OutputArtifactResponse(
        id=generated.id,
        filename=artifact.filename,
        format=artifact.format,
        bytes=artifact.bytes,
        checksum=artifact.checksum,
        printer=artifact.printer,
        material=artifact.material,
        slicer=artifact.slicer,
        slicer_version=artifact.slicer_version,
        configuration_fingerprint=artifact.configuration_fingerprint,
        validation=artifact.validation,
        media_type=artifact.media_type,
        created_at=generated.created_at.isoformat() if generated.created_at else None,
    )


def _detail(db: Session, job: PrintPreparationJob) -> PreparationJobResponse:
    """Everything one attempt recorded, plus what its printer may produce."""
    version = db.get(ModelVersion, job.model_version_id)
    model = db.get(Model, version.model_id) if version else None
    printer = db.get(Printer, job.printer_id)
    machine_profile = (
        db.get(MachineProfile, printer.machine_profile_id) if printer else None
    )
    machine = db.get(Machine, machine_profile.machine_id) if machine_profile else None
    material_profile = db.get(MaterialProfile, job.material_profile_id)
    material = (
        db.get(Material, material_profile.material_id) if material_profile else None
    )

    slice_job = preparation_slice.slice_job_for(db, job)
    slice_ref = (
        PreparationSliceRef(
            slice_job_id=slice_job.id,
            status=slice_job.status.value if slice_job.status else "unknown",
            input_fingerprint=slice_job.input_fingerprint,
            error_code=slice_job.error_code,
            error_detail=slice_job.error_detail,
        )
        if slice_job is not None
        else None
    )

    return PreparationJobResponse(
        **preparation.to_dict(job),
        model={
            "model_version_id": str(version.id),
            "model_id": str(version.model_id),
            "model_name": model.name if model else None,
            "filename": version.sanitized_filename,
            "format": version.format,
            "triangles": version.triangle_count,
            "bbox_mm": {
                "x": version.bbox_x,
                "y": version.bbox_y,
                "z": version.bbox_z,
            },
        }
        if version is not None
        else None,
        printer={
            "printer_id": str(printer.id),
            "name": printer.name,
            "machine": f"{machine.vendor} {machine.model}" if machine else None,
            "technology": machine.technology.value if machine else None,
            "nozzle_diameter_mm": float(printer.nozzle_diameter_mm),
            "firmware": printer.firmware,
            "machine_profile_id": str(printer.machine_profile_id),
            "machine_profile_version": machine_profile.profile_version
            if machine_profile
            else None,
        }
        if printer is not None
        else None,
        material={
            "material_profile_id": str(material_profile.id),
            "brand_sku": material_profile.brand_sku,
            "vendor": material_profile.vendor,
            "material": material.display_name if material else None,
            "family": material.family if material else None,
        }
        if material_profile is not None
        else None,
        output_formats=_formats(machine_profile),
        slice=slice_ref,
        outputs=[
            _artifact(generated, artifact)
            for generated, artifact in preparation_slice.artifacts_for(db, job)
        ],
    )


def _machine_profile(db: Session, printer: Printer) -> MachineProfile:
    machine_profile = db.get(MachineProfile, printer.machine_profile_id)
    if machine_profile is None:  # pragma: no cover - FK guarantees presence
        raise NotFoundError("The printer's machine profile does not exist.")
    return machine_profile


# --------------------------------------------------------------------------- #
# attempts
# --------------------------------------------------------------------------- #


@router.post(
    "/preparation",
    response_model=PreparationJobResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("preparation"))],
    summary="Analyse, plan, and validate one print preparation attempt",
)
def create_preparation(
    payload: PreparationCreate,
    db: DbSession,
    user: CurrentUser,
    settings: SettingsDep,
) -> PreparationJobResponse:
    """One attempt: analysis -> model -> 22 checks, synchronously.

    Always 201 with the attempt row, including a *rejected* one: a refusal is
    a result the operator reviews and re-plans from, not an HTTP failure. Only
    a provider that could not answer at all is an error (5xx), and even then
    the attempt is recorded - its id comes back in `details.job_id`.
    """
    version = models_service.get_version(db, user, payload.model_version_id)
    printer = owned_printer(db, user, payload.printer_id)
    material_profile = db.get(MaterialProfile, payload.material_profile_id)
    if material_profile is None:
        raise NotFoundError("The requested material profile does not exist.")
    machine_profile = _machine_profile(db, printer)

    # Checked before spending a token: a format this deployment cannot produce
    # must fail on the request, not after the model has already run.
    if payload.output_format:
        _require_format(machine_profile, payload.output_format)

    job = preparation.propose(
        db,
        user_id=user.id,
        model_version_id=version.id,
        printer_id=printer.id,
        material_profile_id=material_profile.id,
        objective=payload.objective,
        user_request=payload.user_request,
        provider=get_llm_provider(settings),
        settings=settings,
    )
    if job.status is PreparationStatus.failed:
        _raise_llm_failure(db, job)

    log.info(
        "preparation.requested",
        job_id=str(job.id),
        user_id=str(user.id),
        status=job.status.value if job.status else None,
        output_format=payload.output_format,
    )
    return _detail(db, job)


@router.get(
    "/preparation",
    response_model=list[PreparationJobSummary],
    summary="Your preparation attempts, newest first",
)
def list_preparations(
    db: DbSession,
    user: CurrentUser,
    model_version_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[PreparationJobSummary]:
    """Summary rows only: the full payload of every attempt would carry each
    prompt and plan, and this is a list."""
    rows = preparation.list_jobs(
        db, user_id=user.id, model_version_id=model_version_id, limit=limit
    )
    return [_summary(job) for job in rows]


@router.get(
    "/preparation/{job_id}",
    response_model=PreparationJobResponse,
    summary="One preparation attempt",
)
def get_preparation(
    job_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> PreparationJobResponse:
    return _detail(db, preparation.get_job(db, job_id, user_id=user.id))


@router.post(
    "/preparation/{job_id}/replan",
    response_model=PreparationJobResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("preparation"))],
    summary="Start the next attempt in this plan's chain",
)
def replan_preparation(
    job_id: uuid.UUID,
    payload: PreparationReplanRequest,
    db: DbSession,
    user: CurrentUser,
    settings: SettingsDep,
) -> PreparationJobResponse:
    """Attempt N+1, bounded by the same three-attempt chain the planner uses.

    The intent carried forward is the refused plan's intent, so a re-plan
    corrects what was wrong instead of quietly changing what was asked for.
    """
    job = preparation.get_job(db, job_id, user_id=user.id)
    updated = preparation.replan(
        db,
        job=job,
        user_request=payload.user_request,
        feedback=payload.feedback,
        objective=payload.objective,
        provider=get_llm_provider(settings),
        settings=settings,
    )
    if updated.status is PreparationStatus.failed:
        _raise_llm_failure(db, updated)
    return _detail(db, updated)


@router.post(
    "/preparation/{job_id}/validate",
    summary="Re-run the checklist against current measurements, without a model",
)
def validate_preparation(
    job_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> dict[str, Any]:
    """Would this plan still pass, right now?

    `stored` is the verdict recorded when the plan was produced; `recheck` is
    the same checklist re-measured today. They differ when a profile changed
    in between - which is exactly when slicing must stop.
    """
    job = preparation.get_job(db, job_id, user_id=user.id)
    if not job.plan:
        raise ConflictError(
            "This attempt produced no plan, so there is nothing to validate.",
            code="PREPARATION_NOT_READY",
            details={
                "job_id": str(job.id),
                "status": job.status.value if job.status else None,
            },
        )
    return {
        "job_id": str(job.id),
        "attempt": job.attempt,
        "stored": job.validation,
        "recheck": preparation.revalidate(db, job=job),
    }


@router.post(
    "/preparation/{job_id}/orient",
    summary="List orientation candidates or select a candidate pose for this preparation job",
)
def orient_preparation(
    job_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    payload: PreparationOrientRequest | None = None,
) -> dict[str, Any]:
    """Inspect or update the model's orientation for this preparation job.

    When `candidate_id` is omitted, returns all deterministically analysed
    orientation candidates with their measured contact area, overhangs, and
    ranking. When `candidate_id` is specified, verifies the candidate against
    hardware limits and updates the job's orientation plan and validation.
    """
    job = preparation.get_job(db, job_id, user_id=user.id)
    if payload is None or payload.candidate_id is None:
        return preparation.get_orientations(db, job=job)

    updated_job, candidate = preparation.apply_orientation(
        db, job=job, candidate_id=payload.candidate_id
    )
    return {
        "job_id": str(updated_job.id),
        "selected_candidate": candidate.to_dict(),
        "job": _detail(db, updated_job),
    }


# --------------------------------------------------------------------------- #
# slicing and deliverables
# --------------------------------------------------------------------------- #


@router.post(
    "/preparation/{job_id}/slice",
    response_model=PreparationJobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Slice an approved plan and produce its deliverable",
)
def slice_preparation(
    job_id: uuid.UUID,
    payload: PreparationSliceRequest,
    db: DbSession,
    user: CurrentUser,
) -> PreparationJobResponse:
    """Plan -> configuration -> CuraEngine -> validated artifact.

    The plan's `locked` values and normalisation `overrides` are what the
    optimizer is not allowed to move; the slicer still runs the real pipeline,
    and the file format still comes from the output registry. A request for a
    format this printer never declared (409) or this deployment cannot produce
    (503) is refused *before* any slicing time is spent.
    """
    job = preparation.get_job(db, job_id, user_id=user.id)
    result = preparation_slice.slice_prepared(
        db,
        prep_job=job,
        objective=payload.objective,
        output_format=payload.output_format,
    )
    db.flush()
    sliced = result["slice_job"]
    log.info(
        "preparation.sliced",
        job_id=str(job.id),
        slice_job_id=str(sliced.id),
        status=sliced.status.value if sliced.status else None,
        format=result["requested_format"],
    )
    return _detail(db, job)


@router.get(
    "/preparation/{job_id}/outputs",
    response_model=list[OutputArtifactResponse],
    summary="Files this attempt produced, validated and downloadable",
)
def preparation_outputs(
    job_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> list[OutputArtifactResponse]:
    """Only files that are stored *and* passed their own validation. A blocked
    or unvalidated file is not listed, because the download would refuse it."""
    job = preparation.get_job(db, job_id, user_id=user.id)
    return [
        _artifact(generated, artifact)
        for generated, artifact in preparation_slice.artifacts_for(db, job)
    ]


@router.get(
    "/preparation/{job_id}/outputs/{artifact_id}/download",
    summary="Download a validated output artifact",
    responses={200: {"content": {"text/x-gcode": {}}}},
)
def download_preparation_output(
    job_id: uuid.UUID,
    artifact_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    job = preparation.get_job(db, job_id, user_id=user.id)
    generated, artifact = preparation_slice.artifact_for_id(db, job, artifact_id)

    blob = db.get(Blob, generated.blob_id)
    if blob is None:  # pragma: no cover
        raise NotFoundError("The stored file is missing from the object store.")
    return Response(
        content=storage.read_blob(blob),
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{generated.filename}"',
            "X-Content-SHA256": generated.sha256,
            "X-Output-Format": artifact.format,
        },
    )
