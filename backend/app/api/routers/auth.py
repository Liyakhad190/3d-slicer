"""Authentication routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DbSession
from app.core.errors import AuthenticationError, ConflictError
from app.core.logging import get_logger
from app.core.security import create_access_token, hash_password, verify_password
from app.core.validation import validate_password
from app.models.identity import AuditLog, User, UserRole
from app.schemas.api import LoginRequest, RegisterRequest, TokenResponse, UserResponse

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

MAX_FAILED_LOGINS = 10
LOCKOUT_MINUTES = 15


def _audit(db: DbSession, user: User | None, action: str, ip: str | None = None) -> None:
    db.add(
        AuditLog(
            actor_user_id=user.id if user else None,
            action=action,
            object_type="user",
            object_id=user.id if user else None,
        )
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and return an access token",
)
def register(payload: RegisterRequest, db: DbSession) -> TokenResponse:
    validate_password(payload.password)

    existing = db.execute(
        select(User).where(func.lower(User.email) == payload.email.lower())
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("An account with that email already exists.")

    user = User(
        email=payload.email.lower(),
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        role=UserRole.user.value,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        # Lost a race against a concurrent registration for the same address.
        db.rollback()
        raise ConflictError("An account with that email already exists.") from exc

    user.last_login_at = datetime.now(timezone.utc)
    _audit(db, user, "user.registered")

    token, expires_at = create_access_token(
        subject=user.id, role=user.role, email=user.email
    )
    log.info("auth.registered", user_id=str(user.id))
    return TokenResponse(
        access_token=token, expires_at=expires_at, user=UserResponse.model_validate(user)
    )


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for a token")
def login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    user = db.execute(
        select(User).where(func.lower(User.email) == payload.email.lower())
    ).scalar_one_or_none()

    # Identical error and roughly identical work for "no such user" and "wrong
    # password", so the response cannot be used to enumerate accounts.
    if user is None:
        verify_password(payload.password, hash_password("timing-equaliser"))
        raise AuthenticationError("Incorrect email or password.")

    now = datetime.now(timezone.utc)
    if user.locked_until and user.locked_until > now:
        raise AuthenticationError(
            "Account temporarily locked after repeated failed sign-in attempts.",
            details={"locked_until": user.locked_until.isoformat()},
        )

    if not verify_password(payload.password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_login_count = 0
            log.warning("auth.locked", user_id=str(user.id))
        _audit(db, user, "auth.login_failed")
        raise AuthenticationError("Incorrect email or password.")

    if not user.is_active or user.deleted_at is not None:
        raise AuthenticationError("This account is not active.")

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    _audit(db, user, "auth.login")

    token, expires_at = create_access_token(
        subject=user.id, role=user.role, email=user.email
    )
    return TokenResponse(
        access_token=token, expires_at=expires_at, user=UserResponse.model_validate(user)
    )


@router.get("/me", response_model=UserResponse, summary="Current account")
def me(user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(user)
