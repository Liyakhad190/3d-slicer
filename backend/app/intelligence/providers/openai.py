"""OpenAI-compatible provider. Optional; not the default.

Included so `LLM_PROVIDER=openai` is a real, working option rather than a
placeholder. It speaks the standard chat-completions API and uses
`response_format={"type": "json_schema"}` for structured output, with a
`{"type": "json_object"}` fallback for servers that only support the looser
form.

Not exercised by the default test suite because no key is configured. Its
absence must not degrade the Ollama path in any way.
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
    LLM_INVALID_OUTPUT,
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


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = (settings.ai_base_url or "https://api.openai.com/v1").rstrip("/")
        self._model = settings.ai_model or "gpt-4o-mini"
        self._api_key = settings.ai_api_key

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise LLMError(
                LLM_NOT_CONFIGURED,
                "AI_API_KEY is not set but LLM_PROVIDER=openai.",
                details={"provider": self.name},
            )
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

    def _post(self, path: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMError(
                LLM_PROVIDER_ERROR,
                f"OpenAI-compatible endpoint returned HTTP {exc.code}.",
                details={"status": exc.code, "body": exc.read()[:400].decode("utf-8", "replace")},
            ) from exc
        except (socket.timeout, urllib.error.URLError) as exc:
            raise LLMError(
                LLM_UNAVAILABLE if isinstance(exc, urllib.error.URLError) else LLM_TIMEOUT,
                f"Cannot reach {self._base_url}: {exc}",
                details={"base_url": self._base_url},
            ) from exc

    def _get(self, path: str, timeout: int) -> dict[str, Any]:
        request = urllib.request.Request(f"{self._base_url}{path}", headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMError(
                LLM_PROVIDER_ERROR, f"HTTP {exc.code}.", details={"status": exc.code}
            ) from exc
        except (socket.timeout, urllib.error.URLError) as exc:
            raise LLMError(
                LLM_UNAVAILABLE, f"Cannot reach {self._base_url}.", details={"reason": str(exc)}
            ) from exc

    def list_models(self) -> list[str]:
        try:
            payload = self._get("/models", timeout=10)
        except LLMError:
            return []
        return [
            str(entry["id"])
            for entry in payload.get("data", [])
            if isinstance(entry, dict) and entry.get("id")
        ]

    def health(self) -> ProviderHealth:
        if not self._api_key:
            return ProviderHealth(
                provider=self.name, available=False, model=self._model,
                status="unavailable", code=LLM_NOT_CONFIGURED,
                message="AI_API_KEY is not set.",
            )
        started = time.perf_counter()
        try:
            models = self.list_models()
        except LLMError as exc:
            return ProviderHealth(
                provider=self.name, available=False, model=self._model,
                status="unavailable", code=exc.code, message=exc.message,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        latency = int((time.perf_counter() - started) * 1000)
        if self._model not in models:
            return ProviderHealth(
                provider=self.name, available=False, model=self._model,
                status="unavailable", code="LLM_MODEL_MISSING",
                message=f"Model {self._model!r} is not available to this key.",
                latency_ms=latency, models_available=models[:50],
            )
        return ProviderHealth(
            provider=self.name, available=True, model=self._model,
            status="ready", latency_ms=latency, models_available=models[:50],
        )

    def generate(self, request: LLMRequest) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        schema_enforced = False
        if request.json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "schema": request.json_schema,
                    "strict": True,
                },
            }
            schema_enforced = True

        timeout = request.timeout_seconds or self._settings.llm_timeout_seconds
        started = time.perf_counter()
        try:
            result = self._post("/chat/completions", payload, timeout=timeout)
        except LLMError:
            # Some compatible servers reject json_schema; retry once loosely
            # rather than reporting a provider error for a capability mismatch.
            if schema_enforced:
                payload.pop("response_format", None)
                payload["response_format"] = {"type": "json_object"}
                result = self._post("/chat/completions", payload, timeout=timeout)
                schema_enforced = False
            else:
                raise
        duration_ms = int((time.perf_counter() - started) * 1000)

        choices = result.get("choices") or []
        if not choices:
            raise LLMError(LLM_PROVIDER_ERROR, "No choices in the completion response.")
        text = str((choices[0].get("message") or {}).get("content") or "").strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(
                LLM_INVALID_OUTPUT,
                "The model did not return valid JSON.",
                details={"raw_preview": text[:400]},
            ) from exc
        if not isinstance(parsed, dict):
            raise LLMError(LLM_INVALID_OUTPUT, "The model returned a non-object JSON value.")

        usage = result.get("usage") or {}
        return LLMResponse(
            raw_text=text,
            parsed=parsed,
            model=self._model,
            provider=self.name,
            duration_ms=duration_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            schema_enforced=schema_enforced,
        )
