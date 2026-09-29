"""Password hashing and token issuing.

Argon2id via `argon2-cffi` with parameters baked into the stored hash, so cost
can be raised later without invalidating existing passwords.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import get_settings
from app.core.errors import AuthenticationError

_hasher: PasswordHasher | None = None


def get_hasher() -> PasswordHasher:
    global _hasher
    if _hasher is None:
        settings = get_settings()
        _hasher = PasswordHasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_cost_kib,
            parallelism=settings.argon2_parallelism,
        )
    return _hasher


def hash_password(password: str) -> str:
    return get_hasher().hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return get_hasher().verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash was made with weaker parameters than current."""
    try:
        return get_hasher().check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def generate_api_token() -> tuple[str, str]:
    """Return `(plaintext, sha256_hex)`. Only the hash is ever stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_api_token(raw)


def hash_api_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TokenPayload:
    subject: uuid.UUID
    role: str
    email: str
    expires_at: datetime
    token_type: str = "access"


def create_access_token(*, subject: uuid.UUID, role: str, email: str) -> tuple[str, datetime]:
    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=settings.access_token_ttl_seconds
    )
    claims: dict[str, Any] = {
        "sub": str(subject),
        "role": role,
        "email": email,
        "type": "access",
        "iat": datetime.now(timezone.utc),
        "exp": expires_at,
        "iss": settings.app_name,
    }
    token = jwt.encode(claims, settings.auth_secret, algorithm=settings.auth_algorithm)
    return token, expires_at


def decode_access_token(token: str) -> TokenPayload:
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.auth_secret,
            algorithms=[settings.auth_algorithm],
            issuer=settings.app_name,
            options={"require": ["exp", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Your session has expired. Please sign in again.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid authentication token.") from exc

    if claims.get("type") != "access":
        raise AuthenticationError("Invalid token type.")

    try:
        subject = uuid.UUID(str(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Malformed token subject.") from exc

    return TokenPayload(
        subject=subject,
        role=str(claims.get("role", "user")),
        email=str(claims.get("email", "")),
        expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
    )
