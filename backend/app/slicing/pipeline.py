"""The slicing pipeline: one service, every stage, real stages only.

`run_pipeline` executes the chain the requirements describe. Each stage records a
`JobStageRun` row and emits an event, so progress is *derived* from work that
actually happened. There is no simulated progress anywhere in this module.

Stage order:

    analyze -> optimize -> select_engine -> slice -> validate -> parse

`repair` and `simulate` exist in the schema and the state machine but are not
implemented; they report as `skipped` with a reason rather than pretending to
have run.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import (
    AppError,
    InsufficientInformationError,
    SlicingError,
    ValidationFailedError,
)
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.assets import Blob, ModelVersion
from app.models.catalog import MachineProfile, MaterialProfile, Nozzle, Printer
from app.models.slicing import (
    STAGES,
    GeneratedFile,
    JobEvent,
    JobStageRun,
    JobStatus,
    SliceJob,
    StageStatus,
    ValidationFinding,
    ValidationResult,
)
from app.services import models_service, storage
from app.slicing.configuration import PrintConfiguration, from_profiles
from app.slicing.optimizer import ParameterOptimizer
from app.slicing.registry import SlicerCandidate, get_registry
from app.slicing.toolpath import parse_toolpath
from app.slicing.validator import MachineLimits, validate_gcode

log = get_logger(__name__)

#: Our detected format names -> the short code trimesh expects.
_TRIMESH_FILE_TYPE: dict[str, str] = {
    "stl_binary": "stl",
    "stl_ascii": "stl",
    "obj": "obj",
    "ply": "ply",
    "three_mf": "3mf",
    "amf": "amf",
    "glb": "glb",
    "gltf": "gltf",
}


@dataclass
class PipelineResult:
    job_id: uuid.UUID
    status: JobStatus
    configuration: PrintConfiguration | None = None
    candidates: list[SlicerCandidate] = field(default_factory=list)
    validation: ValidationResult | None = None
    generated_file: GeneratedFile | None = None
    toolpath_summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    engine: str | None = None
    engine_version: str | None = None


def _event(
    db: Session,
    job: SliceJob,
    message: str,
    *,
    stage: str | None = None,
    level: str = "info",
    data: dict[str, Any] | None = None,
) -> None:
    db.add(
        JobEvent(
            slice_job_id=job.id, level=level, stage=stage, message=message, data=data or {}
        )
    )


def _start_stage(db: Session, job: SliceJob, stage: str) -> JobStageRun:
    attempt = (
        db.query(JobStageRun)
        .filter(JobStageRun.slice_job_id == job.id, JobStageRun.stage == stage)
        .count()
    ) + 1
    run = JobStageRun(
        slice_job_id=job.id, stage=stage, attempt=attempt, status=StageStatus.running
    )
    db.add(run)
    db.flush()
    return run


def _finish_stage(db: Session, run: JobStageRun, status: StageStatus, detail: dict[str, Any]) -> None:
    run.status = status
    run.detail = detail
    run.ended_at = utcnow()
    run.duration_ms = max(0, int((utcnow() - run.started_at).total_seconds() * 1000))
    db.flush()


def _remaining(db: Session, job: SliceJob) -> list[str]:
    done = {
        row.stage
        for row in job.stage_runs
        if row.status in {StageStatus.succeeded, StageStatus.skipped, StageStatus.blocked}
    }
    return [stage for stage in STAGES if stage not in done]


def _fail_job(db: Session, job: SliceJob, code: str, detail: str, *, stages: list[str]) -> None:
    job.status = JobStatus.failed
    job.error_code = code
    job.error_detail = detail[:4000]
    job.finished_at = utcnow()
    for stage in stages:
        if any(row.stage == stage for row in job.stage_runs):
            continue
        run = _start_stage(db, job, stage)
        _finish_stage(db, run, StageStatus.skipped, {"reason": "pipeline aborted", "implemented": False})
    _event(db, job, f"Pipeline failed: {code} - {detail}", level="error")


def _persist_validation(db: Session, job: SliceJob, report: Any, input_sha256: str) -> ValidationResult:
    row = ValidationResult(
        slice_job_id=job.id,
        validator=report.validator,
        validator_version=report.validator_version,
        input_sha256=input_sha256,
        passed=report.passed,
        blocks_output=report.blocks_output,
        severity_counts=report.severity_counts,
        summary=report.summary,
        duration_ms=0,
    )
    db.add(row)
    db.flush()
    for finding in report.findings:
        db.add(
            ValidationFinding(
                validation_result_id=row.id,
                severity=finding.severity,
                code=finding.code,
                message=finding.message,
                line_number=finding.line_number,
                axis=finding.axis,
                measured_value=finding.measured_value,
                limit_value=finding.limit_value,
                unit=finding.unit,
                mitigation=finding.mitigation,
                source="deterministic",
            )
        )
    db.flush()
    return row


def run_pipeline(
    db: Session,
    *,
    job: SliceJob,
    version: ModelVersion,
    printer: Printer,
    material_profile: MaterialProfile,
    objective: str = "balanced",
    locked: dict[str, Any] | None = None,
) -> PipelineResult:
    """Execute the pipeline for an already-created job.

    Failures are recorded on the job and re-raised as typed errors, so a caller
    sees exactly what went wrong rather than a generic 500.
    """
    started = time.perf_counter()
    result = PipelineResult(job_id=job.id, status=JobStatus.analyzing)

    machine_profile = db.get(MachineProfile, job.machine_profile_id)
    if machine_profile is None:
        raise InsufficientInformationError("The job's machine profile no longer exists.")
    material_profile = db.get(MaterialProfile, job.material_profile_id)
    if material_profile is None:
        raise InsufficientInformationError("The job's material profile no longer exists.")
    nozzle = db.get(Nozzle, job.nozzle_id)
    if nozzle is None:
        raise InsufficientInformationError("The job's nozzle record no longer exists.")

    try:
        # -- 1. analyze ----------------------------------------------------
        stage = _start_stage(db, job, "analyzing")
        report = models_service.run_analysis(
            db, version=version, no_opening_mm=float(nozzle.diameter_mm), force=False
        )
        geometry = report.to_dict()["metrics"]
        _finish_stage(
            db,
            stage,
            StageStatus.succeeded,
            {
                "triangles": report.metrics.triangle_count,
                "printability_score": report.printability_score,
                "risk_level": report.risk_level,
                "findings": len(report.findings),
            },
        )
        _event(
            db,
            job,
            f"Geometry analysed: {report.metrics.triangle_count:,} triangles, "
            f"printability {report.printability_score}/100",
            stage="analyzing",
            data={"score": report.printability_score, "risk": report.risk_level},
        )

        # -- 1b. does it fit, before spending time on anything else? --------
        build = machine_profile.build_volume or {}
        dims = report.metrics.dimensions_mm
        for axis in ("x", "y", "z"):
            limit = float(build.get(axis) or 0)
            if limit and dims[axis] > limit:
                raise ValidationFailedError(
                    f"The model is {dims[axis]:.1f} mm on {axis.upper()}, larger than this "
                    f"machine's {limit:.1f} mm in that direction.",
                    details={
                        "axis": axis,
                        "model_mm": dims[axis],
                        "build_volume_mm": limit,
                        "remedy": "Re-orient the model, scale it in CAD, or choose a larger machine.",
                    },
                )

        # -- 2. optimize ---------------------------------------------------
        stage = _start_stage(db, job, "optimizing")
        limits = MachineLimits.from_profile(
            machine_profile, nozzle_diameter_mm=float(nozzle.diameter_mm)
        )
        optimised = ParameterOptimizer(
            machine_profile=machine_profile,
            material_profile=material_profile,
            nozzle_diameter_mm=float(nozzle.diameter_mm),
            limits=limits,
        ).optimise(objective=objective, geometry=geometry, locked=locked or {})  # type: ignore[arg-type]
        _finish_stage(
            db,
            stage,
            StageStatus.succeeded,
            {
                "objective": objective,
                "fingerprint": optimised.fingerprint,
                "blocked": optimised.blocked,
                "decisions": len(optimised.decisions),
            },
        )
        result.warnings.extend(optimised.warnings)
        result.blocked_reasons.extend(optimised.block_reasons)
        _event(
            db,
            job,
            f"Parameters resolved: {len(optimised.decisions)} decisions, "
            f"fingerprint {optimised.fingerprint[:12]}",
            stage="optimizing",
        )

        configuration = from_profiles(
            machine_profile=machine_profile,
            material_profile=material_profile,
            nozzle_diameter_mm=float(nozzle.diameter_mm),
            nozzle_material=printer.nozzle_material,
            parameters=optimised.parameters,
            objective=objective,  # type: ignore[arg-type]
            warnings=optimised.warnings,
            blocked_reasons=optimised.block_reasons,
        )
        result.configuration = configuration

        if configuration.is_blocked:
            stage = _start_stage(db, job, "validating")
            _finish_stage(
                db, stage, StageStatus.blocked, {"reasons": list(configuration.blocked_reasons)}
            )
            job.status = JobStatus.blocked
            job.error_code = "UNSAFE_CONFIGURATION"
            job.error_detail = "; ".join(configuration.blocked_reasons)
            job.finished_at = utcnow()
            _event(db, job, f"Blocked: {job.error_detail}", stage="validating", level="error")
            result.status = JobStatus.blocked
            return result

        # -- 3. choose an engine --------------------------------------------
        stage = _start_stage(db, job, "selecting_engine")
        registry = get_registry()
        result.candidates = registry.candidates(configuration)
        adapter = registry.select(configuration)
        result.engine = adapter.key
        result.engine_version = adapter.probe()
        _finish_stage(
            db,
            stage,
            StageStatus.succeeded,
            {
                "engine": adapter.key,
                "version": result.engine_version,
                "candidates": [c.to_dict() for c in result.candidates],
            },
        )
        _event(
            db,
            job,
            f"Slicing engine selected: {adapter.display_name} "
            f"({result.engine_version or 'version unknown'})",
            stage="selecting_engine",
        )

        # -- 4. slice -------------------------------------------------------
        stage = _start_stage(db, job, "slicing")
        job.status = JobStatus.slicing
        db.flush()

        engine_settings = adapter.prepare_configuration(configuration)
        source_blob = db.get(Blob, version.blob_id)
        if source_blob is None:
            raise InsufficientInformationError("The model file is missing from the object store.")

        workspace = Path(storage.new_job_workspace(job.id))
        output_path = workspace / "output.gcode"
        model_path = storage.get_blob_path(source_blob)

        from app.slicing.cura_adapter import SliceRequest

        slice_result = adapter.slice(
            SliceRequest(
                model_path=model_path,
                output_path=output_path,
                settings=engine_settings,
                job_label=str(job.id),
                model_format=_TRIMESH_FILE_TYPE.get(version.format),
            )
        )

        problems = adapter.validate_output(output_path)
        if problems:
            raise SlicingError("; ".join(problems))

        gcode_bytes = output_path.read_bytes()
        gcode_blob, _ = storage.put_blob(
            gcode_bytes, media_type="text/x-gcode", detected_format="gcode", session=db
        )
        _finish_stage(
            db,
            stage,
            StageStatus.succeeded,
            {
                "engine": adapter.key,
                "duration_ms": slice_result.duration_ms,
                "bytes": len(gcode_bytes),
                "dropped_settings": slice_result.dropped_settings,
            },
        )
        _event(
            db, job, f"Sliced in {slice_result.duration_ms} ms -> {len(gcode_bytes):,} bytes",
            stage="slicing",
        )

        generated = GeneratedFile(
            slice_job_id=job.id,
            blob_id=gcode_blob.id,
            kind="gcode",
            filename=f"{version.sanitized_filename.rsplit('.', 1)[0]}.gcode",
            sha256=gcode_blob.sha256,
            byte_size=len(gcode_bytes),
            is_blocked=False,
        )
        db.add(generated)
        db.flush()
        result.generated_file = generated

        # -- 5. validate -----------------------------------------------------
        stage = _start_stage(db, job, "validating")
        job.status = JobStatus.validating
        db.flush()

        gcode_text = gcode_bytes.decode("utf-8", errors="replace")
        report_v = validate_gcode(gcode_text, limits, source_name=generated.filename)
        result.validation = _persist_validation(db, job, report_v, gcode_blob.sha256)
        _finish_stage(
            db,
            stage,
            StageStatus.succeeded if report_v.passed else StageStatus.blocked,
            {"passed": report_v.passed, "severity_counts": report_v.severity_counts},
        )
        _event(
            db, job, report_v.summary, stage="validating",
            level="info" if report_v.passed else "error",
        )

        if not report_v.passed:
            # The file is stored for inspection but must never be downloadable.
            generated.is_blocked = True
            generated.blocked_reason = report_v.summary
            job.status = JobStatus.blocked
            job.error_code = "TOOLPATH_VALIDATION_FAILED"
            job.error_detail = report_v.summary
            job.finished_at = utcnow()
            db.flush()
            result.status = JobStatus.blocked
            return result

        # -- 6. toolpath -----------------------------------------------------
        stage = _start_stage(db, job, "parsing_toolpath")
        path = parse_toolpath(gcode_text, limits)
        result.toolpath_summary = {
            "layer_count": path.layer_count,
            "segments": len(path.segments),
            "segments_elided": path.elided,
            "stats": path.stats,
            "bounds": path.bounds,
        }
        _finish_stage(
            db, stage, StageStatus.succeeded, {"layers": path.layer_count, "segments": len(path.segments)}
        )
        _event(
            db, job, f"Toolpath parsed: {path.layer_count} layers, {len(path.segments):,} segments",
            stage="parsing_toolpath",
        )

        # -- 7. not yet implemented, reported honestly -------------------------
        for name, reason in (
            ("repairing", "Mesh repair is not implemented; the original upload is used unchanged."),
            ("simulating", "The print simulator is not implemented; no simulation frames are produced."),
        ):
            skipped = _start_stage(db, job, name)
            _finish_stage(db, skipped, StageStatus.skipped, {"reason": reason, "implemented": False})
            _event(db, job, f"{name} skipped: {reason}", stage=name, level="warning")

        job.status = JobStatus.completed
        job.finished_at = utcnow()
        job.input_fingerprint = configuration.fingerprint()
        db.flush()
        result.status = JobStatus.completed
        _event(db, job, "Pipeline complete and validated.")
        log.info(
            "pipeline.completed",
            job_id=str(job.id),
            engine=result.engine,
            duration_ms=int((time.perf_counter() - started) * 1000),
            layers=path.layer_count,
        )
        return result

    except AppError as exc:
        _fail_job(db, job, exc.code, str(exc), stages=_remaining(db, job))
        result.status = JobStatus.failed
        raise
    except Exception as exc:  # pragma: no cover - unexpected
        log.exception("pipeline.unexpected_failure", job_id=str(job.id))
        _fail_job(db, job, "INTERNAL_ERROR", str(exc), stages=_remaining(db, job))
        result.status = JobStatus.failed
        raise
