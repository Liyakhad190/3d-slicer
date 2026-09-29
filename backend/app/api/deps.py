"""Shared FastAPI dependencies: session, authentication, rate limiting."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, NotFoundError, RateLimitedError
from app.core.logging import get_logger
from app.core.security import decode_access_token
from app.db.session import get_session_factory
from app.models.identity import RateLimitBucket, User
from app.models.slicing import SliceJob

log = get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_db() -> Iterator[Session]:
    """Request-scoped session. Commits on success, rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Provide a bearer token in the Authorization header.")

    payload = decode_access_token(credentials.credentials)
    user = db.get(User, payload.subject)
    # A deleted or deactivated user is indistinguishable from a missing one:
    # returning 404 rather than 403 avoids confirming that the account exists.
    if user is None or not user.is_active or user.deleted_at is not None:
        raise NotFoundError("The requested resource does not exist.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise AuthenticationError("Administrator role required.")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def client_ip(request: Request) -> str:
    """Best-effort client address.

    `X-Forwarded-For` is only honoured when the app is not in development,
    because outside a trusted proxy that header is attacker-controlled and
    would let anyone bypass the rate limiter.
    """
    settings = get_settings()
    if not settings.is_production:
        return request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    real_ip = request.headers.get("x-real-ip", "")
    if real_ip:
        return real_ip.strip()[:64]
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(
    db: Session,
    *,
    scope: str,
    identifier: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Fixed-window limiter, persisted so it survives worker processes.

    The window is floored to the minute so the bucket key is stable: with a
    300 s upload window, truncating to 60 s granularity still enforces the
    limit within a factor of five.
    """
    now = datetime.now(timezone.utc)
    window_start = now.replace(second=0, microsecond=0) - timedelta(
        minutes=(now.minute % max(window_seconds // 60, 1))
    )

    bucket = db.execute(
        select(RateLimitBucket).where(
            RateLimitBucket.scope == scope,
            RateLimitBucket.identifier == identifier,
            RateLimitBucket.window_start == window_start,
        )
    ).scalar_one_or_none()

    if bucket is None:
        # Opportunistic cleanup so the table does not grow without bound.
        db.execute(
            delete(RateLimitBucket).where(
                RateLimitBucket.scope == scope,
                RateLimitBucket.window_start < window_start - timedelta(hours=2),
            )
        )
        bucket = RateLimitBucket(
            scope=scope, identifier=identifier, window_start=window_start, count=1
        )
        db.add(bucket)
    else:
        bucket.count += 1

    db.flush()
    if bucket.count > limit:
        retry_after = int((window_start + timedelta(seconds=window_seconds) - now).total_seconds())
        log.warning("rate_limit.exceeded", scope=scope, count=bucket.count, limit=limit)
        raise RateLimitedError(
            "Too many requests. Please wait before trying again.",
            details={"scope": scope, "retry_after_seconds": max(retry_after, 1)},
        )


def rate_limit(
    scope: str,
    *,
    limit_attr: str = "rate_limit_requests",
    window_attr: str = "rate_limit_window_seconds",
):
    """Dependency factory: `Depends(rate_limit("uploads"))`.

    The limiter writes to the database, so it needs its own short-lived
    transaction. It must not borrow the request's session: the counter is
    written *before* the handler runs, and on SQLite an unclosed write
    transaction holds the single write lock and deadlocks the request that is
    supposed to follow it.
    """

    def dependency(request: Request, settings: SettingsDep) -> None:
        if not settings.rate_limit_enabled:
            return
        from app.db.session import session_scope

        with session_scope() as limiter_session:
            enforce_rate_limit(
                limiter_session,
                scope=scope,
                identifier=client_ip(request),
                limit=getattr(settings, limit_attr),
                window_seconds=getattr(settings, window_attr),
            )

    return dependency


# --------------------------------------------------------------------------- #
# ownership guards
# --------------------------------------------------------------------------- #


def owned_model_version(db: Session, user: User, model_version_id: uuid.UUID):
    """Fetch a model version owned by `user`, or 404.

    Ownership is resolved through the join rather than a separate query so a
    cross-tenant id produces the same response as a nonexistent one.
    """
    from app.models.assets import Model, ModelVersion

    row = db.execute(
        select(ModelVersion)
        .join(Model, Model.id == ModelVersion.model_id)
        .where(ModelVersion.id == model_version_id, Model.user_id == user.id)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("The requested resource does not exist.")
    return row


def owned_job(db: Session, user: User, job_id: uuid.UUID) -> SliceJob:
    job = db.execute(
        select(SliceJob).where(SliceJob.id == job_id, SliceJob.user_id == user.id)
    ).scalar_one_or_none()
    if job is None:
        raise NotFoundError("The requested resource does not exist.")
    return job


def owned_printer(db: Session, user: User, printer_id: uuid.UUID):
    from app.models.catalog import Printer

    printer = db.execute(
        select(Printer).where(Printer.id == printer_id, Printer.user_id == user.id)
    ).scalar_one_or_none()
    if printer is None:
        raise NotFoundError("The requested resource does not exist.")
    return printer
