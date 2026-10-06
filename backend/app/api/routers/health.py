"""Health and readiness routes."""

from __future__ import annotations

import shutil
import time
from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select, text

from app.api.deps import DbSession, SettingsDep
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.slicing import JobStatus, SlicerEngine, SliceJob

log = get_logger(__name__)
router = APIRouter(tags=["system"])

STARTED_AT = time.time()

ACTIVE_STATUSES = (
    JobStatus.queued,
    JobStatus.analyzing,
    JobStatus.repairing,
    JobStatus.optimizing,
    JobStatus.slicing,
    JobStatus.validating,
    JobStatus.simulating,
)


@router.get("/health", summary="Liveness probe")
def health() -> dict[str, Any]:
    return {"status": "ok", "uptime_seconds": round(time.time() - STARTED_AT, 1)}


@router.get("/ready", summary="Readiness probe with dependency checks")
def ready(db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    checks: dict[str, Any] = {}

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {
            "ok": True,
            "backend": "sqlite" if settings.is_sqlite else "postgresql",
        }
    except Exception as exc:  # pragma: no cover
        checks["database"] = {"ok": False, "error": str(exc)}

    try:
        usage = shutil.disk_usage(settings.data_dir)
        checks["storage"] = {
            "ok": usage.free > 1_000_000_000,
            "free_bytes": usage.free,
            "path": str(settings.data_dir),
        }
    except Exception as exc:  # pragma: no cover
        checks["storage"] = {"ok": False, "error": str(exc)}

    engine = db.execute(select(SlicerEngine).order_by(SlicerEngine.created_at).limit(1)).scalar_one_or_none()
    engine_ok = bool(engine and engine.is_available)
    checks["slicer"] = {
        "ok": engine_ok,
        "engine": engine.key if engine else None,
        "version": engine.engine_version if engine else None,
        "note": (
            None
            if engine_ok
            else "No slicing engine has been probed and marked available. Slicing "
            "is unavailable until one is registered via POST /admin/slicer-engines/probe."
        ),
    }

    queued = db.execute(
        select(func.count(SliceJob.id)).where(SliceJob.status.in_(ACTIVE_STATUSES))
    ).scalar_one()
    checks["queue"] = {"ok": True, "jobs_active": int(queued)}

    ok = all(entry.get("ok") for entry in checks.values())
    return {"status": "ready" if ok else "degraded", "checks": checks}


@router.get("/version", summary="Build and version information")
def version() -> dict[str, Any]:
    settings = get_settings()
    return {
        "app": settings.app_name,
        "environment": settings.environment,
        "api_prefix": settings.api_prefix,
        "ai_provider": settings.llm_provider,
        "ai_model": settings.ollama_model if settings.llm_provider == "ollama" else settings.ai_model,
        "debug": settings.debug,
    }
