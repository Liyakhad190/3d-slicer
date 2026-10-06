"""Bed placement: the model must touch the plate and stay inside the volume.

Placement is checked here, deterministically, against the machine's own
coordinate frame. The LLM does not calculate coordinates and its answer to
"does it fit" is never consulted: `verify_bed_placement` measures the posed
mesh against `MachineLimits.axis_bounds`, which is the same function the
G-code validator uses, so the pre-slice check and the post-slice check cannot
disagree about where the machine is.

Three separate claims, checked separately because they fail for different
reasons and need different remedies:

* **seated** — minimum Z is on the plate. No floating, nothing below the bed.
* **inside** — every axis is within the machine's legal range.
* **volume** — the model's own extents fit the build volume at this pose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import trimesh

from app.slicing.validator import MachineLimits

PLACEMENT_CHECK_VERSION = "bed-placement-1.0.0"


@dataclass(frozen=True)
class PlacementViolation:
    axis: str
    code: str
    measured_mm: float
    limit_mm: float
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "code": self.code,
            "measured_mm": round(self.measured_mm, 4),
            "limit_mm": round(self.limit_mm, 4),
            "message": self.message,
        }


@dataclass(frozen=True)
class BedPlacementResult:
    seated: bool
    inside: bool
    fits_volume: bool
    bounds: dict[str, dict[str, float]]
    build_volume_mm: dict[str, float]
    origin_convention: str
    violations: list[PlacementViolation] = field(default_factory=list)
    offset_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def passed(self) -> bool:
        return self.seated and self.inside and self.fits_volume

    @property
    def summary(self) -> str:
        if self.passed:
            return (
                f"Seated on the plate and inside the "
                f"{self.build_volume_mm['x']:.0f} x {self.build_volume_mm['y']:.0f} x "
                f"{self.build_volume_mm['z']:.0f} mm build volume."
            )
        return "; ".join(v.message for v in self.violations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_version": PLACEMENT_CHECK_VERSION,
            "seated": self.seated,
            "inside": self.inside,
            "fits_build_volume": self.fits_volume,
            "passed": self.passed,
            "bounds_mm": self.bounds,
            "build_volume_mm": self.build_volume_mm,
            "origin_convention": self.origin_convention,
            "offset_mm": [round(v, 4) for v in self.offset_mm],
            "violations": [v.to_dict() for v in self.violations],
            "summary": self.summary,
        }


def bed_centre(limits: MachineLimits) -> tuple[float, float, float]:
    """The XY midpoint of the plate in the machine's own frame.

    `corner` machines span 0..build, so the midpoint is half the build.
    `center` machines span -build/2..+build/2, so the midpoint is the origin.
    """
    if limits.origin_convention == "center":
        return (0.0, 0.0, 0.0)
    return (limits.build_x / 2.0, limits.build_y / 2.0, 0.0)


def place_for_bed(mesh: trimesh.Trimesh, limits: MachineLimits) -> trimesh.Trimesh:
    """Return a copy seated on the plate and centred on the bed."""
    placed = mesh.copy()
    centre = (placed.bounds[0] + placed.bounds[1]) / 2.0
    target = bed_centre(limits)
    placed.apply_translation(
        [target[0] - float(centre[0]), target[1] - float(centre[1]), -float(placed.bounds[0][2])]
    )
    return placed


def verify_bed_placement(mesh: trimesh.Trimesh, limits: MachineLimits) -> BedPlacementResult:
    """Measure a posed mesh against the machine's legal work volume."""
    placed = place_for_bed(mesh, limits)
    lower = [float(v) for v in placed.bounds[0]]
    upper = [float(v) for v in placed.bounds[1]]
    extents = [float(v) for v in placed.extents]

    build = {"x": limits.build_x, "y": limits.build_y, "z": limits.build_z}
    violations: list[PlacementViolation] = []

    # Seated: the bottom of the part is on the plate, within a micron.
    seated = abs(lower[2]) <= 1e-3
    if not seated:
        violations.append(
            PlacementViolation(
                axis="z",
                code="NOT_ON_BUILD_PLATE",
                measured_mm=lower[2],
                limit_mm=0.0,
                message=(
                    f"The model's lowest point is at Z={lower[2]:.3f} mm rather than on the "
                    "build plate."
                ),
            )
        )
    if lower[2] < -1e-3:
        violations.append(
            PlacementViolation(
                axis="z",
                code="BELOW_BUILD_PLATE",
                measured_mm=lower[2],
                limit_mm=0.0,
                message=f"{abs(lower[2]):.3f} mm of the model sits below the build plate.",
            )
        )

    inside = True
    for axis, index in (("x", 0), ("y", 1), ("z", 2)):
        low, high = limits.axis_bounds(axis)
        if lower[index] < low - 1e-6:
            inside = False
            violations.append(
                PlacementViolation(
                    axis=axis,
                    code="BELOW_BED_MIN",
                    measured_mm=lower[index],
                    limit_mm=low,
                    message=(
                        f"min_{axis} = {lower[index]:.3f} mm is below the bed minimum "
                        f"{low:.3f} mm."
                    ),
                )
            )
        if upper[index] > high + 1e-6:
            inside = False
            violations.append(
                PlacementViolation(
                    axis=axis,
                    code="ABOVE_BED_MAX",
                    measured_mm=upper[index],
                    limit_mm=high,
                    message=(
                        f"max_{axis} = {upper[index]:.3f} mm exceeds the bed maximum "
                        f"{high:.3f} mm."
                    ),
                )
            )

    fits = (
        extents[0] <= limits.build_x + 1e-6
        and extents[1] <= limits.build_y + 1e-6
        and extents[2] <= limits.build_z + 1e-6
    )
    if not fits:
        for axis, index, limit in (
            ("x", 0, limits.build_x),
            ("y", 1, limits.build_y),
            ("z", 2, limits.build_z),
        ):
            if extents[index] > limit + 1e-6:
                violations.append(
                    PlacementViolation(
                        axis=axis,
                        code="EXCEEDS_BUILD_VOLUME",
                        measured_mm=extents[index],
                        limit_mm=limit,
                        message=(
                            f"The model is {extents[index]:.1f} mm on {axis.upper()} but this "
                            f"machine only has {limit:.1f} mm."
                        ),
                    )
                )

    return BedPlacementResult(
        seated=seated,
        inside=inside,
        fits_volume=fits,
        bounds={
            "min": {axis: round(lower[i], 4) for i, axis in enumerate("xyz")},
            "max": {axis: round(upper[i], 4) for i, axis in enumerate("xyz")},
        },
        build_volume_mm={k: round(v, 4) for k, v in build.items()},
        origin_convention=limits.origin_convention,
        violations=violations,
    )
