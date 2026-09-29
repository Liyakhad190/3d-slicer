"""FastAPI application factory.

`create_app()` builds the app, registers middleware and routers, attaches the
error handlers, and refuses to start if the configuration is unsafe. It returns
a real app object so tests can drive the full stack with `TestClient`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routers import auth, catalog, health, models, slicing
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import get_logger

log = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    problems = settings.validate_runtime()
    if problems:
        for problem in problems:
            log.error("startup.config_invalid", problem=problem)
        raise RuntimeError(
            "Refusing to start with an unsafe configuration:\n- "
            + "\n- ".join(problems)
        )

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blob_dir.mkdir(parents=True, exist_ok=True)

    # Production applies schema changes through `alembic upgrade head` in a
    # deploy step. Only the local SQLite convenience path auto-creates.
    if not settings.is_production:
        try:
            from app.db.schema import create_all_for_dev

            create_all_for_dev()
        except Exception as exc:
            log.error("startup.schema_init_failed", error=str(exc))

    from app.db.session import session_scope

    try:
        with session_scope() as session:
            from app.catalog.seed import seed_catalog

            counts = seed_catalog(session)
            log.info("startup.catalog_ready", **counts)
    except Exception as exc:
        # A missing catalog is not fatal for liveness, but it must be loud.
        log.error("startup.catalog_seed_failed", error=str(exc))

    log.info(
        "startup.ready",
        environment=settings.environment,
        api_prefix=settings.api_prefix,
        ai_provider=settings.ai_provider,
    )
    yield
    log.info("startup.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="AI Slicing Platform",
        version="0.1.0",
        description=(
            "AI-powered intelligent slicing and print-validation platform. The "
            "LLM is a reasoning and orchestration layer; deterministic geometry, "
            "slicing and validation remain authoritative for anything that reaches "
            "a machine."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER, "X-Content-SHA256", "Content-Disposition"],
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "request.unhandled_exception",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        finally:
            structlog.contextvars.clear_contextvars()

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        if not request.url.path.startswith(("/health", "/ready")):
            log.info(
                "request.completed",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
        return response

    _register_error_handlers(app)

    prefix = settings.api_prefix
    app.include_router(health.router)
    app.include_router(auth.router, prefix=prefix)
    app.include_router(models.router, prefix=prefix)
    app.include_router(catalog.router, prefix=prefix)
    app.include_router(slicing.router, prefix=prefix)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, Any]:
        return {
            "name": "AI Slicing Platform",
            "docs": "/docs",
            "api": prefix,
        }

    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.http_status >= 500:
            log.error("request.app_error", code=exc.code, message=exc.message)
        else:
            log.info("request.app_error", code=exc.code, message=exc.message)
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Report the loc and message, never the input value: an echoed password
        # or file payload in an error body is a needless leak.
        errors = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())[1:]) or "body",
                "message": err.get("msg", "Invalid value."),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "REQUEST_VALIDATION_FAILED",
                    "message": "The request body failed validation.",
                    "details": {"errors": errors},
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        codes = {401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND"}
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": codes.get(exc.status_code, f"HTTP_{exc.status_code}"),
                    "message": str(exc.detail),
                    "details": {},
                }
            },
            headers=getattr(exc, "headers", None),
        )


app = create_app()
