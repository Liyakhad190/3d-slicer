"""Ollama provider — the default, local, and key-free.

Talks to a local `ollama serve` over its HTTP API. Two behaviours were measured
against a real `qwen2.5:1.5b` and they drive this implementation:

1. `format="json"` is **not** sufficient. The model wraps its output in ```json
   fences and truncates it. Passing a full JSON Schema to `format` makes Ollama
   constrain decoding, and the result is valid JSON with exactly the declared
   keys.
2. That schema constrains *structure*, not *values*. With
   `"minimum": 0, "maximum": 1` declared, the model still returned `5`.
   Numeric ranges are validated by `app/intelligence/schemas.py` and are never
   trusted from here.

No key management exists, because a local Ollama has no keys.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.intelligence.providers.base import (
    LLM_MODEL_MISSING,
    LLM_NOT_CONFIGURED,
    LLM_PROVIDER_ERROR,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    LLMError,
    LLMRequest,
    LLMResponse,
    ProviderHealth,
)

log = get_logger(__name__)

#: A JSON value lifted out of a ```json fence, for providers that ignore
#: `format` or a model that fences anyway. Never applied blindly: the content
#: must still parse as JSON before it is used.
_FENCE_PREFIXES = ("```json", "```JSON", "```")


class OllamaProvider:
    """Talks to a local Ollama daemon."""

    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._model = settings.ollama_model

    # -- plumbing --------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _post_json(self, path: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        request = urllib.request.Request(
            self._url(path),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            raise LLMError(
                LLM_PROVIDER_ERROR,
                f"Ollama returned HTTP {exc.code}.",
                details={"status": exc.code, "body": detail, "url": self._url(path)},
            ) from exc
        except socket.timeout as exc:
            raise LLMError(
                LLM_TIMEOUT,
                f"Ollama did not respond within {timeout}s.",
                details={"timeout_seconds": timeout},
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMError(
                LLM_UNAVAILABLE,
                f"Cannot reach Ollama at {self._base_url}. Is `ollama serve` running?",
                details={"reason": str(exc.reason), "base_url": self._base_url},
            ) from exc
        except json.JSONDecodeError as exc:
            raise LLMError(
                LLM_PROVIDER_ERROR,
                "Ollama returned a body that is not JSON.",
                details={"url": self._url(path)},
            ) from exc

    def _get_json(self, path: str, timeout: int) -> dict[str, Any]:
        request = urllib.request.Request(self._url(path), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMError(
                LLM_PROVIDER_ERROR,
                f"Ollama returned HTTP {exc.code}.",
                details={"status": exc.code},
            ) from exc
        except (socket.timeout, urllib.error.URLError) as exc:
            raise LLMError(
                LLM_UNAVAILABLE,
                f"Cannot reach Ollama at {self._base_url}. Is `ollama serve` running?",
                details={"reason": str(getattr(exc, "reason", exc))},
            ) from exc

    # -- port ------------------------------------------------------------

    def list_models(self) -> list[str]:
        try:
            payload = self._get_json("/api/tags", timeout=10)
        except LLMError:
            return []
        names: list[str] = []
        for entry in payload.get("models", []) or []:
            if isinstance(entry, dict) and entry.get("name"):
                names.append(str(entry["name"]))
        return names

    def health(self) -> ProviderHealth:
        """Probe for real: reach the daemon, then confirm the model is present.

        This must never be a config echo. A configured model that is not
        installed is `unavailable` with a distinct code, because that is the
        failure the user actually hits.
        """
        if not self._base_url:
            return ProviderHealth(
                provider=self.name, available=False, model=self._model,
                status="unavailable", code=LLM_NOT_CONFIGURED,
                message="OLLAMA_BASE_URL is not configured.",
            )

        started = time.perf_counter()
        try:
            payload = self._get_json("/api/tags", timeout=10)
        except LLMError as exc:
            return ProviderHealth(
                provider=self.name, available=False, model=self._model,
                status="unavailable", code=exc.code, message=exc.message,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        latency = int((time.perf_counter() - started) * 1000)

        available = [
            str(entry.get("name"))
            for entry in payload.get("models", []) or []
            if isinstance(entry, dict) and entry.get("name")
        ]
        # Ollama reports "qwen2.5:1.5b"; a request for "qwen2.5" should match.
        def matches(configured: str, installed: str) -> bool:
            return installed == configured or installed.split(":")[0] == configured.split(":")[0]
        if not any(matches(self._model, name) for name in available):
            return ProviderHealth(
                provider=self.name, available=False, model=self._model,
                status="unavailable", code=LLM_MODEL_MISSING,
                message=(
                    f"Model {self._model!r} is not installed. "
                    f"Run: ollama pull {self._model}"
                ),
                latency_ms=latency, models_available=available,
            )

        return ProviderHealth(
            provider=self.name, available=True, model=self._model,
            status="ready", latency_ms=latency, models_available=available,
        )

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate constrained JSON.

        A single attempt. The provider does not retry: retry policy belongs to
        the caller, which knows whether a second attempt is worth its latency.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": request.prompt,
            "stream": False,
            "keep_alive": self._settings.ollama_keep_alive,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
            },
        }
        if request.system:
            # Ollama has no separate system role on /api/generate, so the system
            # prompt is prepended. It is instructions, not conversation.
            payload["prompt"] = f"{request.system}\n\n{request.prompt}"
        if request.json_schema is not None:
            payload["format"] = request.json_schema

        timeout = request.timeout_seconds or self._settings.llm_timeout_seconds
        started = time.perf_counter()
        result = self._post_json("/api/generate", payload, timeout=timeout)
        duration_ms = int((time.perf_counter() - started) * 1000)

        raw_text = str(result.get("response", ""))
        parsed = _parse_json(raw_text)

        if parsed is None:
            raise LLMError(
                LLM_PROVIDER_ERROR,
                "Ollama returned a response that is not valid JSON.",
                details={
                    "model": self._model,
                    "schema_enforced": request.json_schema is not None,
                    "raw_preview": raw_text[:400],
                },
            )

        return LLMResponse(
            raw_text=raw_text,
            parsed=parsed,
            model=self._model,
            provider=self.name,
            duration_ms=duration_ms,
            prompt_tokens=result.get("prompt_eval_count"),
            completion_tokens=result.get("eval_count"),
            schema_enforced=request.json_schema is not None,
            attempts=1,
        )


def _parse_json(text: str) -> dict[str, Any] | None:
    """Parse JSON, tolerating only a fenced wrapper.

    The tolerance is narrow on purpose: stripping prose to find a JSON object
    would let a model smuggle text past the parser and have it silently
    discarded. A fenced block is a formatting artefact, not content.
    """
    candidate = text.strip()
    for prefix in _FENCE_PREFIXES:
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    candidate = candidate.rstrip()
    if candidate.endswith("```"):
        candidate = candidate[:-3]
    candidate = candidate.strip()
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
