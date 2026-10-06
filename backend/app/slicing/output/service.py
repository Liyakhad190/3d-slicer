"""Turn an already-sliced job into a deliverable file of a chosen format.

This is the seam between *what was sliced* and *what the operator downloads*.
The pipeline produces one validated G-code artifact per job; this service
renders that artifact into any format the printer's profile declares and the
deployment can actually produce, then stores the result as another
:class:`~app.models.slicing.GeneratedFile`.

Guarantees:

* the toolpath is never re-sliced, re-optimised or re-decided here;
* an unavailable format raises with the reason instead of degrading to G-code;
* a format the printer never declared is refused before any work happens;
* bytes that fail validation are discarded — nothing is written, nothing is
  downloadable;
* every artifact carries provenance (checksum, printer, slicer, configuration
  fingerprint, validation status).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.models.assets import Blob
from app.models.catalog import MachineProfile, MaterialProfile, Printer
from app.models.slicing import (
    GeneratedFile,
    SlicerEngine,
    SliceJob,
    StageStatus,
    ValidationResult,
)
from app.services import storage
from app.slicing.output.artifacts import OutputArtifact, describe_generated_file
from app.slicing.output.base import FormatRequest, OutputFormatRejected
from app.slicing.output.registry import (
    OutputFormatterRegistry,
    declared_output_formats,
    get_output_registry,
)

log = get_logger(__name__)


def registry() -> OutputFormatterRegistry:
    return get_output_registry()


# --------------------------------------------------------------------------- #
# capability introspection
# --------------------------------------------------------------------------- #


def formats_for(machine_profile: MachineProfile) -> list[dict[str, Any]]:
    """Everything this printer *could* be offered, with availability and reason."""
    return registry().supported_for(machine_profile)


def advertised_formats(machine_profile: MachineProfile) -> list[str]:
    """Only formats that are both declared by the profile and producible here."""
    return registry().advertised_formats(machine_profile)


def declared_formats(machine_profile: MachineProfile) -> tuple[str, ...]:
    return declared_output_formats(machine_profile)


def require_usable(machine_profile: MachineProfile, format_key: str) -> Any:
    """Resolve a requested format, refusing anything undeclared or unproducible.

    The two gates live here so both callers — the one that decides *before*
    slicing time is spent and the one that actually renders — take exactly the
    same route: 409 when the printer never declared the format, 503 when it
    declared it but this deployment cannot produce it.
    """
    formatter = registry().require_supported(machine_profile, format_key)
    state = formatter.availability()
    if not state.available:
        from app.slicing.output.base import OutputFormatUnavailable

        raise OutputFormatUnavailable(
            f"{formatter.format.value} output is not available: {state.reason}",
            details={
                "format": formatter.format.value,
                "requested": format_key,
                **state.details,
            },
        )
    return formatter


# --------------------------------------------------------------------------- #
# source resolution
# --------------------------------------------------------------------------- #


def source_gcode_file(db: Session, job: SliceJob) -> GeneratedFile:
    """The job's canonical G-code artifact — the only thing we re-present."""
    row = db.execute(
        select(GeneratedFile)
        .where(GeneratedFile.slice_job_id == job.id, GeneratedFile.kind == "gcode")
        .order_by(GeneratedFile.created_at.desc())
    ).scalars().first()
    if row is None:
        raise NotFoundError(
            "This job has no sliced G-code yet; output formats are produced "
            "from an existing toolpath.",
            details={"job_id": str(job.id)},
        )
    return row


def _job_validation(db: Session, job: SliceJob) -> dict[str, Any]:
    """The G-code validation the pipeline ran, for provenance on any format."""
    result = db.execute(
        select(ValidationResult)
        .where(ValidationResult.slice_job_id == job.id)
        .order_by(ValidationResult.checked_at.desc())
    ).scalars().first()
    if result is None:
        return {"status": "unvalidated", "checks": 0}
    return {
        "status": "passed" if result.passed else "blocked",
        "validator": result.validator,
        "validator_version": result.validator_version,
        "summary": result.summary,
        "severity_counts": result.severity_counts,
        "checked_at": result.checked_at.isoformat() if result.checked_at else None,
        "source_validation": True,
    }


def _describe_job_context(
    db: Session,
    job: SliceJob,
    *,
    machine_profile: MachineProfile | None,
) -> dict[str, Any]:
    """Provenance assembled from rows we already have — never invented."""
    printer = db.get(Printer, job.printer_id) if job.printer_id else None
    material = (
        db.get(MaterialProfile, job.material_profile_id) if job.material_profile_id else None
    )
    machine = getattr(machine_profile, "machine", None) if machine_profile else None

    material_label = None
    if material is not None:
        family = getattr(material, "material", None)
        material_label = getattr(family, "display_name", None) or material.brand_sku

    slicer = None
    slicer_version = None
    for run in job.stage_runs or []:
        if run.stage == "slicing" and run.status == StageStatus.succeeded:
            detail = run.detail or {}
            slicer = detail.get("engine") or slicer
    if slicer:
        engine = db.execute(
            select(SlicerEngine).where(SlicerEngine.key == slicer)
        ).scalars().first()
        slicer_version = engine.engine_version if engine else None

    return {
        "printer": f"{machine.vendor} {machine.model}" if machine else None,
        "printer_name": printer.name if printer else None,
        "material": material_label,
        "nozzle_diameter_mm": float(printer.nozzle_diameter_mm) if printer else None,
        "slicer": slicer,
        "slicer_version": slicer_version,
        "configuration_fingerprint": job.input_fingerprint or None,
    }


