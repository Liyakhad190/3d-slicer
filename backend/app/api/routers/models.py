"""Model asset routes: upload, list, fetch, analyse, download."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser, DbSession, SettingsDep, rate_limit
from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.api import (
    AnalysisResponse,
    FormatCapability,
    ModelResponse,
    ModelVersionResponse,
    ModelTransformRequest,
    ModelTransformResponse,
    UploadResponse,
)
from app.services import models_service, storage

log = get_logger(__name__)
router = APIRouter(prefix="/models", tags=["models"])

UPLOAD_LIMIT = Depends(rate_limit("uploads", limit_attr="upload_rate_limit_requests",
                                  window_attr="upload_rate_limit_window_seconds"))


@router.get(
    "/formats",
    response_model=list[FormatCapability],
    summary="Read/write capability matrix per format",
)
def formats(_: CurrentUser) -> list[FormatCapability]:
    return [FormatCapability(**row) for row in models_service.formats_matrix()]


@router.post(
    "",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[UPLOAD_LIMIT],
    summary="Upload a 3D model (STL/OBJ/3MF/AMF/PLY/GLB/GLTF)",
)
async def upload_model(
    db: DbSession,
    settings: SettingsDep,
    user: CurrentUser,
    file: UploadFile = File(..., description="The model file. Format is detected from content."),
    name: str | None = Form(default=None, description="Display name; defaults to the filename."),
) -> UploadResponse:
    data = await file.read()
    result = models_service.store_upload(
        db,
        user,
        data=data,
        filename=file.filename,
        display_name=name,
        max_upload_bytes=settings.max_upload_bytes,
        max_archive_ratio=settings.max_archive_ratio,
        max_archive_entries=settings.max_archive_entries,
    )
    return UploadResponse(
        model=ModelResponse.model_validate(result.model),
        version=ModelVersionResponse.model_validate(result.version),
        deduplicated=result.deduplicated,
    )


@router.get("", response_model=list[ModelResponse], summary="List your models")
def list_models(
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ModelResponse]:
    rows = models_service.list_models(db, user, limit=limit, offset=offset)
    return [ModelResponse.model_validate(row) for row in rows]


@router.get("/{model_id}", response_model=ModelResponse, summary="Fetch one model")
def get_model(model_id: uuid.UUID, db: DbSession, user: CurrentUser) -> ModelResponse:
    return ModelResponse.model_validate(models_service.get_model(db, user, model_id))


@router.get(
    "/{model_id}/versions",
    response_model=list[ModelVersionResponse],
    summary="Version history for a model",
)
def list_versions(
    model_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> list[ModelVersionResponse]:
    model = models_service.get_model(db, user, model_id)
    return [ModelVersionResponse.model_validate(v) for v in model.versions]


@router.get(
    "/versions/{version_id}/file",
    summary="Download the model file",
    response_description="Raw model bytes",
    responses={200: {"content": {"application/octet-stream": {}}}},
)
def download_version(
    version_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> Response:
    version = models_service.get_version(db, user, version_id)
    blob = models_service.active_blob(db, version)
    data = storage.read_blob(blob)
    # The stored name is data, not a path; quote it properly in the header.
    return Response(
        content=data,
        media_type=blob.media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{version.sanitized_filename.rsplit(".", 1)[0]}.stl"'
                if version.transformed_blob_id
                else f'attachment; filename="{version.sanitized_filename}"'
            ),
            "X-Content-SHA256": blob.sha256,
        },
    )


@router.post(
    "/versions/{version_id}/analyze",
    response_model=AnalysisResponse,
    summary="Run deterministic geometry analysis",
)
def analyze(
    version_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    force: bool = Query(default=False, description="Re-run even if cached."),
    nozzle_mm: float = Query(default=0.4, gt=0, le=2.0),
    max_overhang_deg: float = Query(default=45.0, ge=0, le=90),
) -> AnalysisResponse:
    version = models_service.get_version(db, user, version_id)
    report = models_service.run_analysis(
        db,
        version=version,
        no_opening_mm=nozzle_mm,
        max_overhang_deg=max_overhang_deg,
        force=force,
    )
    payload = report.to_dict()
    return AnalysisResponse.model_validate(payload)


@router.post(
    "/versions/{version_id}/transform",
    response_model=ModelTransformResponse,
    summary="Scale or reset a model and re-analyse the resulting geometry",
)
def transform(
    version_id: uuid.UUID,
    body: ModelTransformRequest,
    db: DbSession,
    user: CurrentUser,
) -> ModelTransformResponse:
    version = models_service.get_version(db, user, version_id)
    report = (
        models_service.reset_transformation(db, version=version)
        if body.reset
        else models_service.transform_model(
            db,
            version=version,
            scale_percent=body.scale_percent,
            target_dimensions_mm=body.target_dimensions_mm,
            preserve_aspect_ratio=body.preserve_aspect_ratio,
        )
    )
    return ModelTransformResponse(
        version=ModelVersionResponse.model_validate(version),
        analysis=AnalysisResponse.model_validate(report.to_dict()),
        dimensions_mm=report.metrics.dimensions_mm,
    )
