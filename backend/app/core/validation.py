"""Password policy and filename hygiene.

Two small, deliberately strict helpers. Both exist because "we'll validate it
later" is how traversal bugs and weak passwords get shipped.
"""

from __future__ import annotations

import re
import unicodedata

from app.core.errors import ValidationFailedError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
MAX_FILENAME_LENGTH = 255

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Anything outside this set collapses to a separator. This is a *display* name
# only: no code path ever uses it to build a filesystem path.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MULTI_SEPARATOR = re.compile(r"[-_]{2,}")


def validate_password(password: str) -> None:
    """Reject passwords that are trivially guessable or ambiguous.

    Deliberately not a composition rule (no "must contain a symbol"): length
    plus a blocklist of the most common passwords is the NIST-aligned approach
    and does not push users toward `Password1!`.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationFailedError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
            details={"field": "password", "min_length": MIN_PASSWORD_LENGTH},
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationFailedError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters.",
            details={"field": "password", "max_length": MAX_PASSWORD_LENGTH},
        )
    if password.strip() != password:
        raise ValidationFailedError(
            "Password must not begin or end with whitespace.",
            details={"field": "password"},
        )
    if password.lower() in {"password", "password123", "123456789012", "qwertyuiopas"}:
        raise ValidationFailedError(
            "That password is too common. Please choose another.",
            details={"field": "password"},
        )


def sanitize_filename(filename: str | None, *, fallback: str = "model") -> str:
    """Return a safe *display* filename.

    Strips directory components (both POSIX and Windows separators), normalises
    unicode, removes control characters and Windows-reserved device names, and
    bounds the length while keeping the extension.
    """
    raw = (filename or "").strip()
    if not raw:
        return fallback

    # Drop any directory component. Using both separators matters: a Windows
    # client can upload "a\\b.stl" and a POSIX server would not split on "\\".
    raw = raw.replace("\\", "/")
    raw = raw.rsplit("/", 1)[-1]

    raw = unicodedata.normalize("NFKC", raw)
    raw = "".join(ch for ch in raw if ch.isprintable())
    raw = _SAFE_CHARS.sub("-", raw).strip("-._")
    raw = _MULTI_SEPARATOR.sub("-", raw)

    if not raw:
        return fallback

    stem, dot, ext = raw.rpartition(".")
    if not dot:  # no extension
        stem, ext = raw, ""
    stem = stem.strip("-._") or fallback
    ext = ext.strip("-._").lower()

    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"

    name = f"{stem}.{ext}" if ext else stem
    if len(name) > MAX_FILENAME_LENGTH:
        keep = MAX_FILENAME_LENGTH - (len(ext) + 1 if ext else 0)
        name = f"{stem[:keep]}.{ext}" if ext else stem[:MAX_FILENAME_LENGTH]
    return name
