"""Tests for the intelligence layer.

Most of these run with no model installed: the provider is a test double, so
the suite is deterministic and CI-safe. The tests that talk to a real Ollama are
marked `slow` and skip when it is unreachable — see
`test_provider_ollama_live.py`.

The behaviour worth testing hardest is the validation layer, because it is the
one that stands between a 1.5B model's arithmetic and a real print.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app.core.config import get_settings
from app.core.errors import InsufficientInformationError
from app.intelligence.intent_mapper import apply_intent
from app.intelligence.planner import PlannerContext, plan_from_request
from app.intelligence.providers.base import (
    LLM_INVALID_OUTPUT,
    LLM_MODEL_MISSING,
    LLM_UNAVAILABLE,
    LLMError,
    LLMRequest,
    LLMResponse,
    ProviderHealth,
)
from app.intelligence.providers.registry import (
    SUPPORTED_PROVIDERS,
    build_provider,
    get_llm_provider,
    reset_provider_cache,
)
from app.intelligence.rag.in_memory import InMemoryKnowledgeSource
from app.intelligence.rag.knowledge import KNOWLEDGE_DOCUMENTS
from app.intelligence.rag.retriever import reset_knowledge_source, retrieve
from app.intelligence.schemas import (
    PLAN_JSON_SCHEMA,
    IntentPriorities,
    LLMPlan,
    coerce_plan,
)
from app.intelligence.tools import (
    ALLOWED_TOOLS,
    ToolNotAllowedError,
    ToolRegistry,
    render_tool_context,
    reset_registry,
)

# -- doubles -------------------------------------------------------------


class FakeProvider:
    """Returns canned payloads and records what it was asked."""

    name = "fake"

    def __init__(self, payloads: list[Any] | None = None) -> None:
        self.payloads = payloads or []
        self.requests: list[LLMRequest] = []
        self.failures: list[LLMError] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self.failures:
            raise self.failures.pop(0)
        payload = self.payloads.pop(0) if self.payloads else {}
        return LLMResponse(
            raw_text=json.dumps(payload),
            parsed=payload if isinstance(payload, dict) else None,
            model="fake-1b",
            provider=self.name,
            duration_ms=7,
            schema_enforced=request.json_schema is not None,
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, available=True, model="fake-1b", status="ready")

    def list_models(self) -> list[str]:
        return ["fake-1b"]


def good_plan(**overrides: Any) -> dict[str, Any]:
    payload = {
        "intent": {
            "strength": 0.9,
            "speed": 0.1,
            "material_efficiency": 0.6,
            "surface_quality": 0.3,
        },
        "constraints": {
            "max_print_time_minutes": None,
            "max_material_grams": None,
            "allow_scaling": True,
        },
        "support_preference": "allowed",
        "orientation_preference": "optimized",
        "rationale": "Strong and light; print time is not a concern.",
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_provider_cache()
    reset_knowledge_source()
    reset_registry()
    yield
    reset_provider_cache()
    reset_knowledge_source()
    reset_registry()


# -- schema validation: the measured failure modes -----------------------


class TestCoercePlan:
    """These mirror what a real qwen2.5:1.5b actually returned."""

    def test_accepts_a_well_formed_plan(self):
        result = coerce_plan(good_plan())
        assert result.valid
        assert result.plan.intent.strength == pytest.approx(0.9)
        assert result.normalisations == []

    def test_priority_above_one_is_rescaled_and_recorded(self):
        # Measured: given a schema with minimum 0 / maximum 1, the model
        # returned 5 for "strength". The value must not reach a print.
        result = coerce_plan(
            good_plan(
                intent={
                    "strength": 5,
                    "speed": 1,
                    "material_efficiency": 5,
                    "surface_quality": 1,
                }
            )
        )
        assert result.valid
        assert 0.0 <= result.plan.intent.strength <= 1.0
        assert result.plan.intent.strength == pytest.approx(1.0)
        fields = {n.field for n in result.normalisations}
        assert "intent.strength" in fields
        assert "intent.material_efficiency" in fields

    def test_priority_on_a_hundred_point_scale_is_rescaled(self):
        result = coerce_plan(
            good_plan(
                intent={
                    "strength": 90,
                    "speed": 10,
                    "material_efficiency": 50,
                    "surface_quality": 20,
                }
            )
        )
        assert result.valid
        assert result.plan.intent.strength == pytest.approx(0.9)
        assert result.normalisations[0].reason.startswith("outside")

    def test_negative_priority_is_clamped(self):
        result = coerce_plan(
            good_plan(
                intent={
                    "strength": -3,
                    "speed": 0.5,
                    "material_efficiency": 0.5,
                    "surface_quality": 0.5,
                }
            )
        )
        assert result.valid
        assert result.plan.intent.strength == 0.0

    def test_non_numeric_priority_defaults_to_neutral(self):
        result = coerce_plan(
            good_plan(
                intent={
                    "strength": "very strong",
                    "speed": 0.5,
                    "material_efficiency": 0.5,
                    "surface_quality": 0.5,
                }
            )
        )
        assert result.valid
        assert result.plan.intent.strength == 0.5
        assert any(
            n.field == "intent.strength" and n.reason == "not a number; defaulted to neutral"
            for n in result.normalisations
        )

    def test_nonsense_time_limit_is_dropped_not_clamped(self):
        # A negative or zero time limit must not silently become a budget.
        result = coerce_plan(
            good_plan(
                constraints={
                    "max_print_time_minutes": -20,
                    "max_material_grams": None,
                    "allow_scaling": True,
                }
            )
        )
        assert result.valid
        assert result.plan.constraints.max_print_time_minutes is None
        assert any(
            n.field == "constraints.max_print_time_minutes" and n.applied_value is None
            for n in result.normalisations
        )

    def test_nonsense_material_limit_is_dropped(self):
        result = coerce_plan(
            good_plan(
                constraints={
                    "max_print_time_minutes": 90,
                    "max_material_grams": -5,
                    "allow_scaling": True,
                }
            )
        )
        assert result.valid
        assert result.plan.constraints.max_material_grams is None

    def test_unknown_top_level_key_is_dropped_and_recorded(self):
        result = coerce_plan(good_plan(gcode="G28 ; home"))
        assert result.valid
        assert not hasattr(result.plan, "gcode")
        assert any(n.field == "gcode" and n.reason.startswith("not part of the plan")
                   for n in result.normalisations)

    def test_model_cannot_express_a_machine_command(self):
        """The schema has no field for one, so the attempt cannot land."""
        result = coerce_plan(good_plan(parameters={"layer_height": 2.0}))
        assert result.valid
        assert not hasattr(result.plan, "parameters")
        assert any(n.field == "parameters" for n in result.normalisations)

    def test_invalid_enum_is_defaulted(self):
        result = coerce_plan(good_plan(support_preference="maybe"))
        assert result.valid
        assert result.plan.support_preference == "allowed"
        assert any(n.field == "support_preference" for n in result.normalisations)

    def test_missing_intent_is_rejected(self):
        result = coerce_plan({"rationale": "no intent here"})
        assert not result.valid
        assert "intent" in (result.rejected_reason or "")

    def test_non_object_payload_is_rejected(self):
        result = coerce_plan([1, 2, 3])
        assert not result.valid

    def test_json_schema_and_pydantic_agree_on_required_keys(self):
        """Guard against the hand-written schema drifting from the models."""
        pydantic_required = set(LLMPlan.model_fields)
        schema_required = set(PLAN_JSON_SCHEMA["required"])
        assert pydantic_required == schema_required

    def test_json_schema_has_no_unbounded_numeric_field(self):
        """Every declared number carries an explicit bound, even though the
        provider does not enforce it."""
        def walk(node: Any) -> list[dict[str, Any]]:
            found: list[dict[str, Any]] = []
            if isinstance(node, dict):
                if node.get("type") in ("number", "integer"):
                    found.append(node)
                for value in node.values():
                    found.extend(walk(value))
            elif isinstance(node, list):
                for item in node:
                    found.extend(walk(item))
            return found

        numbers = walk(PLAN_JSON_SCHEMA)
        assert numbers, "schema declares no numeric fields"
        for node in numbers:
            has_lower = "minimum" in node or "exclusiveMinimum" in node
            assert has_lower and "maximum" in node, node


class TestDominantPriority:
    def test_single_clear_winner(self):
        assert IntentPriorities(strength=0.9, speed=0.2, material_efficiency=0.3,
                                surface_quality=0.2).dominant() == "strength"

    def test_tie_is_balanced(self):
        assert IntentPriorities(strength=0.8, speed=0.8, material_efficiency=0.2,
                                surface_quality=0.2).dominant() == "balanced"

    def test_all_weak_is_balanced(self):
        assert IntentPriorities(strength=0.3, speed=0.3, material_efficiency=0.3,
                                surface_quality=0.3).dominant() == "balanced"


# -- planner --------------------------------------------------------------


class TestPlanner:
    def test_plans_from_a_valid_payload(self):
        provider = FakeProvider([good_plan()])
        result = plan_from_request(
            "strong but lightweight", provider=provider, context=PlannerContext()
        )
        assert result.plan.valid
        assert result.attempts == 1
        assert result.plan.plan.intent.dominant() == "strength"
        assert result.model == "fake-1b"

    def test_rejects_unrelated_request_before_calling_the_model(self):
        provider = FakeProvider([good_plan()])
        with pytest.raises(LLMError) as excinfo:
            plan_from_request("what is your name?", provider=provider)
        assert excinfo.value.code == LLM_INVALID_OUTPUT
        assert provider.requests == []

    def test_sends_the_schema_to_the_provider(self):
        provider = FakeProvider([good_plan()])
        plan_from_request("Make this part strong", provider=provider)
        assert provider.requests[0].json_schema == PLAN_JSON_SCHEMA
        assert provider.requests[0].system

    def test_rejects_unparseable_model_output(self):
        provider = FakeProvider([{"intent": "not an object"}])
        with pytest.raises(LLMError) as excinfo:
            plan_from_request("Make this part strong", provider=provider)
        assert excinfo.value.code == LLM_INVALID_OUTPUT

    def test_empty_request_is_rejected_before_calling_the_model(self):
        provider = FakeProvider([good_plan()])
        with pytest.raises(LLMError):
            plan_from_request("   ", provider=provider)
        assert provider.requests == []

    def test_transport_failure_is_retried_then_surfaces(self):
        provider = FakeProvider([good_plan()])
        provider.failures = [LLMError(LLM_UNAVAILABLE, "down")]
        result = plan_from_request("Make this part strong", provider=provider)
        # One failure then success: the retry policy recovered.
        assert result.plan.valid
        assert result.attempts == 2

    def test_repeated_transport_failure_surfaces_the_code(self):
        provider = FakeProvider([good_plan()])
        provider.failures = [
            LLMError(LLM_UNAVAILABLE, "down"),
            LLMError(LLM_UNAVAILABLE, "still down"),
        ]
        with pytest.raises(LLMError) as excinfo:
            plan_from_request("Make this part strong", provider=provider)
        assert excinfo.value.code == LLM_UNAVAILABLE

    def test_invalid_output_is_not_retried(self):
        """Retrying a small model with the same prompt just costs 25s again."""
        provider = FakeProvider([{"nonsense": True}, good_plan()])
        with pytest.raises(LLMError):
            plan_from_request("Make this part strong", provider=provider)
        assert len(provider.requests) == 1

    def test_assembles_tool_context_deterministically(self):
        provider = FakeProvider([good_plan()])
        context = PlannerContext(
            machine_limits={"available": True, "bed_size_mm": {"x": 220, "y": 220, "z": 250}}
        )
        plan_from_request("Make this part strong", provider=provider, context=context)
        prompt = provider.requests[0].prompt
        assert "220" in prompt

    def test_retrieval_context_reaches_the_prompt(self):
        provider = FakeProvider([good_plan()])
        plan_from_request("I want it strong", provider=provider)
        assert "strength" in provider.requests[0].prompt.lower()

    def test_missing_context_does_not_break_planning(self):
        provider = FakeProvider([good_plan()])
        result = plan_from_request("Make this part strong", provider=provider, context=PlannerContext())
        assert result.plan.valid
        # Tools ran and reported themselves unavailable rather than raising.
        tool_names = {item["name"] for item in result.tool_context}
        assert "machine_limits" in tool_names


# -- providers ------------------------------------------------------------


class TestProviderRegistry:
    def test_default_is_ollama_and_needs_no_key(self):
        settings = get_settings()
        provider = build_provider(settings)
        assert provider.name in SUPPORTED_PROVIDERS

    def test_unknown_provider_is_refused(self):
        settings = get_settings().model_copy(update={"llm_provider": "not-a-provider"})
        with pytest.raises(InsufficientInformationError):
            build_provider(settings)

    def test_cache_is_reused_then_invalidated(self):
        settings = get_settings()
        first = get_llm_provider(settings)
        assert get_llm_provider(settings) is first
        reset_provider_cache()
        assert get_llm_provider(settings) is not first

    def test_provider_satisfies_the_protocol(self):
        from app.intelligence.providers.base import LLMProvider

        assert isinstance(build_provider(get_settings()), LLMProvider)


class TestOllamaProviderParsing:
    def test_generate_keeps_the_local_model_warm(self, monkeypatch):
        from app.intelligence.providers.ollama import OllamaProvider

        settings = get_settings().model_copy(update={"ollama_keep_alive": "15m"})
        provider = OllamaProvider(settings)
        captured: dict[str, Any] = {}

        def respond(_path, payload, *, timeout):
            captured.update(payload)
            return {"response": "{}"}

        monkeypatch.setattr(provider, "_post_json", respond)
        provider.generate(LLMRequest(prompt="return an empty JSON object"))
        assert captured["keep_alive"] == "15m"

    def test_unfenced_json(self):
        from app.intelligence.providers.ollama import _parse_json

        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_fenced_json_is_accepted(self):
        from app.intelligence.providers.ollama import _parse_json

        assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_truncated_json_is_rejected(self):
        from app.intelligence.providers.ollama import _parse_json

        assert _parse_json('{"intent": {"str') is None

    def test_prose_around_json_is_rejected(self):
        """Fences are a formatting artefact; prose is content and is refused."""
        from app.intelligence.providers.ollama import _parse_json

        assert _parse_json('Sure! Here you go: {"a": 1}') is None

    def test_json_array_is_rejected(self):
        from app.intelligence.providers.ollama import _parse_json

        assert _parse_json("[1, 2, 3]") is None

    def test_empty_is_rejected(self):
        from app.intelligence.providers.ollama import _parse_json

        assert _parse_json("") is None
        assert _parse_json("   \n") is None

    def test_unreachable_host_reports_unavailable_not_a_crash(self, monkeypatch):
        from app.intelligence.providers.ollama import OllamaProvider

        settings = get_settings().model_copy(
            update={"ollama_base_url": "http://127.0.0.1:1", "ollama_model": "qwen2.5:1.5b"}
        )
        provider = OllamaProvider(settings)
        health = provider.health()
        assert health.available is False
        assert health.code == LLM_UNAVAILABLE
        assert "ollama serve" in health.message

    def test_missing_model_is_reported_distinctly(self, monkeypatch):
        from app.intelligence.providers import ollama as ollama_module

        settings = get_settings().model_copy(update={"ollama_model": "not-pulled:99b"})
        provider = ollama_module.OllamaProvider(settings)
        monkeypatch.setattr(
            provider, "_get_json", lambda path, timeout: {"models": [{"name": "qwen2.5:1.5b"}]}
        )
        health = provider.health()
        assert health.available is False
        assert health.code == LLM_MODEL_MISSING
        assert "ollama pull" in health.message

    def test_tagless_model_name_matches_the_configured_model(self, monkeypatch):
        from app.intelligence.providers import ollama as ollama_module

        settings = get_settings().model_copy(update={"ollama_model": "qwen2.5"})
        provider = ollama_module.OllamaProvider(settings)
        monkeypatch.setattr(
            provider, "_get_json", lambda path, timeout: {"models": [{"name": "qwen2.5:1.5b"}]}
        )
        assert provider.health().available is True


# -- tools ----------------------------------------------------------------


class TestTools:
    def test_only_allowlisted_names_exist(self):
        assert ALLOWED_TOOLS == frozenset(
            {
                "model_summary",
                "machine_limits",
                "material_profile",
                "slicer_capabilities",
            }
        )

    def test_unknown_tool_is_refused(self):
        with pytest.raises(ToolNotAllowedError):
            ToolRegistry().invoke("run_shell")

    def test_gcode_tool_is_refused(self):
        registry = ToolRegistry()
        for name in ("write_file", "exec", "slice", "generate_gcode", "delete_model"):
            with pytest.raises(ToolNotAllowedError):
                registry.invoke(name)

    def test_no_tool_declares_side_effects(self):
        for spec in ToolRegistry().describe():
            assert spec["side_effects"] == "none"

    def test_missing_context_reports_unavailable_not_an_error(self):
        result = ToolRegistry({}).invoke("machine_limits")
        assert result.data == {"available": False}
        assert "not available" in result.summary

    def test_context_is_returned(self):
        registry = ToolRegistry(
            {"machine_limits": {"available": True, "bed_size_mm": {"x": 220}}}
        )
        result = registry.invoke("machine_limits")
        assert result.data["bed_size_mm"]["x"] == 220
        assert "220" in result.summary

    def test_unexpected_argument_is_refused(self):
        with pytest.raises(Exception):
            ToolRegistry({}).invoke("machine_limits", {"path": "/etc/passwd"})

    def test_collect_skips_disallowed_names(self):
        results = ToolRegistry({}).collect(("machine_limits", "run_shell"))
        assert [item.name for item in results] == ["machine_limits"]

    def test_tool_context_is_json_serialisable(self):
        registry = ToolRegistry(
            {"model_summary": {"available": True, "dimensions": {"x": 10.0, "y": 20.0}}}
        )
        rendered = render_tool_context(registry.collect(("model_summary",)))
        assert "10.0 mm" in rendered
        json.dumps(rendered)


# -- retrieval ------------------------------------------------------------


class TestRetrieval:
    def test_finds_the_strength_document(self):
        docs = retrieve("I want this to be really strong")
        ids = [item.document.id for item in docs]
        assert "priorities-strength" in ids

    def test_finds_the_supports_document(self):
        docs = retrieve("do I need supports for this overhang")
        ids = [item.document.id for item in docs]
        assert "supports-overhangs" in ids

    def test_scores_are_descending(self):
        docs = retrieve("strong material efficient", limit=5)
        scores = [item.score for item in docs]
        assert scores == sorted(scores, reverse=True)

    def test_respects_the_limit(self):
        settings = get_settings().model_copy(update={"rag_max_documents": 2})
        assert len(retrieve("strong speed material", settings=settings)) <= 2

    def test_empty_query_retrieves_nothing(self):
        assert retrieve("") == []

    def test_stopwords_alone_retrieve_nothing(self):
        assert retrieve("the a an and of to") == []

    def test_rag_can_be_disabled(self):
        settings = get_settings().model_copy(update={"rag_enabled": False})
        assert retrieve("strong", settings=settings) == []

    def test_tag_filter(self):
        source = InMemoryKnowledgeSource()
        docs = source.search("temperature", limit=5, tags=("materials",))
        assert docs
        assert all("materials" in item.document.tags for item in docs)

    def test_corpus_ids_are_unique(self):
        ids = [doc.id for doc in KNOWLEDGE_DOCUMENTS]
        assert len(ids) == len(set(ids))

    def test_every_document_has_content(self):
        for document in KNOWLEDGE_DOCUMENTS:
            assert document.title and document.content
            assert document.tags


# -- intent -> parameters -------------------------------------------------


class TestIntentMapper:
    def _plan(self, **priorities: float) -> LLMPlan:
        base = {"strength": 0.5, "speed": 0.5, "material_efficiency": 0.5, "surface_quality": 0.5}
        base.update(priorities)
        return LLMPlan(intent=IntentPriorities(**base))

    def test_quality_priority_thins_the_layers(self):
        quality, _changes, _w = apply_intent(
            self._plan(surface_quality=1.0, speed=0.0), {"layer_height": 0.2}
        )
        speed_first, _c, _w = apply_intent(
            self._plan(surface_quality=0.0, speed=1.0), {"layer_height": 0.2}
        )
        assert quality["layer_height"] < speed_first["layer_height"]

    def test_layer_height_stays_in_a_printable_band(self):
        for quality in (0.0, 0.25, 0.5, 0.75, 1.0):
            params, _c, _w = apply_intent(
                self._plan(surface_quality=quality, speed=1.0 - quality), {}
            )
            assert 0.08 <= params["layer_height"] <= 0.32

    def test_strength_adds_walls(self):
        weak, _c, _w = apply_intent(self._plan(strength=0.0), {})
        strong, _c, _w = apply_intent(self._plan(strength=1.0), {})
        assert strong["wall_line_count"] > weak["wall_line_count"]

    def test_material_saving_never_drops_below_one_wall(self):
        params, _c, _w = apply_intent(
            self._plan(material_efficiency=1.0, strength=0.0), {}
        )
        assert params["wall_line_count"] >= 2

    def test_material_saving_uses_a_sparse_pattern(self):
        params, _c, _w = apply_intent(
            self._plan(material_efficiency=0.9, strength=0.2), {}
        )
        assert params["infill_pattern"] == "gyroid"

    def test_strength_prevents_the_sparse_pattern(self):
        params, _c, _w = apply_intent(
            self._plan(material_efficiency=0.9, strength=0.95), {}
        )
        assert params.get("infill_pattern") != "gyroid"

    def test_existing_walls_are_never_reduced(self):
        params, _c, _w = apply_intent(self._plan(strength=0.0), {"wall_line_count": 6})
        assert params["wall_line_count"] == 6

    def test_machine_max_layer_height_is_honoured(self):
        # Quality 0 / speed 1 asks for the thickest layer (0.32); a machine that
        # caps at 0.2 must win.
        params, _c, warnings = apply_intent(
            self._plan(surface_quality=0.0, speed=1.0),
            {},
            limits={"min_layer_height_mm": 0.1, "max_layer_height_mm": 0.2},
        )
        assert params["layer_height"] == 0.2
        assert any("caps layer height" in w for w in warnings)

    def test_machine_min_layer_height_is_honoured(self):
        # Quality 1 / speed 0 asks for the thinnest layer (0.082); a machine that
        # requires 0.15 must win.
        params, _c, warnings = apply_intent(
            self._plan(surface_quality=1.0, speed=0.0),
            {},
            limits={"min_layer_height_mm": 0.15, "max_layer_height_mm": 0.32},
        )
        assert params["layer_height"] == 0.15
        assert any("requires at least" in w for w in warnings)

    def test_does_not_mutate_the_input(self):
        original = {"layer_height": 0.2}
        snapshot = dict(original)
        apply_intent(self._plan(strength=1.0), original)
        assert original == snapshot

    def test_is_deterministic(self):
        plan = self._plan(strength=0.8, surface_quality=0.3)
        first, _c, _w = apply_intent(plan, {"layer_height": 0.2})
        second, _c, _w = apply_intent(plan, {"layer_height": 0.2})
        assert first == second

    def test_time_budget_is_surfaced_not_silently_applied(self):
        plan = LLMPlan(
            intent=IntentPriorities(),
            constraints={"max_print_time_minutes": 120},
        )
        _params, _c, warnings = apply_intent(plan, {})
        assert any("120 min" in w for w in warnings)

    def test_no_scaling_is_surfaced(self):
        plan = LLMPlan(intent=IntentPriorities(), constraints={"allow_scaling": False})
        _params, _c, warnings = apply_intent(plan, {})
        assert any("Scaling is disabled" in w for w in warnings)

    def test_every_change_carries_a_reason(self):
        _params, changes, _w = apply_intent(self._plan(strength=0.9, surface_quality=0.2), {})
        assert changes
        for change in changes:
            assert change.reason
            assert change.to_dict()["field"]