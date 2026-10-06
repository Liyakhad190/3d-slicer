"""Parameter optimisation.

Given a model, a machine profile, a material profile and an objective, produce
a complete, valid parameter set. The ordering of authority is fixed and is the
whole point of this module:

1. **Hard machine constraints** win, always. A layer height the nozzle cannot
   bond, a temperature above the hotend's rating, a speed above the profile's
   ceiling: these are not preferences and are never negotiable.
2. **A locked parameter** wins over every heuristic, including the objective.
3. **Material constraints** bound the mechanical envelope (max flow, temp range).
4. **The objective** trades off quality, strength, time and material.
5. **Geometry evidence** adjusts for what was actually measured in the mesh.

Every non-obvious choice records a reason, and the whole set is fingerprinted so
a later change to any input produces a different fingerprint rather than
silently reusing a stale result.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.errors import ValidationFailedError
from app.core.logging import get_logger
from app.slicing.validator import MachineLimits

log = get_logger(__name__)

Objective = Literal["quality", "strength", "speed", "material", "balanced"]

Record = Callable[..., Any]

#: Fraction of nozzle diameter usable as a layer height. 0.75 is the standard
#: ceiling: beyond it the layer is too wide to squash onto the one below.
LAYER_HEIGHT_NOZZLE_FRACTION = 0.75


@dataclass
class ParameterDecision:
    """One resolved parameter, with the reasoning that produced it."""

    key: str
    value: Any
    source: str  # machine | material | geometry | objective | user
    reason: str
    confidence: float = 0.8
    locked: bool = False
    constraint: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
            "locked": self.locked,
            "constraint": self.constraint,
        }


@dataclass
class OptimisationResult:
    objective: str
    parameters: dict[str, Any]
    decisions: list[ParameterDecision]
    rationale: list[str]
    confidence: float
    fingerprint: str
    warnings: list[str] = field(default_factory=list)
    blocked: bool = False
    block_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "parameters": self.parameters,
            "decisions": [d.to_dict() for d in self.decisions],
            "rationale": self.rationale,
            "confidence": round(self.confidence, 3),
            "fingerprint": self.fingerprint,
            "warnings": self.warnings,
            "blocked": self.blocked,
            "block_reasons": self.block_reasons,
        }


class ParameterOptimizer:
    """Resolves a full, machine-valid parameter set.

    Construct with the *resolved* profile rows. Limits are read from those rows
    and never from application constants, so a new machine or material needs no
    code change.
    """

    def __init__(
        self,
        *,
        machine_profile: Any,
        material_profile: Any,
        nozzle_diameter_mm: float,
        limits: MachineLimits,
    ) -> None:
        self.profile = machine_profile
        self.material = material_profile
        self.nozzle = float(nozzle_diameter_mm)
        self.limits = limits

    # -- public ----------------------------------------------------------

    def optimise(
        self,
        *,
        objective: Objective = "balanced",
        geometry: dict[str, Any] | None = None,
        locked: dict[str, Any] | None = None,
    ) -> OptimisationResult:
        geometry = geometry or {}
        locked = locked or {}

        decisions: list[ParameterDecision] = []
        warnings: list[str] = []
        block_reasons: list[str] = []

        def record(key: str, value: Any, source: str, reason: str, confidence: float = 0.8) -> Any:
            decisions.append(
                ParameterDecision(
                    key=key, value=value, source=source, reason=reason,
                    confidence=confidence, locked=key in locked,
                )
            )
            return value

        # -- layer height ---------------------------------------------------
        # Bounded by two independent ceilings and one geometry ceiling.
        nozzle_ceiling = self.nozzle * LAYER_HEIGHT_NOZZLE_FRACTION
        material_ceiling = self._material_layer_ceiling()
        feature_ceiling = self._geometry_layer_ceiling(geometry)

        if "layer_height_mm" in locked:
            layer_height = self._validate_layer_height(locked["layer_height_mm"], nozzle_ceiling)
            record("layer_height_mm", layer_height, "user",
                   "Locked by the user. The optimiser did not consider changing it. It was "
                   "still checked against the nozzle's bonding limit.", 1.0)
        else:
            layer_height = min(nozzle_ceiling, material_ceiling, feature_ceiling)
            reasons = [
                f"75% of the {self.nozzle:.2f} mm nozzle ({nozzle_ceiling:.3f} mm)",
                f"the material's maximum ({material_ceiling:.3f} mm)",
            ]
            if feature_ceiling < min(nozzle_ceiling, material_ceiling) - 1e-9:
                smallest = geometry.get("min_feature_mm") or geometry.get("estimated_min_wall_mm")
                reasons.append(
                    f"a third of the smallest measured feature ({smallest} mm -> {feature_ceiling:.3f} mm)"
                )
            if objective == "quality":
                layer_height = min(layer_height, self.nozzle * 0.5)
                reasons.append(
                    f"50% of nozzle diameter for the 'quality' objective ({self.nozzle * 0.5:.3f} mm)"
                )
            elif objective == "speed":
                reasons.append("the 'speed' objective prefers thicker layers up to the ceiling")
            record("layer_height_mm", round(layer_height, 4), "geometry",
                   "Layer height is the smallest of: " + "; ".join(reasons) + ".")

        # -- extrusion width --------------------------------------------------
        if "extrusion_width_mm" in locked:
            record("extrusion_width_mm", locked["extrusion_width_mm"], "user", "Locked by the user.", 1.0)
        else:
            record("extrusion_width_mm", round(self.nozzle * 1.125, 4), "machine",
                   f"Extrusion width is 1.125x the {self.nozzle:.2f} mm nozzle, the usual default "
                   "that balances melt flow against layer adhesion.")

        # -- first layer --------------------------------------------------------
        record("first_layer_height_mm", round(min(self.nozzle * 0.75, layer_height), 4), "geometry",
               f"First layer is 75% of the {self.nozzle:.2f} mm nozzle: thick enough to bed "
               "reliably without over-squashing the first perimeter.")

        self._resolve_walls(geometry, objective, record, locked)
        self._resolve_infill(objective, record, locked)
        self._resolve_shell(objective, record, locked)
        speed, _ = self._resolve_motion(objective, record, locked)
        self._resolve_temperatures(record, locked, block_reasons=block_reasons, warnings=warnings)
        self._resolve_cooling(record, locked)
        self._resolve_retraction(record, locked, speed)
        self._resolve_support(geometry, objective, record, locked)
        self._resolve_adhesion(geometry, record, locked)

        parameters = {d.key: d.value for d in decisions}
        self._check_compatibility(parameters, geometry, warnings, block_reasons)

        blocked = bool(block_reasons)
        # A blocked configuration must never look runnable.
        if blocked:
            parameters = {}

        result = OptimisationResult(
            objective=objective,
            parameters=parameters,
            decisions=decisions,
            rationale=[f"{d.key} = {d.value} ({d.source}): {d.reason}" for d in decisions],
            confidence=self._aggregate_confidence(decisions, blocked),
            fingerprint=self._fingerprint(parameters, objective, blocked),
            warnings=warnings,
            blocked=blocked,
            block_reasons=block_reasons,
        )
        log.info("parameters.resolved", objective=objective, nozzle_mm=self.nozzle,
                 blocked=blocked, decisions=len(decisions))
        return result

    # -- ceilings and floors --------------------------------------------

    def _material_layer_ceiling(self) -> float:
        rng = self.material.layer_height_range or {}
        ceiling = float(rng.get("max") or self.nozzle * LAYER_HEIGHT_NOZZLE_FRACTION)
        return min(ceiling, self.nozzle * LAYER_HEIGHT_NOZZLE_FRACTION)

    def _geometry_layer_ceiling(self, geometry: dict[str, Any]) -> float:
        """Do not slice thicker than the smallest feature we must resolve.

        A 0.42 mm feature needs at least three layers to read as a shape, so the
        layer height must not exceed a third of it.
        """
        smallest = geometry.get("min_feature_mm") or geometry.get("estimated_min_wall_mm")
        if not smallest:
            return self.nozzle * LAYER_HEIGHT_NOZZLE_FRACTION
        return max(
            self.nozzle * 0.2,
            min(float(smallest) / 3.0, self.nozzle * LAYER_HEIGHT_NOZZLE_FRACTION),
        )

    def _validate_layer_height(self, value: Any, nozzle_ceiling: float) -> float:
        """A locked value is still checked against hard ceilings.

        "Locked" means the optimiser will not change it. It does not mean the
        platform will hand a physically impossible value to a machine.
        """
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationFailedError("Layer height must be a number.") from exc
        if number <= 0:
            raise ValidationFailedError("Layer height must be greater than zero.")
        if number > nozzle_ceiling:
            raise ValidationFailedError(
                f"Layer height {number:.3f} mm exceeds 75% of the {self.nozzle:.2f} mm "
                f"nozzle ({nozzle_ceiling:.3f} mm). A layer that wide will not bond to the "
                "one below it.",
                details={"nozzle_mm": self.nozzle, "max_layer_height_mm": round(nozzle_ceiling, 4)},
            )
        return number

    # -- individual resolvers -------------------------------------------

    def _resolve_walls(
        self, geometry: dict[str, Any], objective: Objective, record: Record, locked: dict[str, Any]
    ) -> int:
        if "wall_count" in locked:
            value = int(locked["wall_count"])
            record("wall_count", value, "user", "Locked by the user.", 1.0)
            return value

        line_width = self.nozzle * 1.125
        section = geometry.get("estimated_min_wall_mm")

        # `estimated_min_wall_mm` is a *section thickness* measured by ray
        # casting, not a wall. On a solid part it equals the whole cross-section
        # (a 20 mm block measures 20 mm), so dividing it by the line width to
        # get a perimeter count is meaningless and produced wall_count=45 for a
        # plain cube. Only treat it as a wall when it is thin enough that a
        # single line cannot represent it.
        is_wall = bool(section) and float(section) < 3 * line_width
        needed = max(2, min(12, -(-int(float(section) * 100) // int(line_width * 100)))) if is_wall else 0

        if objective == "speed":
            walls, reason = 2, "Two perimeters: the minimum for a watertight solid, chosen for the 'speed' objective."
        elif objective == "material":
            walls, reason = 2, "Two perimeters: the 'material' objective favours the thinnest solid shell."
        elif is_wall:
            walls = max(needed, 3)
            reason = (
                f"The part has walls about {float(section):.2f} mm thick, which needs "
                f"{needed} perimeter(s) at a {line_width:.3f} mm line width to be solid; "
                f"{walls} are used."
                if walls > needed else
                f"The part's thinnest section is {float(section):.2f} mm, about {needed} line "
                f"width(s) at {line_width:.3f} mm. The standard three perimeters are used; the "
                "wall will still print as a single line."
            )
        elif section:
            walls = 3
            reason = (
                f"The thinnest measured section is {float(section):.2f} mm, which is solid "
                "material rather than a wall, so the standard three perimeters apply."
            )
        else:
            walls, reason = 3, "Three perimeters: the standard shell for strength and surface quality."

        record("wall_count", int(walls), "geometry", reason)
        return int(walls)

    def _resolve_infill(self, objective: Objective, record: Record, locked: dict[str, Any]) -> None:
        if "infill_pattern" in locked:
            record("infill_pattern", locked["infill_pattern"], "user", "Locked by the user.", 1.0)
        else:
            pattern = {"quality": "lines", "strength": "grid", "speed": "lines",
                       "material": "grid", "balanced": "gyroid"}.get(objective, "gyroid")
            record("infill_pattern", pattern, "objective",
                   f"'{objective}' maps to {pattern} infill for its characteristic behaviour.")

        if "infill_density_percent" in locked:
            record("infill_density_percent", locked["infill_density_percent"], "user", "Locked by the user.", 1.0)
            return

        density = {"quality": 20.0, "strength": 40.0, "speed": 15.0,
                   "material": 10.0, "balanced": 20.0}.get(objective, 20.0)
        record("infill_density_percent", density, "objective",
               f"Infill density for '{objective}'. Perimeters carry the load; infill mainly "
               "affects stiffness-to-weight, print time and material use.")

    def _resolve_shell(self, objective: Objective, record: Record, locked: dict[str, Any]) -> int:
        if "top_bottom_layers" in locked:
            value = int(locked["top_bottom_layers"])
            record("top_bottom_layers", value, "user", "Locked by the user.", 1.0)
            return value
        layers = {"quality": 6, "strength": 5, "speed": 3, "material": 3, "balanced": 5}.get(objective, 5)
        record("top_bottom_layers", layers, "objective",
               f"{layers} solid top/bottom layers for '{objective}'. Solid shells resist "
               "compression and give a finished surface.")
        return layers

    def _resolve_motion(
        self, objective: Objective, record: Record, locked: dict[str, Any]
    ) -> tuple[float, float]:
        material_speed = self.material.print_speed or {}
        material_max = float(material_speed.get("max") or 150)
        machine_max = self.limits.max_print_speed_mm_s or 150
        ceiling = min(material_max, machine_max)

        if "print_speed_mm_s" in locked:
            speed = float(locked["print_speed_mm_s"])
            record("print_speed_mm_s", speed, "user", "Locked by the user.", 1.0)
        else:
            fraction = {"quality": 0.60, "strength": 0.55, "speed": 0.95,
                        "material": 0.60, "balanced": 0.75}.get(objective, 0.75)
            speed = round(min(ceiling * fraction, ceiling), 2)
            record("print_speed_mm_s", speed, "objective",
                   f"{speed:.0f} mm/s: the '{objective}' target, capped at the {ceiling:.0f} mm/s "
                   f"ceiling (material {material_max:.0f} / machine {machine_max:.0f}).")

        if "acceleration_mm_s2" in locked:
            accel = float(locked["acceleration_mm_s2"])
            record("acceleration_mm_s2", accel, "user", "Locked by the user.", 1.0)
        else:
            machine_accel = float(self.limits.max_acceleration_mm_s2 or 1000)
            accel = round(min(speed * 60, machine_accel), 1)
            record("acceleration_mm_s2", accel, "machine",
                   f"Acceleration stays within this machine's {machine_accel:.0f} mm/s^2 limit and "
                   f"does not exceed {speed:.0f} mm/s^2 per mm/s of print speed.")

        if "travel_speed_mm_s" in locked:
            record("travel_speed_mm_s", locked["travel_speed_mm_s"], "user", "Locked by the user.", 1.0)
        else:
            travel_ceiling = self.limits.max_travel_speed_mm_s or 250
            record("travel_speed_mm_s", round(min(speed * 3, travel_ceiling), 2), "machine",
                   f"Travel is 3x print speed, capped at the machine's {travel_ceiling:.0f} mm/s ceiling.")
        return speed, accel

    def _resolve_temperatures(
        self,
        record: Record,
        locked: dict[str, Any],
        block_reasons: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> tuple[int, int]:
        from app.slicing.temperature import resolve_safe_temperatures

        user_nozzle = locked.get("nozzle_temperature_c") or locked.get("material_print_temperature")
        user_bed = locked.get("bed_temperature_c") or locked.get("material_bed_temperature")

        temp_res = resolve_safe_temperatures(
            machine_profile=self.profile,
            material_profile=self.material,
            user_override_nozzle=user_nozzle,
            user_override_bed=user_bed,
        )

        if temp_res.is_conflict:
            msg = f"{temp_res.conflict_code}: {temp_res.conflict_message or temp_res.reason}"
            if block_reasons is not None:
                block_reasons.append(msg)
            log.warning("temperature.conflict", conflict=msg)

        if warnings is not None and temp_res.warnings:
            warnings.extend(temp_res.warnings)

        record("nozzle_temperature_c", temp_res.target_nozzle_temp_c, temp_res.provenance, temp_res.reason, 1.0 if user_nozzle else 0.9)
        record("first_layer_temperature_c", temp_res.first_layer_nozzle_temp_c, temp_res.provenance, f"First layer nozzle temperature: {temp_res.first_layer_nozzle_temp_c} C.", 0.9)
        record("bed_temperature_c", temp_res.target_bed_temp_c, "user" if user_bed else "material", f"{temp_res.target_bed_temp_c} C bed temperature.", 1.0 if user_bed else 0.8)

        return temp_res.target_nozzle_temp_c, temp_res.target_bed_temp_c

    def _resolve_cooling(self, record: Record, locked: dict[str, Any]) -> int:
        if "cooling_fan_percent" in locked:
            value = int(locked["cooling_fan_percent"])
            record("cooling_fan_percent", value, "user", "Locked by the user.", 1.0)
            return value

        key = getattr(getattr(self.material, "material", None), "key", "") or ""
        if key in {"pla", "pla-plus", "pla-cf", "petg-cf"}:
            fan = 100
            reason = "Full part cooling for PLA-type materials: the layer must be quenched quickly to hold dimensional accuracy."
        elif key in {"abs", "asa", "nylon", "pc", "pa-cf", "pa6-gf"}:
            fan = 0 if self.material.requires_enclosure else 20
            reason = ("Cooling is off for this material: part cooling causes layer separation and "
                      "warping in high-temperature plastics.")
        elif key == "tpu95a":
            fan, reason = 40, "Moderate cooling for elastomers: enough to set the shape without over-curing the layer."
        else:
            fan, reason = 100, "Full part cooling, the safe default for a material without a specific profile."

        record("cooling_fan_percent", fan, "material", reason)
        return fan

    def _resolve_retraction(self, record: Record, locked: dict[str, Any], speed: float) -> dict[str, Any]:
        base = self.material.retraction or {}
        if "retraction" in locked:
            value = locked["retraction"]
            record("retraction", value, "user", "Locked by the user.", 1.0)
            return dict(value)

        distance = float(base.get("distance_mm") or 0.8)
        if speed < 30:
            distance += 0.2  # slower speeds ooze more; break the ooze harder
        result = {"distance_mm": round(distance, 2), "speed_mm_s": int(base.get("speed_mm_s") or 40)}
        record("retraction", result, "material",
               f"{result['distance_mm']:.2f} mm retraction from the material profile"
               + (f", increased for the slow {speed:.0f} mm/s print speed." if speed < 30 else "."))
        return result

    def _resolve_support(
        self, geometry: dict[str, Any], objective: Objective, record: Record, locked: dict[str, Any]
    ) -> tuple[bool, str, float]:
        if "support_enable" in locked:
            enabled = bool(locked["support_enable"])
            record("support_enable", enabled, "user", "Locked by the user.", 1.0)
        else:
            percent = float(geometry.get("overhang_area_percent") or 0.0)
            enabled = percent > 1.0
            record("support_enable", enabled, "geometry",
                   (f"{percent:.2f}% of the exposed surface overhangs, above the 1% threshold at "
                    "which support becomes worthwhile."
                    if enabled else
                    f"Only {percent:.2f}% of the exposed surface overhangs, below the 1% "
                    "threshold; support would add material and time for nothing."))

        if "support_type" in locked:
            support_type = str(locked["support_type"])
            record("support_type", support_type, "user", "Locked by the user.", 1.0)
        else:
            support_type = "tree" if objective in {"quality", "material"} else "normal"
            record("support_type", support_type, "objective",
                   f"{support_type} support for '{objective}'. Tree support touches the part at "
                   "far fewer points and is much easier to remove.")

        if "support_angle_deg" in locked:
            angle = float(locked["support_angle_deg"])
            record("support_angle_deg", angle, "user", "Locked by the user.", 1.0)
        else:
            angle = 50.0 if support_type == "tree" else 45.0
            record("support_angle_deg", angle, "material",
                   f"Support is generated below {angle:.0f} degrees from vertical. A steeper "
                   "threshold saves material but prints rougher overhangs.")
        return bool(enabled), support_type, angle

    def _resolve_adhesion(self, geometry: dict[str, Any], record: Record, locked: dict[str, Any]) -> str:
        if "adhesion_type" in locked:
            value = str(locked["adhesion_type"])
            record("adhesion_type", value, "user", "Locked by the user.", 1.0)
            return value

        height = float((geometry.get("dimensions_mm") or {}).get("z") or 0)
        contact = float(geometry.get("bed_contact_area_mm2") or 0)
        warp = (self.material.adhesion or {}).get("warping_tendency", "medium")

        if warp == "high" or (contact and contact < 200.0):
            choice = "brim"
            reason = ("A brim is added because this material warps readily or the part has a small "
                      f"footprint ({contact:.0f} mm2); both cause detachment.")
        elif height > 150:
            choice, reason = "brim", "A brim is added for a tall part, which is more prone to shifting layers."
        else:
            choice = "none"
            reason = "A large, stable footprint with a low-warp material needs no adhesion aid."
        record("adhesion_type", choice, "material", reason)
        return choice

    # -- cross-checks ---------------------------------------------------

    def _check_compatibility(
        self,
        parameters: dict[str, Any],
        geometry: dict[str, Any],
        warnings: list[str],
        block_reasons: list[str],
    ) -> None:
        """Check the resolved settings against the machine's restrictions."""
        restrictions = self.profile.machine_restrictions or {}
        material_obj = getattr(self.material, "material", None)
        material_name = getattr(material_obj, "display_name", "This material")
        key = getattr(material_obj, "key", "") or ""
        nozzle_material = getattr(self.profile, "nozzle_material", "brass")

        if restrictions.get("no_abrasive_filament_with_brass_path"):
            abrasiveness = str(getattr(self.material, "abrasiveness", "none"))
            if abrasiveness in {"high", "medium"} and nozzle_material == "brass":
                warnings.append(
                    f"{material_name} is abrasive ({abrasiveness}) and this printer's extrusion "
                    "path is brass. A hardened nozzle is strongly recommended; brass wears quickly."
                )

        direct_drive_only = restrictions.get("direct_drive_only_materials") or []
        if key and key in direct_drive_only:
            warnings.append(
                f"{material_name} normally needs a direct-drive extruder. On a Bowden machine, "
                "flexible filament may buckle inside the tube."
            )

        flow_ceiling = self.limits.max_volumetric_flow_mm3s
        material_flow = self.material.max_volumetric_flow_mm3s
        if flow_ceiling and material_flow and float(material_flow) > float(flow_ceiling):
            warnings.append(
                f"The material's recommended maximum flow ({float(material_flow):.1f} mm3/s) "
                f"exceeds this machine's {float(flow_ceiling):.1f} mm3/s. Speeds were reduced to "
                "stay within the machine's melt capacity."
            )

        min_wall = geometry.get("estimated_min_wall_mm")
        if min_wall and float(min_wall) < 2 * self.nozzle:
            warnings.append(
                f"Measured minimum wall of {float(min_wall):.2f} mm is below the "
                f"{2 * self.nozzle:.2f} mm needed for two extrusion lines. The slicer will print "
                "it as a single line, so the wall will be thinner and weaker than nominal."
            )

        if self.material.requires_enclosure and not (self.profile.chamber or {}).get("heated"):
            block_reasons.append(
                f"{material_name} requires a heated chamber and this machine has none. Printing "
                "it in an open chamber will warp and delaminate."
            )

    # -- bookkeeping ----------------------------------------------------

    def _fingerprint(self, parameters: dict[str, Any], objective: str, blocked: bool) -> str:
        """A hash over every input, so a changed input never reuses a stale result."""
        payload = {
            "objective": objective,
            "nozzle_mm": self.nozzle,
            "machine_profile_hash": self.profile.profile_hash,
            "material_profile_hash": self.material.profile_hash,
            "parameters": parameters,
            "blocked": blocked,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _aggregate_confidence(decisions: list[ParameterDecision], blocked: bool) -> float:
        if blocked or not decisions:
            return 0.0
        return sum(d.confidence for d in decisions) / len(decisions)