# --------------------------------------------------------------------------- #
# generation
# --------------------------------------------------------------------------- #


def generate_output(
    db: Session,
    *,
    job: SliceJob,
    machine_profile: MachineProfile,
    format_key: str,
    filename_base: str | None = None,
    limits: Any | None = None,
) -> tuple[GeneratedFile, OutputArtifact]:
    """Render the job's G-code into `format_key` and store the result.

    Raises :class:`OutputFormatUnsupported` (409) when the printer profile does
    not declare the format, :class:`OutputFormatUnavailable` (503) when it is
    declared but no encoder can run, and :class:`OutputFormatRejected` (422)
    when the produced bytes fail validation. In none of those cases is anything
    written.
    """
    formatter = require_usable(machine_profile, format_key)

    source = source_gcode_file(db, job)

    # G-code is the source artifact itself — never stored a second time.
    if formatter.key == "gcode":
        if source.is_blocked:
            from app.core.errors import ConflictError

            raise ConflictError(
                "This file was blocked by a mandatory safety validation and "
                "cannot be produced.",
                details={"reason": source.blocked_reason},
            )
        return source, artifact_for(
            db, source, job=job, machine_profile=machine_profile
        )

    # Idempotent: asking twice for the same format returns the same file.
    existing = db.execute(
        select(GeneratedFile).where(
            GeneratedFile.slice_job_id == job.id,
            GeneratedFile.kind == formatter.key,
            GeneratedFile.is_blocked.is_(False),
        )
    ).scalars().first()
    if existing is not None:
        return existing, artifact_for(
            db, existing, job=job, machine_profile=machine_profile
        )

    source_blob = db.get(Blob, source.blob_id)
    if source_blob is None:  # pragma: no cover
        raise NotFoundError("The sliced toolpath is missing from the object store.")
    gcode = storage.read_blob(source_blob)

    base = filename_base or source.filename or "print"
    request = FormatRequest(
        gcode=gcode,
        filename_base=base,
        context={
            "limits": limits,
            "job_id": str(job.id),
            "machine": str(getattr(machine_profile, "machine", None) or ""),
            **_describe_job_context(db, job, machine_profile=machine_profile),
        },
    )

    result = formatter.render(request)

    problems = formatter.validate(request, result)
    if problems:
        # Nothing is written: a file that failed validation is never stored and
        # therefore can never be served.
        log.error(
            "output.rejected",
            format=formatter.key,
            job_id=str(job.id),
            problems=problems,
        )
        raise OutputFormatRejected(
            "The generated file did not pass validation and was discarded.",
            details={"format": formatter.key, "problems": problems},
        )

    blob, _ = storage.put_blob(
        result.payload,
        media_type=result.media_type,
        detected_format=formatter.key,
        session=db,
    )
    generated = GeneratedFile(
        slice_job_id=job.id,
        blob_id=blob.id,
        kind=formatter.key,
        filename=result.filename,
        sha256=blob.sha256,
        byte_size=len(result.payload),
        is_blocked=False,
    )
    db.add(generated)
    db.flush()

    artifact = describe_generated_file(
        generated,
        payload=result.payload,
        **_describe_job_context(db, job, machine_profile=machine_profile),
        validation={
            "status": "passed",
            "checks": 1,
            "validator": f"{formatter.key}-format/v1",
            "source_validation": _job_validation(db, job),
        },
        metadata=result.metadata,
    )
    log.info(
        "output.generated",
        format=formatter.key,
        job_id=str(job.id),
        bytes=len(result.payload),
        filename=result.filename,
    )
    return generated, artifact


def artifact_for(
    db: Session,
    generated: GeneratedFile,
    *,
    job: SliceJob | None = None,
    machine_profile: MachineProfile | None = None,
) -> OutputArtifact:
    """Describe an already-stored file without regenerating it."""
    job = job or db.get(SliceJob, generated.slice_job_id)
    blob = db.get(Blob, generated.blob_id)
    payload = storage.read_blob(blob) if blob is not None else b""
    fmt = (generated.kind or "gcode").lower()

    if fmt == "gcode":
        validation = _job_validation(db, job) if job else {"status": "unvalidated"}
    else:
        from app.slicing.output.gx import validate_gx

        source = None
        if job is not None:
            try:
                source_blob = db.get(Blob, source_gcode_file(db, job).blob_id)
                source = storage.read_blob(source_blob) if source_blob else None
            except NotFoundError:
                source = None
        problems = validate_gx(payload, source_gcode=source) if fmt == "gx" else []
        validation = {
            "status": "passed" if not problems else "blocked",
            "checks": 1,
            "validator": f"{fmt}-format/v1",
            "problems": problems,
        }

    ctx = _describe_job_context(db, job, machine_profile=machine_profile) if job else {}
    return describe_generated_file(
        generated,
        payload=payload,
        **ctx,
        validation=validation,
        metadata={"byte_size": generated.byte_size},
    )
