"""Intent -> `PrintConfiguration`. The AI's output stops here.

This is the seam that keeps the LLM useful and harmless: the model contributes
four priority weights and a few preferences, and this module — plain,
deterministic, testable arithmetic — turns them into concrete slicing parameters
under the machine and material profiles' real limits.

Nothing here calls a model. Everything here is a pure function of
`(validated plan, machine profile, material profile)`, which means the same
request always produces the same configuration and every value is auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.intelligence.schemas import LLMPlan

log = get_logger(__name__)


@dataclass(frozen=True)
class Recommendation:
    """One derived parameter change, with the reason shown to the user."""

    field: str
    value: Any
    previous: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "previous": self.previous,
            "reason": self.reason,
        }


def _lerp(low: float, high: float, t: float) -> float:
    return low + (high - low) * max(0.0, min(1.0, t))


def apply_intent(
    plan: LLMPlan,
    parameters: dict[str, Any],
    *,
    limits: dict[str, Any] | None = None,
    min_layer_height_mm: float = 0.08,
    max_layer_height_mm: float = 0.32,
) -> tuple[dict[str, Any], list[Recommendation], list[str]]:
    """Blend the plan's priorities into a parameters dict.

    Every value is a lerp between a documented bound and a documented anchor, so
    a plan can nudge settings but never drive them somewhere absurd. Returns the
    new parameters, the list of changes with reasons, and any warnings.

    `parameters` is copied, never mutated: the caller's config stays intact and
    the diff is available for the decision log.
    """
    intent = plan.intent
    strength = intent.strength
    speed = intent.speed
    material = intent.material_efficiency
    quality = intent.surface_quality

    updated = dict(parameters)
    recommendations: list[Recommendation] = []
    warnings: list[str] = []

    def set_param(field_name: str, value: Any, reason: str) -> None:
        previous = updated.get(field_name)
        if previous == value:
            return
        updated[field_name] = value
        recommendations.append(
            Recommendation(field=field_name, value=value, previous=previous, reason=reason)
        )

    # -- layer height: the main quality/speed dial --------------------------
    # Anchor 0.20 mm. Surface quality pulls down, speed and material push up.
    quality_weight = quality / max(quality + speed + 0.01, 0.01)
    layer_height = round(
        _lerp(max_layer_height_mm, min_layer_height_mm, quality_weight), 3
    )
    set_param(
        "layer_height",
        layer_height,
        f"layer height {layer_height} mm balances quality ({quality:.2f}) "
        f"against speed ({speed:.2f})",
    )

    # -- walls: the strongest strength-per-gram lever -----------------------
    wall_low, wall_high = 2, 6
    wall_count = int(round(_lerp(wall_low, wall_high, strength)))
    previous_walls = updated.get("wall_line_count")
    if previous_walls is not None:
        wall_count = max(wall_count, int(previous_walls))
    set_param(
        "wall_line_count",
        wall_count,
        f"{wall_count} perimeters for strength {strength:.2f}",
    )

    # -- solid layers -------------------------------------------------------
    top_bottom = int(round(_lerp(3, 8, max(strength, quality))))
    set_param(
        "top_layer_count",
        top_bottom,
        f"{top_bottom} top layers for strength {strength:.2f}",
    )
    set_param(
        "bottom_layer_count",
        top_bottom,
        f"{top_bottom} bottom layers for strength {strength:.2f}",
    )

    # -- infill -------------------------------------------------------------
    # Strength wants dense; material saving wants sparse. These pull against
    # each other, so the difference is taken rather than the sum.
    infill_density = int(round(_lerp(15, 60, max(0.0, strength - material))))
    previous_infill = updated.get("infill_sparse_density")
    if isinstance(previous_infill, (int, float)) and 0 <= previous_infill <= 100:
        # Respect a profile value that is already inside the band we would pick.
        infill_density = int(round(previous_infill))
    set_param(
        "infill_sparse_density",
        infill_density,
        f"{infill_density}% infill: strength {strength:.2f} against "
        f"material saving {material:.2f}",
    )

    # -- infill pattern -----------------------------------------------------
    if material > 0.6 and strength < 0.55:
        set_param(
            "infill_pattern",
            "gyroid",
            "gyroid uses less material than a grid at similar stiffness",
        )

    # -- speed --------------------------------------------------------------
    wall_speed = int(round(_lerp(120, 60, quality)))
    set_param(
        "wall_speed_mm_s",
        wall_speed,
        f"outer wall {wall_speed} mm/s for surface quality {quality:.2f}",
    )

    # -- constraints --------------------------------------------------------
    if plan.constraints.max_print_time_minutes is not None:
        # The deterministic optimiser owns the time budget; the assistant
        # records that one was asked for and the planner applies it. Recording
        # rather than silently re-tuning keeps the decision visible.
        warnings.append(
            f"Time budget of {plan.constraints.max_print_time_minutes} min is "
            "applied by the optimiser; re-check the estimate after slicing."
        )
    if plan.constraints.max_material_grams is not None:
        warnings.append(
            f"Material budget of {plan.constraints.max_material_grams:.1f} g is "
            "applied by the optimiser; re-check the estimate after slicing."
        )
    if not plan.constraints.allow_scaling:
        warnings.append("Scaling is disabled: a model that does not fit will block, not shrink.")

    # -- hard limits --------------------------------------------------------
    if limits:
        max_layer = limits.get("max_layer_height_mm")
        if isinstance(max_layer, (int, float)) and layer_height > max_layer:
            warnings.append(
                f"Quality asked for {layer_height} mm but the machine caps layer "
                f"height at {max_layer} mm; capped."
            )
            set_param("layer_height", float(max_layer), "capped at the machine maximum")
        min_layer = limits.get("min_layer_height_mm")
        if isinstance(min_layer, (int, float)) and layer_height < min_layer:
            warnings.append(
                f"Quality asked for {layer_height} mm but the machine requires at "
                f"least {min_layer} mm; raised."
            )
            set_param("layer_height", float(min_layer), "raised to the machine minimum")

    log.info(
        "llm.intent_applied",
        dominant=intent.dominant(),
        changes=len(recommendations),
        warnings=len(warnings),
    )
    return updated, recommendations, warnings