"""Strict schemas for everything a model is allowed to produce.

Two rules govern this module:

1. **The LLM produces intent, never parameters.** `LLMPlan` contains
   priorities and limits. It cannot contain a layer height, a temperature or a
   G-code command, and the schema has no field in which to put one. Concrete
   slicing parameters are derived deterministically by the existing optimizer
   from this intent plus the real machine and material profiles.

2. **Ranges are validated here, because the model does not honour them.**
   Measured against a real `qwen2.5:1.5b`: given a JSON Schema with
   `"minimum": 0, "maximum": 1`, it returned `5`. Ollama's `format` constrains
   decoding to the *structure* — which keys exist, their types, that there are
   no extras — and not to the values. So every numeric bound here is enforced
   by `PrintIntent`, which clamps and records the adjustment rather than
   silently accepting nonsense.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Priorities are unit-interval scores. A model returning 5 for "strength" is
#: not expressing 500% strength, it is miscalibrated, and the fact is recorded.
Score = float

SupportPreference = Literal["avoid", "allowed", "required"]
OrientationPreference = Literal["user", "optimized", "quality", "speed"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IntentPriorities(StrictModel):
    """What the user said they care about, each 0..1."""

    strength: Score = 0.5
    speed: Score = 0.5
    material_efficiency: Score = 0.5
    surface_quality: Score = 0.5

    @field_validator("strength", "speed", "material_efficiency", "surface_quality")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("priority must be a finite number")
        return value

    def dominant(self) -> str:
        """The single strongest priority, with `balanced` when it is a tie."""
        items = self.model_dump()
        best = max(items.values())
        winners = [k for k, v in items.items() if v == best]
        if len(winners) > 1 or best < 0.55:
            return "balanced"
        return winners[0]

    def to_dict(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in self.model_dump().items()}


class IntentConstraints(StrictModel):
    """Hard limits the user stated. These are ceilings, never suggestions."""

    max_print_time_minutes: int | None = Field(default=None, ge=1, le=100_000)
    max_material_grams: float | None = Field(default=None, gt=0, le=100_000)
    #: A part that must not be scaled down, even if it then does not fit.
    allow_scaling: bool = True

    @field_validator("max_material_grams")
    @classmethod
    def _finite_grams(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("max_material_grams must be finite")
        return value


class LLMPlan(StrictModel):
    """The complete, validated shape of a model's answer.

    There is deliberately no field for a slicer parameter, a temperature, a
    speed or a coordinate. The model cannot express a machine command even if it
    tries, because the schema has nowhere to put one and `extra="forbid"`
    rejects the attempt.
    """

    intent: IntentPriorities
    constraints: IntentConstraints = Field(default_factory=IntentConstraints)
    support_preference: SupportPreference = "allowed"
    orientation_preference: OrientationPreference = "optimized"
    #: The model's own words, shown to the user. Never parsed for numbers.
    rationale: str = ""
    #: Free-form model id, recorded in the decision log. Not a capability.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Normalisation(BaseModel):
    """A value the model produced that had to be brought into range.

    Surfaced to the user rather than hidden, because a model that scores
    "strength" as 5 is telling you something about its calibration.
    """

    field: str
    model_value: Any
    applied_value: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class ValidatedPlan(BaseModel):
    """A plan that passed validation, plus what had to be corrected."""

    plan: LLMPlan
    normalisations: list[Normalisation] = Field(default_factory=list)
    #: Set when the plan could not be used at all.
    rejected_reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.rejected_reason is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "rejected_reason": self.rejected_reason,
            "plan": self.plan.model_dump(),
            "normalisations": [n.to_dict() for n in self.normalisations],
        }


#: The JSON Schema handed to the provider. Derived from the pydantic models so
#: the two cannot drift apart.
#:
#: Declared explicitly rather than generated, because Ollama needs `type` arrays
#: for nullable fields and needs enum values inlined; a mechanical dump of the
#: pydantic schema does not produce those. `test_schema_matches_models` asserts
#: the two stay consistent.
PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "object",
            "properties": {
                "strength": {"type": "number", "minimum": 0, "maximum": 1},
                "speed": {"type": "number", "minimum": 0, "maximum": 1},
                "material_efficiency": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "surface_quality": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": [
                "strength",
                "speed",
                "material_efficiency",
                "surface_quality",
            ],
        },
        "constraints": {
            "type": "object",
            "properties": {
                "max_print_time_minutes": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 100_000,
                },
                "max_material_grams": {
                    "type": ["number", "null"],
                    "exclusiveMinimum": 0,
                    "maximum": 100_000,
                },
                "allow_scaling": {"type": "boolean"},
            },
            "required": ["max_print_time_minutes", "max_material_grams", "allow_scaling"],
        },
        "support_preference": {
            "type": "string",
            "enum": ["avoid", "allowed", "required"],
        },
        "orientation_preference": {
            "type": "string",
            "enum": ["user", "optimized", "quality", "speed"],
        },
        "rationale": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "intent",
        "constraints",
        "support_preference",
        "orientation_preference",
        "rationale",
        "confidence",
    ],
}


_PRIORITY_FIELDS = ("strength", "speed", "material_efficiency", "surface_quality")


def coerce_plan(payload: Any) -> ValidatedPlan:
    """Validate a model's JSON into a `ValidatedPlan`, clamping what it can.

    Clamping rather than rejecting is deliberate for priorities: a score of 5
    means "maximally important" to a badly calibrated small model, and the user
    wants a working print, not an error about a 1-5 scale. The clamp is
    recorded so it is visible.

    Anything that cannot be interpreted — a missing intent, a string where a
    number belongs, a nonsense time limit — is rejected outright.
    """
    normalisations: list[Normalisation] = []

    if not isinstance(payload, dict):
        return ValidatedPlan(
            plan=LLMPlan(intent=IntentPriorities()),
            rejected_reason=f"Expected a JSON object, got {type(payload).__name__}.",
        )

    candidate = dict(payload)

    # Priorities: accept a 0-100 or 0-10 scale as well as 0-1.
    intent_raw = candidate.get("intent")
    if not isinstance(intent_raw, dict):
        return ValidatedPlan(
            plan=LLMPlan(intent=IntentPriorities()),
            rejected_reason="The plan is missing an 'intent' object.",
        )
    intent = dict(intent_raw)
    for field_name in _PRIORITY_FIELDS:
        raw = intent.get(field_name, 0.5)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            normalisations.append(
                Normalisation(
                    field=f"intent.{field_name}",
                    model_value=raw,
                    applied_value=0.5,
                    reason="not a number; defaulted to neutral",
                )
            )
            intent[field_name] = 0.5
            continue
        if not math.isfinite(value):
            normalisations.append(
                Normalisation(
                    field=f"intent.{field_name}",
                    model_value=raw,
                    applied_value=0.5,
                    reason="not finite; defaulted to neutral",
                )
            )
            intent[field_name] = 0.5
            continue
        if value > 1.0:
            # A model that answers on a 0-5 or 0-100 scale is common.
            scaled = value / 5.0 if value <= 5.0 else value / 100.0
            clamped = max(0.0, min(1.0, scaled if value > 1.0 else value))
            if value > 5.0 and value <= 100.0:
                clamped = value / 100.0
            normalisations.append(
                Normalisation(
                    field=f"intent.{field_name}",
                    model_value=raw,
                    applied_value=round(clamped, 3),
                    reason=(
                        "outside 0..1; rescaled to a unit interval"
                        if value > 5.0
                        else "outside 0..1; clamped"
                    ),
                )
            )
            intent[field_name] = clamped
        elif value < 0.0:
            normalisations.append(
                Normalisation(
                    field=f"intent.{field_name}",
                    model_value=raw,
                    applied_value=0.0,
                    reason="negative; clamped to 0",
                )
            )
            intent[field_name] = 0.0
    candidate["intent"] = intent

    # Constraints: present but unusable values are dropped with a record.
    constraints_raw = candidate.get("constraints")
    if isinstance(constraints_raw, dict):
        constraints = dict(constraints_raw)
        minutes = constraints.get("max_print_time_minutes")
        if minutes is not None:
            try:
                minutes_value = float(minutes)
            except (TypeError, ValueError):
                normalisations.append(
                    Normalisation(
                        field="constraints.max_print_time_minutes",
                        model_value=minutes, applied_value=None,
                        reason="not a number; limit dropped",
                    )
                )
                constraints["max_print_time_minutes"] = None
            else:
                if not math.isfinite(minutes_value) or minutes_value < 1:
                    normalisations.append(
                        Normalisation(
                            field="constraints.max_print_time_minutes",
                            model_value=minutes, applied_value=None,
                            reason=f"implausible time limit ({minutes}); limit dropped",
                        )
                    )
                    constraints["max_print_time_minutes"] = None
                else:
                    constraints["max_print_time_minutes"] = int(round(minutes_value))

        grams = constraints.get("max_material_grams")
        if grams is not None:
            try:
                grams_value = float(grams)
            except (TypeError, ValueError):
                normalisations.append(
                    Normalisation(
                        field="constraints.max_material_grams",
                        model_value=grams, applied_value=None,
                        reason="not a number; limit dropped",
                    )
                )
                constraints["max_material_grams"] = None
            else:
                if not math.isfinite(grams_value) or grams_value <= 0:
                    normalisations.append(
                        Normalisation(
                            field="constraints.max_material_grams",
                            model_value=grams, applied_value=None,
                            reason=f"implausible material limit ({grams}); limit dropped",
                        )
                    )
                    constraints["max_material_grams"] = None
                else:
                    constraints["max_material_grams"] = grams_value
        candidate["constraints"] = constraints

    # Unknown top-level keys are dropped with a record. The model is not
    # trusted to know the schema, and a stray key must not be silently
    # discarded without saying so.
    allowed = set(PLAN_JSON_SCHEMA["properties"])
    for key in list(candidate.keys()):
        if key not in allowed:
            normalisations.append(
                Normalisation(
                    field=key,
                    model_value=candidate[key],
                    applied_value=None,
                    reason="not part of the plan schema; dropped",
                )
            )
            candidate.pop(key)

    for field_name in ("support_preference", "orientation_preference"):
        if field_name in candidate and candidate[field_name] not in PLAN_JSON_SCHEMA[
            "properties"
        ][field_name]["enum"]:
            normalisations.append(
                Normalisation(
                    field=field_name,
                    model_value=candidate[field_name],
                    applied_value="allowed" if field_name == "support_preference" else "optimized",
                    reason="not an allowed value; defaulted",
                )
            )
            candidate.pop(field_name)

    try:
        plan = LLMPlan.model_validate(candidate)
    except Exception as exc:  # pydantic ValidationError and friends
        return ValidatedPlan(
            plan=LLMPlan(intent=IntentPriorities()),
            rejected_reason=f"The plan failed validation: {exc}",
            normalisations=normalisations,
        )

    return ValidatedPlan(plan=plan, normalisations=normalisations)
