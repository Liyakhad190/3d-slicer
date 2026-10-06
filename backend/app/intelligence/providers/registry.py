"""Provider registry.

`get_llm_provider()` is the only way the rest of the application reaches a
model. Swapping providers is a configuration change; nothing else in the
codebase knows which backend is in use.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.errors import InsufficientInformationError
from app.core.logging import get_logger
from app.intelligence.providers.base import LLMError, LLMProvider

log = get_logger(__name__)

SUPPORTED_PROVIDERS = ("ollama", "openai")

_cached: dict[str, LLMProvider] = {}


def build_provider(settings: Settings) -> LLMProvider:
    """Construct the configured provider. Raises on an unknown provider."""
    name = settings.llm_provider.strip().lower()
    if name == "ollama":
        from app.intelligence.providers.ollama import OllamaProvider

        return OllamaProvider(settings)
    if name == "openai":
        from app.intelligence.providers.openai import OpenAIProvider

        return OpenAIProvider(settings)
    raise InsufficientInformationError(
        f"Unknown LLM provider {settings.llm_provider!r}.",
        details={"supported": list(SUPPORTED_PROVIDERS)},
    )


def get_llm_provider(settings: Settings | None = None) -> LLMProvider:
    """The process-wide provider instance, rebuilt if the config changes."""
    settings = settings or get_settings()
    key = f"{settings.llm_provider}:{settings.ollama_base_url}:{settings.ollama_model}"
    provider = _cached.get(key)
    if provider is None:
        provider = build_provider(settings)
        _cached.clear()
        _cached[key] = provider
        log.info("llm.provider_resolved", provider=provider.name)
    return provider


def reset_provider_cache() -> None:
    """Test hook, so a settings change takes effect immediately."""
    _cached.clear()


__all__ = [
    "LLMError",
    "LLMProvider",
    "SUPPORTED_PROVIDERS",
    "build_provider",
    "get_llm_provider",
    "reset_provider_cache",
]
