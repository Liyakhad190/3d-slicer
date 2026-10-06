"""The structured AI print plan: the only shape a model may answer in.

Design rules, in priority order:

1. **Every category in the print-preparation checklist is a field.** A category
   the model considers unnecessary must say so with ``status = "not_required"``
   rather than being omitted, because "not evaluated" and "not needed" are
   different claims and the UI shows them differently.
2. **Ranges are enforced here, not by the model.** Ollama's ``format``
   constrains the *structure* of an answer and never its values (measured: a
   1.5B model returned ``5`` for ``minimum: 0, maximum: 1``). Every numeric
   bound is therefore clamped by `coerce_print_plan`, which records the
   correction instead of hiding it.
3. **No field exists for a G-code command, a coordinate or a machine limit.**
   The model proposes *print preparation decisions*; the deterministic resolver
   in `app.preparation.validators` turns them into canonical parameters and is
   free to refuse them.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.intelligence.schemas import IntentPriorities, Normalisation

CategoryStatus = Literal["required", "not_required", "unsupported"]
SupportPlacement = Literal["build_plate_only", "everywhere"]
AdhesionRisk = Literal["LOW_RISK", "MEDIUM_RISK", "HIGH_RISK"]
Strategy = Literal[
    "material_profile",
    "validated_range",
    "balanced",
    "quality",
    "speed",
    "strength",
    "material",
    "printer_material_profile",
    "geometry_driven",
    "disabled",
]

#: Categories A..Z of the print-preparation checklist, in checklist order.
CHECKLIST: tuple[str, ...] = (
    "model_validity",
    "orientation",
    "bed_placement",
    "layer_height",
    "walls",
    "top_bottom",
    "infill",
    "supports",
    "support_interface",
    "brim",
    "raft",
    "bed_adhesion",
    "temperature",
    "cooling",
    "speed",
    "flow",
    "retraction",
    "first_layer",
    "acceleration",
    "min_layer_time",
    "material_specific",
    "slicer_specific",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CategoryBase(StrictModel):
    """Shared envelope for every checklist category."""

    status: CategoryStatus = "required"
    reason: str = Field(default="", max_length=500)
    #: Concrete, measurable facts. Generic assertions are rejected by the UI
    #: contract: an evidence line must name a number, a limit or a profile.
    evidence: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("evidence")
    @classmethod
    def _bounded(cls, value: list[str]) -> list[str]:
        return [str(item)[:300] for item in value][:6]


class ModelValidity(CategoryBase):
    mesh_repair_required: bool = False
    watertight: bool = True


class Orientation(CategoryBase):
    candidate_id: str = Field(default="", max_length=64)
    strategy: str = Field(default="", max_length=120)


class BedPlacement(CategoryBase):
    strategy: str = Field(default="center_on_bed", max_length=120)


class LayerHeight(CategoryBase):
    value_mm: float | None = Field(default=None, gt=0, le=2.0)


class Walls(CategoryBase):
    count: int | None = Field(default=None, ge=1, le=20)


class TopBottom(CategoryBase):
    top_layers: int | None = Field(default=None, ge=0, le=50)
    bottom_layers: int | None = Field(default=None, ge=0, le=50)


class Infill(CategoryBase):
    density_percent: float | None = Field(default=None, ge=0, le=100)
    pattern: str | None = Field(default=None, max_length=32)


class Supports(CategoryBase):
    enabled: bool = False
    placement: SupportPlacement = "build_plate_only"
    angle_deg: float | None = Field(default=None, ge=0, le=90)
    density_percent: float | None = Field(default=None, ge=0, le=100)
    pattern: str | None = Field(default=None, max_length=32)


class SupportInterface(CategoryBase):
    enabled: bool = False
    height_mm: float | None = Field(default=None, gt=0, le=10)
    density_percent: float | None = Field(default=None, ge=0, le=100)


class Brim(CategoryBase):
    enabled: bool = False
    width_mm: float | None = Field(default=None, ge=0, le=100)


class Raft(CategoryBase):
    enabled: bool = False


class BedAdhesion(CategoryBase):
    risk: AdhesionRisk = "LOW_RISK"


class Temperature(CategoryBase):
    nozzle_c: int | None = Field(default=None, ge=0, le=1000)
    bed_c: int | None = Field(default=None, ge=0, le=1000)
    first_layer_nozzle_c: int | None = Field(default=None, ge=0, le=1000)
    first_layer_bed_c: int | None = Field(default=None, ge=0, le=1000)
    source: str = Field(default="validated_profile", max_length=40)


class Cooling(CategoryBase):
    fan_percent: int | None = Field(default=None, ge=0, le=100)
    min_layer_time_s: float | None = Field(default=None, ge=0, le=600)


class Speed(CategoryBase):
    print_mm_s: float | None = Field(default=None, gt=0, le=1000)
    outer_wall_mm_s: float | None = Field(default=None, gt=0, le=1000)
    inner_wall_mm_s: float | None = Field(default=None, gt=0, le=1000)
    infill_mm_s: float | None = Field(default=None, gt=0, le=1000)
    first_layer_mm_s: float | None = Field(default=None, gt=0, le=1000)
    support_mm_s: float | None = Field(default=None, gt=0, le=1000)
    travel_mm_s: float | None = Field(default=None, gt=0, le=2000)


class Flow(CategoryBase):
    strategy: str = Field(default="validated_material_profile", max_length=60)
    volumetric_flow_mm3s: float | None = Field(default=None, ge=0, le=1000)


class Retraction(CategoryBase):
    distance_mm: float | None = Field(default=None, ge=0, le=50)
    speed_mm_s: float | None = Field(default=None, ge=0, le=500)
    z_hop_mm: float | None = Field(default=None, ge=0, le=10)


class FirstLayer(CategoryBase):
    height_mm: float | None = Field(default=None, gt=0, le=2.0)
    speed_mm_s: float | None = Field(default=None, gt=0, le=500)
    line_width_mm: float | None = Field(default=None, gt=0, le=5)
    fan_percent: int | None = Field(default=None, ge=0, le=100)


class Acceleration(CategoryBase):
    value_mm_s2: float | None = Field(default=None, gt=0, le=100_000)
    wall_mm_s2: float | None = Field(default=None, gt=0, le=100_000)
    travel_mm_s2: float | None = Field(default=None, gt=0, le=100_000)


class MinLayerTime(CategoryBase):
    seconds: float | None = Field(default=None, ge=0, le=600)


class MaterialSpecific(CategoryBase):
    honoured: list[str] = Field(default_factory=list, max_length=8)


class SlicerSpecific(CategoryBase):
    requested_features: list[str] = Field(default_factory=list, max_length=8)


class Objective(StrictModel):
    """What the user asked the print to optimise. Reuses the intent schema."""

    intent: IntentPriorities = Field(default_factory=IntentPriorities)


class AiPrintPlan(StrictModel):
    """A complete print-preparation proposal.

    Every checklist category is present. A category the model did not need
    still appears, carrying ``status = "not_required"`` and a reason, because
    the platform must be able to distinguish "considered and unnecessary" from
    "never looked at".
    """

    model_validity: ModelValidity = Field(default_factory=ModelValidity)
    orientation: Orientation = Field(default_factory=Orientation)
    bed_placement: BedPlacement = Field(default_factory=BedPlacement)
    layer_height: LayerHeight = Field(default_factory=LayerHeight)
    walls: Walls = Field(default_factory=Walls)
    top_bottom: TopBottom = Field(default_factory=TopBottom)
    infill: Infill = Field(default_factory=Infill)
    supports: Supports = Field(default_factory=Supports)
    support_interface: SupportInterface = Field(default_factory=SupportInterface)
    brim: Brim = Field(default_factory=Brim)
    raft: Raft = Field(default_factory=Raft)
    bed_adhesion: BedAdhesion = Field(default_factory=BedAdhesion)
    temperature: Temperature = Field(default_factory=Temperature)
    cooling: Cooling = Field(default_factory=Cooling)
    speed: Speed = Field(default_factory=Speed)
    flow: Flow = Field(default_factory=Flow)
    retraction: Retraction = Field(default_factory=Retraction)
    first_layer: FirstLayer = Field(default_factory=FirstLayer)
    acceleration: Acceleration = Field(default_factory=Acceleration)
    min_layer_time: MinLayerTime = Field(default_factory=MinLayerTime)
    material_specific: MaterialSpecific = Field(default_factory=MaterialSpecific)
    slicer_specific: SlicerSpecific = Field(default_factory=SlicerSpecific)
    objective: Objective = Field(default_factory=Objective)
    summary: str = Field(default="", max_length=800)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    def category(self, name: str) -> CategoryBase:
        value = getattr(self, name)
        if not isinstance(value, CategoryBase):  # pragma: no cover - defensive
            raise KeyError(name)
        return value

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class ValidatedPrintPlan(BaseModel):
    """A coerced plan plus every correction the coercion had to make."""

    plan: AiPrintPlan
    normalisations: list[Normalisation] = Field(default_factory=list)
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


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "object", "properties": properties}
    node["required"] = required or list(properties)
    return node


_NUM = {"type": "number"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_STR = {"type": "string"}
_STATUS = {"type": "string", "enum": ["required", "not_required", "unsupported"]}
_EVIDENCE = {"type": "array", "items": {"type": "string"}, "maxItems": 6}


def _category(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "status": _STATUS,
        "reason": _STR,
        "evidence": _EVIDENCE,
    }
    properties.update(extra or {})
    # Only the envelope is required: a 1.5B model asked for twenty-two fully
    # populated objects answers with prose instead. Defaults in the pydantic
    # models fill whatever it leaves out, and the coercion records it.
    return _object(properties, required=["status", "reason", "evidence"])


#: JSON Schema handed to the provider. Declared rather than generated: Ollama
#: needs inlined enums and explicit `required` arrays, and a mechanical dump of
#: the pydantic schema does not produce them. `test_print_plan_schema_matches_models`
#: asserts the two stay in sync.
PRINT_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "model_validity": _category({"mesh_repair_required": _BOOL, "watertight": _BOOL}),
        "orientation": _category({"candidate_id": _STR, "strategy": _STR}),
        "bed_placement": _category({"strategy": _STR}),
        "layer_height": _category({"value_mm": {"type": ["number", "null"]}}),
        "walls": _category({"count": {"type": ["integer", "null"]}}),
        "top_bottom": _category(
            {
                "top_layers": {"type": ["integer", "null"]},
                "bottom_layers": {"type": ["integer", "null"]},
            }
        ),
        "infill": _category(
            {"density_percent": {"type": ["number", "null"]}, "pattern": {"type": ["string", "null"]}}
        ),
        "supports": _category(
            {
                "enabled": _BOOL,
                "placement": {"type": "string", "enum": ["build_plate_only", "everywhere"]},
                "angle_deg": {"type": ["number", "null"]},
                "density_percent": {"type": ["number", "null"]},
                "pattern": {"type": ["string", "null"]},
            }
        ),
        "support_interface": _category(
            {
                "enabled": _BOOL,
                "height_mm": {"type": ["number", "null"]},
                "density_percent": {"type": ["number", "null"]},
            }
        ),
        "brim": _category({"enabled": _BOOL, "width_mm": {"type": ["number", "null"]}}),
        "raft": _category({"enabled": _BOOL}),
        "bed_adhesion": _category(
            {"risk": {"type": "string", "enum": ["LOW_RISK", "MEDIUM_RISK", "HIGH_RISK"]}}
        ),
        "temperature": _category(
            {
                "nozzle_c": {"type": ["integer", "null"]},
                "bed_c": {"type": ["integer", "null"]},
                "first_layer_nozzle_c": {"type": ["integer", "null"]},
                "first_layer_bed_c": {"type": ["integer", "null"]},
                "source": _STR,
            }
        ),
        "cooling": _category(
            {"fan_percent": {"type": ["integer", "null"]}, "min_layer_time_s": {"type": ["number", "null"]}}
        ),
        "speed": _category(
            {
                "print_mm_s": {"type": ["number", "null"]},
                "outer_wall_mm_s": {"type": ["number", "null"]},
                "inner_wall_mm_s": {"type": ["number", "null"]},
                "infill_mm_s": {"type": ["number", "null"]},
                "first_layer_mm_s": {"type": ["number", "null"]},
                "support_mm_s": {"type": ["number", "null"]},
                "travel_mm_s": {"type": ["number", "null"]},
            }
        ),
        "flow": _category(
            {"strategy": _STR, "volumetric_flow_mm3s": {"type": ["number", "null"]}}
        ),
        "retraction": _category(
            {
                "distance_mm": {"type": ["number", "null"]},
                "speed_mm_s": {"type": ["number", "null"]},
                "z_hop_mm": {"type": ["number", "null"]},
            }
        ),
        "first_layer": _category(
            {
                "height_mm": {"type": ["number", "null"]},
                "speed_mm_s": {"type": ["number", "null"]},
                "line_width_mm": {"type": ["number", "null"]},
                "fan_percent": {"type": ["integer", "null"]},
            }
        ),
        "acceleration": _category(
            {
                "value_mm_s2": {"type": ["number", "null"]},
                "wall_mm_s2": {"type": ["number", "null"]},
                "travel_mm_s2": {"type": ["number", "null"]},
            }
        ),
        "min_layer_time": _category({"seconds": {"type": ["number", "null"]}}),
        "material_specific": _category(
            {"honoured": {"type": "array", "items": {"type": "string"}, "maxItems": 8}}
        ),
        "slicer_specific": _category(
            {"requested_features": {"type": "array", "items": {"type": "string"}, "maxItems": 8}}
        ),
        "objective": _object(
            {
                "intent": _object(
                    {
                        "strength": {"type": "number", "minimum": 0, "maximum": 1},
                        "speed": {"type": "number", "minimum": 0, "maximum": 1},
                        "material_efficiency": {"type": "number", "minimum": 0, "maximum": 1},
                        "surface_quality": {"type": "number", "minimum": 0, "maximum": 1},
                    }
                )
            }
        ),
        "summary": _STR,
        "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
    },
    "required": [*CHECKLIST, "objective", "summary", "confidence"],
}


def _clamp_number(
    container: dict[str, Any],
    key: str,
    *,
    low: float,
    high: float,
    normalisations: list[Normalisation],
    path: str,
    integer: bool = False,
    scale_hint: bool = False,
) -> None:
    """Bring one numeric field into range, recording every correction."""
    if key not in container or container[key] is None:
        return
    raw = container[key]
    try:
        value = float(raw)
    except (TypeError, ValueError):
        container[key] = None
        normalisations.append(
            Normalisation(
                field=path, model_value=raw, applied_value=None,
                reason="not a number; field dropped",
            )
        )
        return
    if not math.isfinite(value):
        container[key] = None
        normalisations.append(
            Normalisation(
                field=path, model_value=raw, applied_value=None,
                reason="not finite; field dropped",
            )
        )
        return
    if scale_hint and (value > high and value <= high * 10):
        # A model asked for 0..1 answering on 0..10 is common enough to name.
        scaled = value / 10.0
        normalisations.append(
            Normalisation(
                field=path, model_value=raw, applied_value=round(scaled, 4),
                reason=f"outside {low}..{high}; read as a 0..10 scale",
            )
        )
        value = scaled
    if value < low or value > high:
        clamped = max(low, min(high, value))
        normalisations.append(
            Normalisation(
                field=path, model_value=raw, applied_value=clamped,
                reason=f"outside the allowed range {low}..{high}; clamped",
            )
        )
        value = clamped
    container[key] = int(round(value)) if integer else round(value, 4)


def coerce_print_plan(payload: Any) -> ValidatedPrintPlan:
    """Validate a model's JSON into a `ValidatedPrintPlan`.

    Clamping rather than rejecting for anything expressible as a number: the
    user wants a printable plan, and a recorded correction is more useful than
    an error about a scale mismatch. Missing categories are filled with their
    defaults, which is honest only because the defaults declare
    ``status = "required"`` with an empty reason and the resolver then reports
    that no evidence was supplied.

    Anything structurally unusable — not an object, a category that is not an
    object — is rejected outright. There is no fallback plan.
    """
    normalisations: list[Normalisation] = []

    if not isinstance(payload, dict):
        return ValidatedPrintPlan(
            plan=AiPrintPlan(),
            rejected_reason=f"Expected a JSON object, got {type(payload).__name__}.",
        )

    candidate = dict(payload)

    unknown = [k for k in candidate if k not in PRINT_PLAN_JSON_SCHEMA["properties"]]
    for key in unknown:
        normalisations.append(
            Normalisation(
                field=key, model_value=candidate.pop(key), applied_value=None,
                reason="not part of the print-plan schema; dropped",
            )
        )

    for name in CHECKLIST:
        raw = candidate.get(name, {})
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            return ValidatedPrintPlan(
                plan=AiPrintPlan(),
                rejected_reason=f"Category '{name}' must be an object, got {type(raw).__name__}.",
                normalisations=normalisations,
            )
        candidate[name] = raw

    # A plan that omits `summary` entirely must be normalised, not crash: the
    # model may legitimately return `{}`, and rejection is a decision, not an
    # exception.
    if not isinstance(candidate.get("summary"), str):
        candidate["summary"] = str(candidate.get("summary") or "")
    if len(candidate["summary"]) > 800:
        candidate["summary"] = candidate["summary"][:800]

    _clamp_number(
        candidate, "confidence", low=0.0, high=1.0,
        normalisations=normalisations, path="confidence", scale_hint=True,
    )
    if candidate.get("confidence") is None:
        candidate.pop("confidence", None)

    _clamp_number(
        candidate.get("layer_height") or {}, "value_mm", low=0.05, high=2.0,
        normalisations=normalisations, path="layer_height.value_mm",
    )
    _clamp_number(
        candidate.get("walls") or {}, "count", low=1, high=20,
        normalisations=normalisations, path="walls.count", integer=True,
    )
    for key in ("top_layers", "bottom_layers"):
        _clamp_number(
            candidate.get("top_bottom") or {}, key, low=0, high=50,
            normalisations=normalisations, path=f"top_bottom.{key}", integer=True,
        )
    _clamp_number(
        candidate.get("infill") or {}, "density_percent", low=0.0, high=100.0,
        normalisations=normalisations, path="infill.density_percent",
    )

    supports = candidate.get("supports") or {}
    _clamp_number(supports, "angle_deg", low=0.0, high=90.0,
                   normalisations=normalisations, path="supports.angle_deg")
    _clamp_number(supports, "density_percent", low=0.0, high=100.0,
                   normalisations=normalisations, path="supports.density_percent")
    if supports.get("placement") not in ("build_plate_only", "everywhere"):
        if supports.get("placement") is not None:
            normalisations.append(
                Normalisation(
                    field="supports.placement", model_value=supports["placement"],
                    applied_value="build_plate_only",
                    reason="not an allowed placement; defaulted to build_plate_only",
                )
            )
        supports["placement"] = "build_plate_only"
    candidate["supports"] = supports

    _clamp_number(candidate.get("support_interface") or {}, "height_mm", low=0.05, high=10.0,
                   normalisations=normalisations, path="support_interface.height_mm")
    _clamp_number(candidate.get("support_interface") or {}, "density_percent", low=0.0, high=100.0,
                   normalisations=normalisations, path="support_interface.density_percent")
    _clamp_number(candidate.get("brim") or {}, "width_mm", low=0.0, high=100.0,
                   normalisations=normalisations, path="brim.width_mm")

    adhesion = candidate.get("bed_adhesion") or {}
    if adhesion.get("risk") not in ("LOW_RISK", "MEDIUM_RISK", "HIGH_RISK"):
        if adhesion.get("risk") is not None:
            normalisations.append(
                Normalisation(
                    field="bed_adhesion.risk", model_value=adhesion["risk"],
                    applied_value="MEDIUM_RISK",
                    reason="not an allowed risk level; defaulted to MEDIUM_RISK",
                )
            )
        adhesion["risk"] = "MEDIUM_RISK"
    candidate["bed_adhesion"] = adhesion

    temperature = candidate.get("temperature") or {}
    for key in ("nozzle_c", "bed_c", "first_layer_nozzle_c", "first_layer_bed_c"):
        _clamp_number(temperature, key, low=0, high=1000,
                      normalisations=normalisations, path=f"temperature.{key}", integer=True)
    candidate["temperature"] = temperature

    _clamp_number(candidate.get("cooling") or {}, "fan_percent", low=0, high=100,
                   normalisations=normalisations, path="cooling.fan_percent", integer=True)
    _clamp_number(candidate.get("cooling") or {}, "min_layer_time_s", low=0.0, high=600.0,
                   normalisations=normalisations, path="cooling.min_layer_time_s")

    speed = candidate.get("speed") or {}
    for key, high in (
        ("print_mm_s", 1000.0), ("outer_wall_mm_s", 1000.0), ("inner_wall_mm_s", 1000.0),
        ("infill_mm_s", 1000.0), ("first_layer_mm_s", 1000.0), ("support_mm_s", 1000.0),
        ("travel_mm_s", 2000.0),
    ):
        _clamp_number(speed, key, low=0.1, high=high,
                      normalisations=normalisations, path=f"speed.{key}")
    candidate["speed"] = speed

    retraction = candidate.get("retraction") or {}
    for key, high in (("distance_mm", 50.0), ("speed_mm_s", 500.0), ("z_hop_mm", 10.0)):
        _clamp_number(retraction, key, low=0.0, high=high,
                      normalisations=normalisations, path=f"retraction.{key}")
    candidate["retraction"] = retraction

    first_layer = candidate.get("first_layer") or {}
    _clamp_number(first_layer, "height_mm", low=0.05, high=2.0,
                   normalisations=normalisations, path="first_layer.height_mm")
    _clamp_number(first_layer, "speed_mm_s", low=0.1, high=500.0,
                   normalisations=normalisations, path="first_layer.speed_mm_s")
    _clamp_number(first_layer, "line_width_mm", low=0.1, high=5.0,
                   normalisations=normalisations, path="first_layer.line_width_mm")
    _clamp_number(first_layer, "fan_percent", low=0, high=100,
                   normalisations=normalisations, path="first_layer.fan_percent", integer=True)
    candidate["first_layer"] = first_layer

    acceleration = candidate.get("acceleration") or {}
    for key in ("value_mm_s2", "wall_mm_s2", "travel_mm_s2"):
        _clamp_number(acceleration, key, low=1.0, high=100_000.0,
                      normalisations=normalisations, path=f"acceleration.{key}")
    candidate["acceleration"] = acceleration

    _clamp_number(candidate.get("min_layer_time") or {}, "seconds", low=0.0, high=600.0,
                   normalisations=normalisations, path="min_layer_time.seconds")
    _clamp_number(candidate.get("flow") or {}, "volumetric_flow_mm3s", low=0.0, high=1000.0,
                   normalisations=normalisations, path="flow.volumetric_flow_mm3s")

    objective = candidate.get("objective")
    if objective is None:
        candidate["objective"] = {"intent": {}}
    elif not isinstance(objective, dict):
        candidate["objective"] = {"intent": {}}
        normalisations.append(
            Normalisation(
                field="objective", model_value=objective, applied_value={"intent": {}},
                reason="not an object; defaults used",
            )
        )
    elif not isinstance(objective.get("intent"), dict):
        objective["intent"] = {}

    intent = candidate["objective"]["intent"]
    for field_name in ("strength", "speed", "material_efficiency", "surface_quality"):
        _clamp_number(intent, field_name, low=0.0, high=1.0,
                      normalisations=normalisations, path=f"objective.intent.{field_name}",
                      scale_hint=True)
        if intent.get(field_name) is None:
            intent[field_name] = 0.5

    for name in ("material_specific", "slicer_specific"):
        node = candidate.get(name) or {}
        for list_key in ("honoured", "requested_features"):
            if list_key in node and not isinstance(node[list_key], list):
                node[list_key] = []
        candidate[name] = node

    try:
        plan = AiPrintPlan.model_validate(candidate)
    except Exception as exc:  # pydantic ValidationError and friends
        return ValidatedPrintPlan(
            plan=AiPrintPlan(),
            rejected_reason=f"The print plan failed validation: {exc}",
            normalisations=normalisations,
        )

    return ValidatedPrintPlan(plan=plan, normalisations=normalisations)
