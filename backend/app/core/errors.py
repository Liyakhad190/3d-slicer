"""Typed application errors.

Every error carries a stable machine-readable `code` so the frontend can branch
on it, and the API renders a consistent envelope:

    {"error": {"code": "NOT_FOUND", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from typing import Any

from fastapi import status


class AppError(Exception):
    """Base class for all deliberate, expected failures."""

    code: str = "INTERNAL_ERROR"
    http_status: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details or {}
        if code:
            self.code = code
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class NotFoundError(AppError):
    code = "NOT_FOUND"
    http_status = status.HTTP_404_NOT_FOUND
    message = "The requested resource does not exist."


class ValidationFailedError(AppError):
    code = "VALIDATION_FAILED"
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "The request was well-formed but semantically invalid."


class AuthenticationError(AppError):
    code = "UNAUTHENTICATED"
    http_status = status.HTTP_401_UNAUTHORIZED
    message = "Authentication is required."


class PermissionDeniedError(AppError):
    code = "PERMISSION_DENIED"
    http_status = status.HTTP_403_FORBIDDEN
    message = "You do not have access to this resource."


class ConflictError(AppError):
    code = "CONFLICT"
    http_status = status.HTTP_409_CONFLICT
    message = "The resource is in a conflicting state."


class RateLimitedError(AppError):
    code = "RATE_LIMITED"
    http_status = status.HTTP_429_TOO_MANY_REQUESTS
    message = "Too many requests. Please slow down."


class PayloadTooLargeError(AppError):
    code = "PAYLOAD_TOO_LARGE"
    http_status = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    message = "The uploaded file exceeds the configured size limit."


class UnsupportedFormatError(AppError):
    code = "UNSUPPORTED_FORMAT"
    http_status = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    message = "That file format is not supported."


class InsufficientInformationError(AppError):
    """Raised when the system refuses to guess.

    This is a first-class error, not a fallback trigger. Per the no-hallucination
    policy, when a required profile is missing the platform stops and asks for
    it rather than interpolating a plausible value.
    """

    code = "INSUFFICIENT_INFORMATION"
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "Insufficient information to safely determine this."


class UnsafeOutputError(AppError):
    """A validation gate refused to allow output to be produced or served."""

    code = "UNSAFE_OUTPUT"
    http_status = status.HTTP_409_CONFLICT
    message = "Output was blocked because it failed a mandatory safety validation."


class SlicingError(AppError):
    code = "SLICING_FAILED"
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    message = "The slicing engine failed to produce output."


class ExternalToolError(AppError):
    """A subprocess (e.g. CuraEngine) failed, timed out, or misbehaved."""

    code = "EXTERNAL_TOOL_FAILED"
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    message = "An external tool failed to complete."
