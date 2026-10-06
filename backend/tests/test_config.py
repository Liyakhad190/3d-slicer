"""Configuration tests.

Mostly regression tests for values a user is expected to type by hand. The
CORS case is here because the bug it guards was invisible until someone
followed the README: `cp .env.example .env` produced a file the app refused to
start with, because pydantic-settings JSON-decoded a bare origin list and a bare
origin is not valid JSON.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings


def build(**overrides) -> Settings:
    """Settings with no `.env` read, so the developer's file cannot affect it."""
    return Settings(_env_file=None, **overrides)


class TestCorsOrigins:
    def test_a_single_bare_origin_loads(self):
        """Exactly the line `.env.example` ships."""
        assert build(cors_origins="http://localhost:5173").cors_origins == [
            "http://localhost:5173"
        ]

    def test_a_comma_separated_list_loads(self):
        settings = build(cors_origins="https://a.example, https://b.example")
        assert settings.cors_origins == ["https://a.example", "https://b.example"]

    def test_a_json_array_still_loads(self):
        """Deployment tooling tends to emit one, so it must keep working."""
        settings = build(cors_origins='["https://a.example", "https://b.example"]')
        assert settings.cors_origins == ["https://a.example", "https://b.example"]

    def test_blank_entries_are_dropped(self):
        settings = build(cors_origins="https://a.example, ,https://b.example,")
        assert settings.cors_origins == ["https://a.example", "https://b.example"]

    def test_an_empty_value_is_an_empty_list_not_a_crash(self):
        assert build(cors_origins="").cors_origins == []

    def test_a_list_passes_through(self):
        assert build(cors_origins=["https://a.example"]).cors_origins == [
            "https://a.example"
        ]


class TestLlmSettings:
    def test_ollama_needs_no_api_key(self):
        """The default path is a local process. Shipping a required secret for
        it would put a key in the repo for no reason."""
        settings = build(llm_provider="ollama", ai_api_key="")
        assert "AI_API_KEY" not in " ".join(settings.validate_runtime())

    def test_openai_without_a_key_is_a_production_problem(self):
        settings = build(llm_provider="openai", ai_api_key="").model_copy(
            update={"environment": "production", "debug": False, "auth_secret": "x" * 40}
        )
        problems = settings.validate_runtime()
        assert any("AI_API_KEY" in problem for problem in problems)

    def test_an_unknown_provider_is_reported(self):
        settings = build(llm_provider="not-a-provider")
        assert any("not-a-provider" in problem for problem in settings.validate_runtime())

    def test_an_empty_model_is_reported(self):
        settings = build(ollama_model="   ")
        assert any("OLLAMA_MODEL" in problem for problem in settings.validate_runtime())

    @pytest.mark.parametrize(
        "url", ["localhost:11434", "ftp://localhost:11434"]
    )
    def test_a_non_http_base_url_is_reported(self, url):
        settings = build(ollama_base_url=url)
        assert any("OLLAMA_BASE_URL" in problem for problem in settings.validate_runtime())
