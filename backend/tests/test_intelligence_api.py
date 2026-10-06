"""HTTP tests for the AI endpoints.

The model is replaced with a fake in every test here, so the suite never depends
on a running Ollama. The endpoints are tested for what they must guarantee:
authentication, the absence of side effects, honest failure, and the fact that
no model output can smuggle a machine command through.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app.intelligence.api import schemas as ai_schemas
from app.intelligence.providers.base import LLM_UNAVAILABLE, LLMError, LLMRequest, LLMResponse, ProviderHealth
from app.intelligence.providers.registry import reset_provider_cache


class ScriptedProvider:
    """A provider whose answers the test dictates."""

    name = "fake"

    def __init__(self) -> None:
        self.payload: dict[str, Any] = {}
        self.error: LLMError | None = None
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return LLMResponse(
            raw_text=json.dumps(self.payload),
            parsed=self.payload,
            model="fake-1b",
            provider=self.name,
            duration_ms=11,
            schema_enforced=True,
        )

    def health(self) -> ProviderHealth:
        if self.error is not None:
            return ProviderHealth(
                provider=self.name, available=False, model="fake-1b",
                status="unavailable", code=self.error.code, message=self.error.message,
            )
        return ProviderHealth(provider=self.name, available=True, model="fake-1b", status="ready", latency_ms=3)

    def list_models(self) -> list[str]:
        return ["fake-1b"]


DEFAULT_PLAN = {
    "intent": {
        "strength": 0.9,
        "speed": 0.1,
        "material_efficiency": 0.5,
        "surface_quality": 0.3,
    },
    "constraints": {
        "max_print_time_minutes": None,
        "max_material_grams": None,
        "allow_scaling": True,
    },
    "support_preference": "allowed",
    "orientation_preference": "optimized",
    "rationale": "Strong, and print time does not matter.",
    "confidence": 0.8,
}


@pytest.fixture
def scripted(monkeypatch):
    provider = ScriptedProvider()
    provider.payload = dict(DEFAULT_PLAN)
    monkeypatch.setattr(
        "app.intelligence.api.router.get_llm_provider", lambda settings=None: provider
    )
    return provider


class TestAiHealth:
    def test_requires_authentication(self, client):
        assert client.get("/api/v1/ai/health").status_code in (401, 403)

    def test_reports_ready_when_the_model_is_reachable(self, client, auth_headers, scripted):
        response = client.get("/api/v1/ai/health", headers=auth_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["available"] is True
        assert body["status"] == "ready"

    def test_reports_unavailable_with_a_hint(self, client, auth_headers, scripted):
        scripted.error = LLMError(LLM_UNAVAILABLE, "Cannot reach Ollama.")
        response = client.get("/api/v1/ai/health", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["available"] is False
        assert body["code"] == LLM_UNAVAILABLE
        assert "ollama serve" in body["hint"]

    def test_is_a_real_probe_not_a_config_echo(self, client, auth_headers, scripted):
        """The endpoint must reflect a probe, not echo the settings."""
        body = client.get("/api/v1/ai/health", headers=auth_headers).json()
        assert body["model"] == "fake-1b"
        assert "latency_ms" in body


class TestAiPlan:
    def test_requires_authentication(self, client):
        assert client.post("/api/v1/ai/plan", json={"request": "strong"}).status_code in (401, 403)

    def test_returns_a_validated_plan(self, client, auth_headers, scripted):
        response = client.post(
            "/api/v1/ai/plan",
            json={"request": "Make this strong but lightweight, time does not matter."},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["validation"]["valid"] is True
        assert body["validation"]["plan"]["intent"]["strength"] == pytest.approx(0.9)
        assert body["provider"] == "fake"
        assert body["duration_ms"] >= 0

    def test_reports_out_of_range_values_it_corrected(self, client, auth_headers, scripted):
        """The 1.5B model's actual failure mode, exercised through the API."""
        scripted.payload = json.loads(json.dumps(DEFAULT_PLAN))
        scripted.payload["intent"]["strength"] = 5
        response = client.post(
            "/api/v1/ai/plan", json={"request": "strong"}, headers=auth_headers
        )
        assert response.status_code == 200
        validation = response.json()["validation"]
        assert validation["valid"] is True
        assert validation["plan"]["intent"]["strength"] <= 1.0
        assert any(n["field"] == "intent.strength" for n in validation["normalisations"])

    def test_rejects_an_unusable_plan(self, client, auth_headers, scripted):
        scripted.payload = {"rationale": "no intent"}
        response = client.post(
            "/api/v1/ai/plan", json={"request": "strong"}, headers=auth_headers
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "LLM_INVALID_OUTPUT"

    def test_unavailable_provider_is_a_503_not_a_fallback(self, client, auth_headers, scripted):
        scripted.error = LLMError(LLM_UNAVAILABLE, "down")
        response = client.post(
            "/api/v1/ai/plan", json={"request": "strong"}, headers=auth_headers
        )
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "LLM_UNAVAILABLE"
        assert error["details"]["hint"]

    def test_empty_request_is_rejected_by_the_schema(self, client, auth_headers):
        assert client.post("/api/v1/ai/plan", json={"request": ""}, headers=auth_headers).status_code == 422

    def test_unknown_field_is_rejected(self, client, auth_headers):
        response = client.post(
            "/api/v1/ai/plan",
            json={"request": "strong", "gcode": "G28"},
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_creates_no_slice_job(self, client, auth_headers, scripted):
        """Planning must not have side effects on the slice pipeline."""
        before = client.get("/api/v1/slice", headers=auth_headers).json()
        client.post("/api/v1/ai/plan", json={"request": "strong"}, headers=auth_headers)
        after = client.get("/api/v1/slice", headers=auth_headers).json()
        assert len(before) == len(after)


class TestAiApply:
    def test_requires_authentication(self, client):
        assert client.post("/api/v1/ai/apply", json={"plan": DEFAULT_PLAN}).status_code in (401, 403)

    def test_turns_a_plan_into_parameters(self, client, auth_headers):
        response = client.post(
            "/api/v1/ai/apply",
            json={"plan": DEFAULT_PLAN, "parameters": {"layer_height": 0.2}},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "layer_height" in body["parameters"]
        assert body["dominant_priority"] == "strength"
        assert body["changes"]

    def test_is_deterministic(self, client, auth_headers):
        """The same plan yields the same parameters, every time.

        The ledger id is excluded on purpose: two calls are two separate user
        actions and each one earns its own row. Determinism is a claim about the
        arithmetic, not about the audit trail.
        """
        payload = {"plan": DEFAULT_PLAN, "parameters": {"layer_height": 0.2}}
        first = client.post("/api/v1/ai/apply", json=payload, headers=auth_headers).json()
        second = client.post("/api/v1/ai/apply", json=payload, headers=auth_headers).json()

        for body in (first, second):
            assert body["decision"]["id"]
        assert first["decision_id"] != second["decision_id"]

        for key in ("parameters", "changes", "warnings", "dominant_priority"):
            assert first[key] == second[key]

    def test_needs_no_model(self, client, auth_headers, scripted):
        """apply is pure arithmetic: it must not call the provider at all."""
        response = client.post(
            "/api/v1/ai/apply", json={"plan": DEFAULT_PLAN}, headers=auth_headers
        )
        assert response.status_code == 200
        assert scripted.calls == 0

    def test_rejects_an_invalid_plan(self, client, auth_headers):
        response = client.post(
            "/api/v1/ai/apply", json={"plan": {"rationale": "nope"}}, headers=auth_headers
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "PLAN_INVALID"

    def test_cannot_produce_gcode(self, client, auth_headers):
        """Even a plan carrying G-code yields only parameters."""
        hostile = dict(DEFAULT_PLAN)
        hostile["gcode"] = "G1 X999 Y999 E999"
        body = client.post(
            "/api/v1/ai/apply", json={"plan": hostile}, headers=auth_headers
        ).json()
        assert "G1" not in json.dumps(body["parameters"])


class TestAiRefine:
    def test_requires_authentication(self, client):
        response = client.post(
            "/api/v1/ai/refine",
            json={"previous_plan": DEFAULT_PLAN, "instruction": "make it faster"},
        )
        assert response.status_code in (401, 403)

    def test_returns_a_validated_plan(self, client, auth_headers, scripted):
        response = client.post(
            "/api/v1/ai/refine",
            json={"previous_plan": DEFAULT_PLAN, "instruction": "actually make it faster"},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["validation"]["valid"] is True


class TestDecisionLedgerOverHttp:
    """Boundary 3 of ARCHITECTURE.md, exercised the way a user reaches it."""

    def test_planning_records_a_decision(self, client, auth_headers, scripted):
        body = client.post(
            "/api/v1/ai/plan", json={"request": "strong"}, headers=auth_headers
        ).json()
        assert body["decision_id"]
        assert body["decision"]["decision_type"] == "print_intent_plan"
        assert body["decision"]["reason"]

    def test_the_recorded_decision_can_be_read_back(self, client, auth_headers, scripted):
        planned = client.post(
            "/api/v1/ai/plan", json={"request": "strong but light"}, headers=auth_headers
        ).json()

        listed = client.get("/api/v1/ai/decisions", headers=auth_headers).json()["decisions"]
        row = next(item for item in listed if item["id"] == planned["decision_id"])
        assert row["source"] == "llm"
        assert row["calibration"] == "uncalibrated"
        assert row["model_identifier"] == "fake:fake-1b"
        assert len(row["prompt_hash"]) == 64
        assert row["evidence"], "a decision with no evidence is not explainable"
        assert all(item["excerpt"] for item in row["evidence"])

    def test_the_evidence_names_the_retrieved_documents(self, client, auth_headers, scripted):
        """The row must cite the platform rules the plan actually leaned on."""
        client.post(
            "/api/v1/ai/plan",
            json={"request": "make this as strong as you reasonably can"},
            headers=auth_headers,
        )
        listed = client.get("/api/v1/ai/decisions", headers=auth_headers).json()["decisions"]
        kinds = {item["kind"] for item in listed[0]["evidence"]}
        assert "knowledge" in kinds
        assert "user_request" in kinds

    def test_applying_records_a_second_row(self, client, auth_headers, scripted):
        planned = client.post(
            "/api/v1/ai/plan", json={"request": "strong"}, headers=auth_headers
        ).json()
        applied = client.post(
            "/api/v1/ai/apply",
            json={"plan": planned["validation"]["plan"], "plan_decision_id": planned["decision_id"]},
            headers=auth_headers,
        ).json()

        assert applied["decision_id"] != planned["decision_id"]
        listed = client.get("/api/v1/ai/decisions", headers=auth_headers).json()["decisions"]
        row = next(item for item in listed if item["id"] == applied["decision_id"])
        # The model proposed, the arithmetic disposed, and the ledger says which
        # of the two this row is.
        assert row["source"] == "deterministic"
        assert row["calibration"] == "heuristic"
        assert row["applied_value"]["parameters"]
        assert planned["decision_id"] in {
            item["ref_id"] for item in row["evidence"]
        }

    def test_a_failed_plan_records_nothing(self, client, auth_headers, scripted):
        """A proposal that never happened must not leave a row suggesting it did."""
        before = len(client.get("/api/v1/ai/decisions", headers=auth_headers).json()["decisions"])
        scripted.error = LLMError(LLM_UNAVAILABLE, "Cannot reach Ollama.")
        assert (
            client.post(
                "/api/v1/ai/plan", json={"request": "strong"}, headers=auth_headers
            ).status_code
            == 503
        )
        after = len(client.get("/api/v1/ai/decisions", headers=auth_headers).json()["decisions"])
        assert after == before

    def test_another_tenant_sees_nothing(self, client, auth_headers, fresh_headers, scripted):
        client.post("/api/v1/ai/plan", json={"request": "strong"}, headers=auth_headers)
        assert (
            client.get("/api/v1/ai/decisions", headers=fresh_headers).json()["decisions"] == []
        )

    def test_overriding_records_the_reason(self, client, auth_headers, scripted):
        planned = client.post(
            "/api/v1/ai/plan", json={"request": "strong"}, headers=auth_headers
        ).json()
        response = client.post(
            f"/api/v1/ai/decisions/{planned['decision_id']}/override",
            json={"reason": "I only wanted a draft, not a strong part."},
            headers=auth_headers,
        )
        assert response.status_code == 200

        listed = client.get("/api/v1/ai/decisions", headers=auth_headers).json()["decisions"]
        row = next(item for item in listed if item["id"] == planned["decision_id"])
        assert row["override_reason"].startswith("I only wanted")
        assert row["overridden_at"]

    def test_an_override_needs_a_reason(self, client, auth_headers, scripted):
        planned = client.post(
            "/api/v1/ai/plan", json={"request": "strong"}, headers=auth_headers
        ).json()
        response = client.post(
            f"/api/v1/ai/decisions/{planned['decision_id']}/override",
            json={"reason": "   "},
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_another_tenants_decision_cannot_be_overridden(
        self, client, auth_headers, fresh_headers, scripted
    ):
        """404, not 403: the ledger must not confirm the id exists elsewhere."""
        planned = client.post(
            "/api/v1/ai/plan", json={"request": "strong"}, headers=auth_headers
        ).json()
        response = client.post(
            f"/api/v1/ai/decisions/{planned['decision_id']}/override",
            json={"reason": "not mine"},
            headers=fresh_headers,
        )
        assert response.status_code == 404

    def test_the_ledger_requires_authentication(self, client):
        assert client.get("/api/v1/ai/decisions").status_code in (401, 403)


class TestAiEndpointSurface:
    def test_endpoints_are_documented(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        for path in (
            "/api/v1/ai/health",
            "/api/v1/ai/plan",
            "/api/v1/ai/apply",
            "/api/v1/ai/refine",
            "/api/v1/ai/decisions",
            "/api/v1/ai/decisions/{decision_id}/override",
        ):
            assert path in paths, path

    def test_request_models_forbid_extra_fields(self):
        for model in (
            ai_schemas.AiPlanRequest,
            ai_schemas.AiRefineRequest,
            ai_schemas.AiApplyRequest,
            ai_schemas.AiOverrideRequest,
        ):
            with pytest.raises(Exception):
                model.model_validate({"request": "x", "instruction": "y", "plan": {}, "gcode": "G28"})


@pytest.fixture(autouse=True)
def _reset():
    reset_provider_cache()
    yield
    reset_provider_cache()


def _unused_uuid() -> str:
    return str(uuid.uuid4())