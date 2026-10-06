"""Tests that talk to a real Ollama.

Skipped unless a model server answers on `OLLAMA_BASE_URL`, so the default
suite stays fast and CI-safe. Run them explicitly with:

    pytest tests/test_provider_ollama_live.py -m slow

These are the tests that keep the prose in the provider honest: they assert the
behaviour that was measured on this machine, so a change that reintroduces
`format="json"` or drops the schema fails here rather than in production.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.intelligence.intent_mapper import apply_intent
from app.intelligence.planner import PlannerContext, plan_from_request
from app.intelligence.providers.base import LLMError
from app.intelligence.providers.ollama import OllamaProvider
from app.intelligence.providers.registry import get_llm_provider
from app.intelligence.schemas import PLAN_JSON_SCHEMA

pytestmark = pytest.mark.slow


def _live_health() -> bool:
    try:
        return get_llm_provider(get_settings()).health().available
    except LLMError:
        return False


LIVE = pytest.mark.skipif(
    not _live_health(), reason="no Ollama model server reachable on OLLAMA_BASE_URL"
)


@LIVE
class TestLiveProvider:
    def test_health_is_a_real_probe(self):
        health = get_llm_provider(get_settings()).health()
        assert health.available is True
        assert health.status == "ready"
        assert health.latency_ms is not None and health.latency_ms >= 0
        assert health.model in health.models_available

    def test_schema_constrained_generation_returns_valid_json(self):
        """The reason `format` receives the schema rather than "json".

        Measured: with `format="json"` the model returns ```json fences and
        truncated objects. With the schema it returns parseable JSON.
        """
        provider = get_llm_provider(get_settings())
        from app.intelligence.providers.base import LLMRequest

        response = provider.generate(
            LLMRequest(
                prompt=(
                    "A user says: 'I need this bracket to be strong but light, "
                    "print time does not matter.' Score the four priorities 0 to 1. "
                    "Choose support_preference from avoid, allowed, required. "
                    "Choose orientation_preference from user, optimized, quality, "
                    "speed. Give a one-sentence rationale and a confidence."
                ),
                json_schema=PLAN_JSON_SCHEMA,
                system="You produce a structured print-intent plan.",
            )
        )
        assert response.parsed is not None
        assert set(response.parsed) <= set(PLAN_JSON_SCHEMA["properties"])
        assert response.schema_enforced is True

    def test_a_real_plan_validates_and_maps_to_parameters(self):
        provider = get_llm_provider(get_settings())
        result = plan_from_request(
            "Make this strong but lightweight. Print time does not matter.",
            provider=provider,
            context=PlannerContext(),
        )
        assert result.plan.valid
        plan = result.plan.plan

        # Whatever the model scored, it lands in range: this is the guarantee
        # that matters, not the specific numbers.
        for name, value in plan.intent.to_dict().items():
            assert 0.0 <= value <= 1.0, (name, value)

        parameters, changes, _warnings = apply_intent(plan, {"layer_height": 0.2})
        assert 0.08 <= parameters["layer_height"] <= 0.32
        assert parameters["wall_line_count"] >= 2

    def test_off_range_scores_are_recorded_not_trusted(self):
        """If the model miscalibrates, the record must show it."""
        provider = get_llm_provider(get_settings())
        result = plan_from_request(
            "Make it as strong as physically possible, I do not care about anything else.",
            provider=provider,
        )
        scores = list(result.plan.plan.intent.to_dict().values())
        assert all(0.0 <= value <= 1.0 for value in scores)
        for normalisation in result.plan.normalisations:
            assert normalisation.reason


@LIVE
class TestLiveOllamaProvider:
    def test_list_models_includes_the_configured_model(self):
        provider = OllamaProvider(get_settings())
        assert provider._model in provider.list_models()

    def test_unreachable_url_is_reported_not_raised(self):
        settings = get_settings().model_copy(
            update={"ollama_base_url": "http://127.0.0.1:1"}
        )
        health = OllamaProvider(settings).health()
        assert health.available is False
        assert health.code == "LLM_UNAVAILABLE"


class TestProviderWithoutAServer:
    """These need no server: they assert the failure contract."""

    def test_unreachable_url_yields_unavailable(self):
        settings = get_settings().model_copy(
            update={"ollama_base_url": "http://127.0.0.1:1"}
        )
        health = OllamaProvider(settings).health()
        assert health.available is False
        assert health.code == "LLM_UNAVAILABLE"

    def test_generate_on_unreachable_url_raises_llm_error(self):
        from app.intelligence.providers.base import LLMRequest

        settings = get_settings().model_copy(
            update={"ollama_base_url": "http://127.0.0.1:1", "llm_timeout_seconds": 2}
        )
        with pytest.raises(LLMError) as excinfo:
            OllamaProvider(settings).generate(LLMRequest(prompt="hi"))
        assert excinfo.value.code in ("LLM_UNAVAILABLE", "LLM_TIMEOUT")