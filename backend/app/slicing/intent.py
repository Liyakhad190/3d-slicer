"""Canonical print intent mapping.

Bridges user high-level objectives (e.g., "quality", "strength", "speed",
"material", "balanced") and explicit multidimensional priorities into
concrete slicing targets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Objective = Literal["quality", "strength", "speed", "material", "balanced"]

OBJECTIVE_PRESETS: dict[Objective, dict[str, float]] = {
    "quality": {
        "surface_quality": 0.9,
        "strength": 0.5,
        "speed": 0.2,
        "material_efficiency": 0.4,
    },
    "strength": {
        "surface_quality": 0.4,
        "strength": 0.95,
        "speed": 0.3,
        "material_efficiency": 0.3,
    },
    "speed": {
        "surface_quality": 0.3,
        "strength": 0.4,
        "speed": 0.95,
        "material_efficiency": 0.5,
    },
    "material": {
        "surface_quality": 0.4,
        "strength": 0.5,
        "speed": 0.4,
        "material_efficiency": 0.9,
    },
    "balanced": {
        "surface_quality": 0.5,
        "strength": 0.5,
        "speed": 0.5,
        "material_efficiency": 0.5,
    },
}


@dataclass(frozen=True)
class PrintIntent:
    """Explicit multi-objective print intent weights (all normalized 0.0 - 1.0)."""

    surface_quality: float = 0.5
    strength: float = 0.5
    speed: float = 0.5
    material_efficiency: float = 0.5
    infill_override_percent: float | None = None
    layer_height_override_mm: float | None = None
    wall_count_override: int | None = None

    @classmethod
    def from_objective(cls, objective: Objective | str) -> PrintIntent:
        """Create PrintIntent from a named Objective preset."""
        preset = OBJECTIVE_PRESETS.get(objective, OBJECTIVE_PRESETS["balanced"])
        return cls(
            surface_quality=preset["surface_quality"],
            strength=preset["strength"],
            speed=preset["speed"],
            material_efficiency=preset["material_efficiency"],
        )

    def dominant_objective(self) -> Objective:
        """Derive the closest matching canonical objective."""
        weights = {
            "quality": self.surface_quality,
            "strength": self.strength,
            "speed": self.speed,
            "material": self.material_efficiency,
        }
        best_name = max(weights, key=lambda k: weights[k])
        best_val = weights[best_name]
        # If no single priority is strongly dominant (>= 0.6) or top two are close, it is balanced
        sorted_weights = sorted(weights.values(), reverse=True)
        if best_val < 0.6 or (sorted_weights[0] - sorted_weights[1] < 0.15):
            return "balanced"
        return best_name  # type: ignore[return-value]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
