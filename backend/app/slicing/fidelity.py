"""Print fidelity: REQUESTED vs GENERATED vs MEASURED.

The project's fidelity gap had a cause that was invisible from the outside:
CuraEngine exposes both *primitive* settings (which it reads) and *derived*
settings that only the GUI computes (which it silently ignores). Sending the
derived ones produced valid G-code that did not do what was asked.

This module is the regression net for that class of defect. For each
calibration model it slices with a known configuration, measures the result
from the G-code, and reports three separate claims:

* `requested`  — what `PrintConfiguration` asked for.
* `generated`  — what the engine's settings actually contained, after the
  adapter's translation and the engine's own schema filtering.
* `measured`   — what the G-code can be shown to deposit.

Nothing here claims physical accuracy. Every report carries
`physical_validation_required: true` because a toolpath is not a part.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.slicing.measurement import MeasurementReport, measure_gcode
from app.slicing.validator import MachineLimits

log = get_logger(__name__)

FIDELITY_REPORT_VERSION = "fidelity-report-1.0.0"


@dataclass
class Claim:
    """One parameter, traced through all three stages.

    `verified` means "the emitted G-code demonstrably does this". It is `None`
    for any claim that G-code cannot settle, which is a distinct answer from
    "false" — a falsified claim is a defect, an unverifiable one is a limit of
    the method.
    """

    requested: Any
    generated: Any = None
    measured: Any = None
    verified: bool | None = False
    method: str = ""
    note: str = ""
    requires_physical_validation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ModelFidelityResult:
    model: str
    description: str
    claims: dict[str, Claim] = field(default_factory=dict)
    measurement: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        """True when every claim G-code *can* settle was settled correctly.

        A claim marked `verified=None` is excluded: it is a limit of the
        method, not a failure.
        """
        decidable = [c for c in self.claims.values() if c.verified is not None]
        return bool(decidable) and all(bool(c.verified) for c in decidable) and not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "description": self.description,
            "verified": self.verified,
            "claims": {name: claim.to_dict() for name, claim in self.claims.items()},
            "measurement": self.measurement,
            "issues": self.issues,
        }


@dataclass
class FidelityReport:
    engine: str
    engine_version: str
    engine_license: str
    results: list[ModelFidelityResult] = field(default_factory=list)
    operative_settings: dict[str, str] = field(default_factory=dict)
    inert_settings: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return bool(self.results) and all(r.verified for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": FIDELITY_REPORT_VERSION,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "engine_license": self.engine_license,
            "license_review_required": True,
            "physical_validation_required": True,
            "physical_validation_note": (
                "Every 'measured' value is derived from generated G-code. It "
                "establishes what the toolpath does, not what the part will be. "
                "Dimensional accuracy, adhesion, flow and material behaviour "
                "require a printed coupon and a measuring instrument."
            ),
            "verified": self.verified,
            "operative_settings": self.operative_settings,
            "inert_settings": self.inert_settings,
            "models": [r.to_dict() for r in self.results],
            "summary": {
                "models_tested": len(self.results),
                "models_verified": sum(1 for r in self.results if r.verified),
                "claims_failed": sorted(
                    f"{r.model}:{name}"
                    for r in self.results
                    for name, c in r.claims.items()
                    if c.verified is False
                ),
                "claims_needing_physical_validation": sorted(
                    f"{r.model}:{name}"
                    for r in self.results
                    for name, c in r.claims.items()
                    if c.verified is None
                ),
            },
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


#: Verified by slicing with each varied and measuring the deposited geometry.
#: Anything not listed here is either unmapped or measured to have no effect.
OPERATIVE_SETTINGS: dict[str, str] = {
    "wall_count": "wall_line_count",
    "top_bottom_layers": "top_layers + bottom_layers",
    "infill_density_percent": "infill_line_distance (derived from density)",
    "infill_pattern": "infill_pattern",
    "extrusion_width_mm": "line_width",
    "layer_height_mm": "layer_height",
    "first_layer_height_mm": "layer_height_0",
    "material_diameter": "material_diameter",
}

#: Measured to be accepted and ignored by the headless engine. Listed so a
#: future change that starts honouring one of them is noticed.
INERT_SETTINGS: list[str] = [
    "infill_sparse_density",
    "infill_sparse_pattern",
    "infill_sparse_thickness",
    "top_bottom_thickness",
    "wall_thickness",
    "skin_outline_count",
    "bottom_layers (in Cura 5.13; top_layers governs both shells)",
]


def measure_calibration_model(
    gcode: str,
    limits: MachineLimits,
    *,
    model: str,
    description: str,
    requested: dict[str, Any],
    generated: dict[str, Any],
    line_width_mm: float,
    layer_height_mm: float,
    expected: dict[str, Any] | None = None,
) -> ModelFidelityResult:
    """Measure one calibration slice and compare against what was asked for."""
    report: MeasurementReport = measure_gcode(
        gcode, limits, line_width_mm=line_width_mm, layer_height_mm=layer_height_mm
    )
    result = ModelFidelityResult(model=model, description=description)
    expected = expected or {}

    # --- layer count ------------------------------------------------------
    requested_layers = requested.get("expected_layer_count")
    if requested_layers is not None:
        result.claims["layer_count"] = Claim(
            requested=requested_layers,
            generated=requested.get("layer_height_mm"),
            measured=report.layer_count,
            verified=abs(report.layer_count - int(requested_layers)) <= 1,
            method="count of ;LAYER: markers, or Z-change inference without them",
        )

    # --- layer height ------------------------------------------------------
    if layer_height_mm:
        heights = [layer.z_mm for layer in report.layers if layer.z_mm > 0]
        if heights:
            step = _median_step(heights)
            if step is not None:
                result.claims["layer_height"] = Claim(
                    requested=layer_height_mm,
                    generated=generated.get("layer_height"),
                    measured=round(step, 4),
                    verified=abs(step - layer_height_mm) <= max(0.01, layer_height_mm * 0.05),
                    method="median difference between consecutive layer Z values",
                )

    # --- XY extent ----------------------------------------------------------
    bounds = report.bounds
    expected_size = expected.get("size_mm")
    if expected_size is not None and bounds["max"][0] > bounds["min"][0]:
        # The larger of the two axes: a part's characteristic width is whichever
        # is bigger, and a rotated model's X extent is not its nominal length.
        size_x = round(
            max(bounds["max"][0] - bounds["min"][0], bounds["max"][1] - bounds["min"][1]),
            3,
        )
        # The first layer carries a brim or skirt, so the toolpath is wider
        # than the part by a known allowance.
        allowance = float(expected.get("bed_adhesion_allowance_mm", 0.0))
        result.claims["part_width"] = Claim(
            requested=expected_size,
            generated=None,
            measured=size_x,
            verified=abs(size_x - allowance - expected_size) <= max(
                0.6, expected_size * 0.05
            ),
            method="larger of the X and Y toolpath extents, minus the measured adhesion allowance",
            note=(
                f"Adhesion features add up to {allowance} mm of overhang beyond the part."
                if allowance
                else ""
            ),
        )

    # --- deposited volume ---------------------------------------------------
    # Deliberately NOT a verification claim. Swept volume (path length x line
    # width x layer height) ignores line overlap, the gaps between infill lines
    # and every thermal effect, so a mismatch cannot be attributed without a
    # printed coupon. Claiming it verified would be the exact "G-code proves
    # physical accuracy" error this project refuses to make.
    solid_volume = expected.get("solid_volume_mm3")
    if solid_volume:
        fill = expected.get("expected_fill_fraction")
        target = solid_volume * float(fill or 1.0)
        measured = report.total_deposited_volume_mm3
        result.claims["deposited_volume_estimate"] = Claim(
            requested=round(target, 2),
            generated=None,
            measured=round(measured, 2),
            verified=None,
            method=(
                "total extruded path length x line width x layer height, accumulated "
                "over every extruding move in the file"
            ),
            note=(
                "An estimate, not a verification. It ignores line overlap, the gaps "
                "between infill lines and every thermal effect, so it neither "
                "confirms nor contradicts a real part without measuring one."
            ),
            requires_physical_validation=True,
        )

    result.measurement = {
        "layer_count": report.layer_count,
        "used_layer_markers": report.used_layer_markers,
        "bounds": report.bounds,
        "extruded_length_mm": round(report.total_extruded_length_mm, 3),
        "travel_length_mm": round(report.total_travel_length_mm, 3),
        "deposited_volume_mm3": round(report.total_deposited_volume_mm3, 3),
        "median_contour_count": report.median_contour_count(),
        "retraction_count": report.retraction_count,
        "estimated_time_s": round(report.estimated_time_s(), 2),
        "nozzle_temp_range_c": list(report.nozzle_temp_range) if report.nozzle_temp_range else None,
        "bed_temp_range_c": list(report.bed_temp_range) if report.bed_temp_range else None,
    }
    return result


def _median_step(values: list[float]) -> float | None:
    """Median gap between consecutive sorted values, ignoring the first."""
    if len(values) < 3:
        return None
    ordered = sorted(values)
    gaps = [b - a for a, b in zip(ordered, ordered[1:]) if b - a > 1e-6]
    if not gaps:
        return None
    gaps.sort()
    mid = len(gaps) // 2
    if len(gaps) % 2 == 1:
        return gaps[mid]
    return (gaps[mid - 1] + gaps[mid]) / 2.0
