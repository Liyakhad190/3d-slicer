"""Model asset services: upload, listing, analysis orchestration.

The original upload is never mutated. Repairs, decimations and slicer outputs
all become *new* blobs referenced alongside it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, PayloadTooLargeError, ValidationFailedError
from app.core.logging import get_logger
from app.core.validation import sanitize_filename
from app.models.assets import Blob, Model, ModelAnalysis, ModelVersion
from app.models.identity import AuditLog, User
from app.services import storage
from app.services.formats import (
    ModelFormat,
    assert_safe_archive,
    capability_matrix,
    detect_format,
)
from app.services.geometry.analyzer import MeshAnalysisReport, analyse_file

log = get_logger(__name__)

#: Maps a detected format to the file extension trimesh expects.
_TRIMESH_FILE_TYPE: dict[ModelFormat, str | None] = {
    ModelFormat.stl_binary: "stl",
    ModelFormat.stl_ascii: "stl",
    ModelFormat.obj: "obj",
    ModelFormat.ply: "ply",
    ModelFormat.three_mf: "3mf",
    ModelFormat.amf: "amf",
    ModelFormat.glb: "glb",
    ModelFormat.gltf: "gltf",
    ModelFormat.step: None,
    ModelFormat.stp: None,
}


@dataclass(frozen=True)
class UploadResult:
    model: Model
    version: ModelVersion
    deduplicated: bool


def store_upload(
    db: Session,
    user: User,
    *,
    data: bytes,
    filename: str | None,
    display_name: str | None = None,
    max_upload_bytes: int,
    max_archive_ratio: int,
    max_archive_entries: int,
) -> UploadResult:
    """Validate, store and register an upload.

    Order matters: size is checked before anything reads the bytes, format is
    decided by *content*, and the blob is written only after both pass.
    """
    if len(data) > max_upload_bytes:
        raise PayloadTooLargeError(
            "The uploaded file exceeds the maximum allowed size.",
            details={"size_bytes": len(data), "limit_bytes": max_upload_bytes},
        )

    info = detect_format(data, filename=filename)
    if info.is_archive:
        assert_safe_archive(
            data, max_ratio=max_archive_ratio, max_entries=max_archive_entries
        )

    sanitized = sanitize_filename(filename, fallback=f"model{info.extension}")
    safe_name = sanitize_filename(display_name) if display_name else _stem(sanitized)

    blob, created = storage.put_blob(
        data,
        media_type=info.media_type,
        detected_format=info.format.value,
        session=db,
    )

    # An identical file already stored: reuse the existing model version rather
    # than creating a second logical model that the user cannot tell apart.
    existing = db.execute(
        select(ModelVersion).where(ModelVersion.sha256 == blob.sha256, ModelVersion.blob_id == blob.id)
    ).scalars().first()
    if existing is not None:
        owner = db.execute(
            select(Model).where(Model.id == existing.model_id, Model.user_id == user.id)
        ).scalar_one_or_none()
        if owner is not None:
            return UploadResult(model=owner, version=existing, deduplicated=True)

    model = Model(user_id=user.id, name=safe_name[:255])
    db.add(model)
    db.flush()

    next_version = (
        db.execute(
            select(func.coalesce(func.max(ModelVersion.version), 0)).where(
                ModelVersion.model_id == model.id
            )
        ).scalar_one()
        or 0
    ) + 1

    version = ModelVersion(
        model_id=model.id,
        version=next_version,
        original_filename=(filename or f"model{info.extension}")[:512],
        sanitized_filename=sanitized,
        blob_id=blob.id,
        format=info.format.value,
        byte_size=len(data),
        sha256=blob.sha256,
    )
    db.add(version)
    db.flush()

    model.current_version_id = version.id
    db.flush()

    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="model.uploaded",
            object_type="model_version",
            object_id=version.id,
            after={
                "sha256": blob.sha256,
                "format": info.format.value,
                "byte_size": len(data),
                "original_filename": (filename or "")[:255],
            },
        )
    )

    log.info(
        "model.uploaded",
        user_id=str(user.id),
        model_id=str(model.id),
        format=info.format.value,
        bytes=len(data),
        deduplicated=not created,
    )
    return UploadResult(model=model, version=version, deduplicated=not created)


def run_analysis(
    db: Session,
    *,
    version: ModelVersion,
    no_opening_mm: float = 0.4,
    max_overhang_deg: float = 45.0,
    force: bool = False,
) -> MeshAnalysisReport:
    """Analyse a stored model version, caching by content hash.

    Identical bytes produce an identical report, so the analysis is reused
    unless `force` is set. The analysis itself is computed on a temp file
    because trimesh needs a path for the fancier format readers.
    """
    blob = active_blob(db, version)
    if blob is None:  # pragma: no cover - FK guarantees presence
        raise NotFoundError("The stored model file could not be located.")

    if not force:
        cached = db.execute(
            select(ModelAnalysis).where(
                ModelAnalysis.model_version_id == version.id,
                ModelAnalysis.input_sha256 == blob.sha256,
            )
        ).scalars().first()
        if cached is not None:
            return _report_from_row(cached)

    import tempfile
    from pathlib import Path

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="analyse-") as tmp:
        tmp_path = Path(tmp) / f"input{Path(blob.storage_path).suffix or '.stl'}"
        tmp_path.write_bytes(storage.read_blob(blob))
        report = analyse_file(
            str(tmp_path),
            input_sha256=blob.sha256,
            model_version_id=str(version.id),
            no_opening_mm=no_opening_mm,
            max_overhang_deg=max_overhang_deg,
            file_type=_TRIMESH_FILE_TYPE.get(ModelFormat(blob.detected_format)),
        )
    report.duration_ms = int((time.perf_counter() - started) * 1000)

    m = report.metrics
    row = ModelAnalysis(
        model_version_id=version.id,
        engine=report.analyzer,
        engine_version=report.analyzer_version,
        input_sha256=report.input_sha256,
        report=report.to_dict(),
        printability_score=Decimal(str(report.printability_score)),
        risk_level=report.risk_level,
        findings_count_by_severity=report.score_breakdown.get("counts_by_severity", {}),
        duration_ms=report.duration_ms,
    )
    db.add(row)
    db.flush()

    version.analysis_status = "completed"
    version.triangle_count = m.triangle_count
    version.bbox_x = m.dimensions_mm["x"]
    version.bbox_y = m.dimensions_mm["y"]
    version.bbox_z = m.dimensions_mm["z"]
    db.flush()

    log.info(
        "model.analysed",
        model_version_id=str(version.id),
        triangles=m.triangle_count,
        score=report.printability_score,
        risk=report.risk_level,
        duration_ms=report.duration_ms,
    )
    return report


def active_blob(db: Session, version: ModelVersion) -> Blob:
    """Return the authoritative geometry, without changing the source upload."""
    blob_id = version.transformed_blob_id or version.blob_id
    blob = db.get(Blob, blob_id)
    if blob is None:  # pragma: no cover - foreign key protects persisted ids
        raise NotFoundError("The stored model file could not be located.")
    return blob


def transform_model(
    db: Session,
    *,
    version: ModelVersion,
    scale_percent: dict[str, float] | None = None,
    target_dimensions_mm: dict[str, float] | None = None,
    preserve_aspect_ratio: bool = False,
) -> MeshAnalysisReport:
    """Scale the active mesh, persist its bytes, and analyse those exact bytes."""
    import io

    import numpy as np
    import trimesh

    source = db.get(Blob, version.blob_id)
    if source is None:  # pragma: no cover - foreign key protects persisted ids
        raise NotFoundError("The stored model file could not be located.")
    file_type = _TRIMESH_FILE_TYPE.get(ModelFormat(source.detected_format))
    if file_type is None:
        raise ValidationFailedError("This model format cannot be transformed as triangle geometry.")

    loaded = trimesh.load(
        io.BytesIO(storage.read_blob(source)), force="mesh", process=False, file_type=file_type
    )
    if isinstance(loaded, trimesh.Scene):
        meshes = [geometry for geometry in loaded.geometry.values() if isinstance(geometry, trimesh.Trimesh)]
        if not meshes:
            raise ValidationFailedError("The model contains no triangle geometry to transform.")
        loaded = trimesh.util.concatenate(meshes)
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.vertices) == 0:
        raise ValidationFailedError("The model contains no triangle geometry to transform.")

    current_dimensions = np.asarray(loaded.extents, dtype=float)
    if not np.isfinite(current_dimensions).all() or np.any(current_dimensions <= 0):
        raise ValidationFailedError("The model has invalid dimensions and cannot be scaled.")

    if target_dimensions_mm is not None:
        factors = np.asarray(
            [target_dimensions_mm[axis] / current_dimensions[index]
             for index, axis in enumerate(("x", "y", "z"))],
            dtype=float,
        )
        mode = "target_dimensions_mm"
        requested: dict[str, Any] = {axis: target_dimensions_mm[axis] for axis in ("x", "y", "z")}
    else:
        percentages = scale_percent or {"x": 100.0, "y": 100.0, "z": 100.0}
        factors = np.asarray([percentages[axis] / 100 for axis in ("x", "y", "z")])
        mode = "scale_percent"
        requested = {axis: percentages[axis] for axis in ("x", "y", "z")}

    if preserve_aspect_ratio and not np.allclose(factors, factors[0], rtol=1e-6, atol=1e-8):
        from app.core.errors import ValidationFailedError

        raise ValidationFailedError(
            "Aspect-ratio lock requires the same scale on X, Y and Z.",
            details={"scale_factors": factors.tolist()},
        )

    loaded.apply_scale(factors)
    payload = loaded.export(file_type="stl")
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    transformed, _ = storage.put_blob(
        data,
        media_type="model/stl",
        detected_format=ModelFormat.stl_binary.value,
        session=db,
    )

    version.transformed_blob_id = transformed.id
    version.transformation = {
        "source_sha256": db.get(Blob, version.blob_id).sha256,
        "transformed_sha256": transformed.sha256,
        "scale_factors": {
            axis: float(factors[index]) for index, axis in enumerate(("x", "y", "z"))
        },
        "last_operation": {"mode": mode, "requested": requested,
                           "preserve_aspect_ratio": preserve_aspect_ratio},
    }
    version.analysis_status = "pending"
    db.flush()
    return run_analysis(db, version=version, force=True)


def reset_transformation(db: Session, *, version: ModelVersion) -> MeshAnalysisReport:
    """Restore the original upload as active geometry and remeasure it."""
    version.transformed_blob_id = None
    version.transformation = None
    version.analysis_status = "pending"
    db.flush()
    return run_analysis(db, version=version, force=True)


def _report_from_row(row: ModelAnalysis) -> MeshAnalysisReport:
    """Rebuild a report from a cached analysis row."""
    from app.services.geometry.analyzer import GeometryMetrics, MeshAnalysisReport

    payload = row.report
    metrics = GeometryMetrics(**payload["metrics"])
    return MeshAnalysisReport(
        model_version_id=payload.get("model_version_id"),
        input_sha256=payload["input_sha256"],
        analyzer=payload["analyzer"],
        analyzer_version=payload["analyzer_version"],
        trimesh_version=payload["trimesh_version"],
        metrics=metrics,
        findings=[],  # findings live in the raw dict for the API response
        printability_score=row.printability_score
        and float(row.printability_score)
        or float(payload.get("printability_score", 0.0)),
        risk_level=row.risk_level or "unknown",
        score_breakdown=payload.get("score_breakdown", {}),
        duration_ms=row.duration_ms,
    )


def _stem(filename: str) -> str:
    return filename.rsplit(".", 1)[0] or "model"


def list_models(db: Session, user: User, *, limit: int = 50, offset: int = 0) -> list[Model]:
    return list(
        db.execute(
            select(Model)
            .where(Model.user_id == user.id, Model.deleted_at.is_(None))
            .order_by(Model.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


def get_model(db: Session, user: User, model_id: uuid.UUID) -> Model:
    model = db.execute(
        select(Model).where(
            Model.id == model_id, Model.user_id == user.id, Model.deleted_at.is_(None)
        )
    ).scalar_one_or_none()
    if model is None:
        raise NotFoundError("The requested resource does not exist.")
    return model


def get_version(db: Session, user: User, model_version_id: uuid.UUID) -> ModelVersion:
    version = db.execute(
        select(ModelVersion)
        .join(Model, Model.id == ModelVersion.model_id)
        .where(ModelVersion.id == model_version_id, Model.user_id == user.id)
    ).scalar_one_or_none()
    if version is None:
        raise NotFoundError("The requested resource does not exist.")
    return version


def formats_matrix() -> list[dict[str, object]]:
    return capability_matrix()
