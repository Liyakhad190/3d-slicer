"""The LLM provider port.

A provider does exactly one thing: turn a prompt plus an optional JSON schema
into structured JSON. It does not know what a printer is, does not see the
database, and cannot execute anything.

That boundary is the security property. There is no path from model output to a
shell, a filesystem, or a machine: the only thing that crosses this interface is
a JSON object, which is then validated by `app/intelligence/schemas.py` and
ultimately by the deterministic constraint validator before it can influence a
`PrintConfiguration`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: Stable, machine-readable failure codes. These reach the API response, so the
#: frontend can branch on them instead of parsing prose.
LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
LLM_TIMEOUT = "LLM_TIMEOUT"
LLM_INVALID_OUTPUT = "LLM_INVALID_OUTPUT"
LLM_PROVIDER_ERROR = "LLM_PROVIDER_ERROR"
LLM_NOT_CONFIGURED = "LLM_NOT_CONFIGURED"
LLM_MODEL_MISSING = "LLM_MODEL_MISSING"


class LLMError(Exception):
    """A provider failure with a code the API can surface verbatim."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        recoverable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.recoverable = recoverable

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "recoverable": self.recoverable,
        }


@dataclass(frozen=True)
class LLMRequest:
    """One generation request.

    `json_schema` is the whole point of this class. When present the provider
    must constrain generation to that schema. Ollama honours this natively via
    its `format` field, which is what makes structured output reliable on a
    small local model; providers without the capability must still return
    valid JSON or fail, never prose.
    """

    prompt: str
    json_schema: dict[str, Any] | None = None
    system: str | None = None
    temperature: float = 0.0
    max_tokens: int = 512
    #: Free-form label for logs and traces. Never sent to the model.
    purpose: str = "generic"
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class LLMResponse:
    raw_text: str
    parsed: dict[str, Any] | None
    model: str
    provider: str
    duration_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    #: True when the provider enforced `json_schema` during generation.
    schema_enforced: bool = False
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "schema_enforced": self.schema_enforced,
            "attempts": self.attempts,
        }


@dataclass(frozen=True)
class ProviderHealth:
    """The result of probing a provider. Never inferred from config alone."""

    provider: str
    available: bool
    model: str
    status: str
    code: str | None = None
    message: str = ""
    latency_ms: int | None = None
    models_available: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "available": self.available,
            "model": self.model,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "latency_ms": self.latency_ms,
            "models_available": self.models_available,
        }


@runtime_checkable
class LLMProvider(Protocol):
    """The port every provider implements."""

    name: str

    def health(self) -> ProviderHealth:
        """Probe the provider for real. Must not be a config echo."""
        ...

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate JSON. Raises `LLMError` on any failure."""
        ...

    def list_models(self) -> list[str]:
        ...
