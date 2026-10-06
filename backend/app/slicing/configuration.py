"""The canonical intermediate representation every adapter consumes.

`PrintConfiguration` is the boundary between the AI orchestration layer and the
slicing backends. The optimizer produces it; adapters translate it. No adapter
sees the optimizer's internals, and nothing downstream of here knows what a
Cura setting is called.

It is a plain frozen dataclass with no dependencies beyond the machine profile,
so it can be constructed in a test, serialised for the UI, and diffed between
runs without dragging in the ORM.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.errors import InsufficientInformationError
from app.slicing.validator import MachineLimits

Technology = Literal["fdm", "msla", "dlp", "sls", "slm", "unknown"]
Objective = Literal["quality", "strength", "speed", "material", "balanced"]


@dataclass(frozen=True)
class NozzleSpec:
    diameter_mm: float
    material: str = "brass"
    variant: str = "standard"

    def to_dict(self) -> dict[str, Any]:
        return {
            "diameter_mm": self.diameter_mm,
            "material": self.material,
            "variant": self.variant,
        }


@dataclass(frozen=True)
class PrintConfiguration:
    """Everything a slicer needs, in engine-neutral terms.

    `parameters` is the optimizer's output. `limits` is derived from the same
    machine profile row the validator uses, so the two cannot disagree.
    """

    technology: Technology
    parameters: dict[str, Any]
    limits: MachineLimits
    objective: Objective
    nozzle: NozzleSpec
    machine_profile_id: str
    material_profile_id: str
    machine_name: str
    material_name: str
    #: Filament diameter in mm. Carried on the configuration rather than looked
    #: up by the adapter: the slicer derives every E value from it, so an
    #: adapter that guessed would mis-scale every extrusion command.
    filament_diameter_mm: float = 1.75
    start_gcode: str | None = None
    end_gcode: str | None = None
    source_fingerprint: str = ""
    warnings: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()

    # -- derived --------------------------------------------------------

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked_reasons)

    @property
    def build_volume(self) -> tuple[float, float, float]:
        return (self.limits.build_x, self.limits.build_y, self.limits.build_z)

    def to_dict(self) -> dict[str, Any]:
        return {
            "technology": self.technology,
            "parameters": self.parameters,
            "objective": self.objective,
            "nozzle": self.nozzle.to_dict(),
            "machine_profile_id": self.machine_profile_id,
            "material_profile_id": self.material_profile_id,
            "machine_name": self.machine_name,
            "material_name": self.material_name,
            "filament_diameter_mm": self.filament_diameter_mm,
            "build_volume_mm": {
                "x": self.limits.build_x,
                "y": self.limits.build_y,
                "z": self.limits.build_z,
            },
            "source_fingerprint": self.source_fingerprint,
            "warnings": list(self.warnings),
            "blocked_reasons": list(self.blocked_reasons),
            "is_blocked": self.is_blocked,
        }

    def fingerprint(self) -> str:
        """A hash over every input, so a changed input cannot reuse a result."""
        payload = {
            "technology": self.technology,
            "parameters": self.parameters,
            "objective": self.objective,
            "nozzle": self.nozzle.to_dict(),
            "machine_profile_id": self.machine_profile_id,
            "material_profile_id": self.material_profile_id,
            "filament_diameter_mm": self.filament_diameter_mm,
            "start_gcode": self.start_gcode,
            "end_gcode": self.end_gcode,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def require_runnable(self) -> None:
        """Refuse to hand a blocked configuration to an adapter."""
        if self.is_blocked:
            raise InsufficientInformationError(
                "This configuration cannot be sliced safely: "
                + "; ".join(self.blocked_reasons),
                details={"blocked_reasons": list(self.blocked_reasons)},
            )


def canonical_parameter_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    """Translate accepted AI intent fields into operative configuration keys."""
    result = dict(overrides)
    aliases = {
        "layer_height": "layer_height_mm",
        "wall_line_count": "wall_count",
        "infill_sparse_density": "infill_density_percent",
        "wall_speed_mm_s": "outer_wall_speed_mm_s",
    }
    for source, target in aliases.items():
        if source not in result:
            continue
        value = result.pop(source)
        result.setdefault(target, value)

    shell_values = [
        result.pop(key)
        for key in ("top_layer_count", "bottom_layer_count")
        if key in result
    ]
    if shell_values:
        result.setdefault("top_bottom_layers", max(int(value) for value in shell_values))
    return result


def from_profiles(
    *,
    machine_profile: Any,
    material_profile: Any,
    nozzle_diameter_mm: float,
    nozzle_material: str,
    parameters: dict[str, Any],
    objective: Objective,
    warnings: list[str] | None = None,
    blocked_reasons: list[str] | None = None,
) -> PrintConfiguration:
    """Assemble a `PrintConfiguration` from resolved profile rows.

    Raises `InsufficientInformationError` when the machine profile has not been
    validated, because generating G-code for a machine whose limits are
    unverified is exactly the failure mode the platform exists to prevent.
    """
    from app.models.catalog import ValidationStatus
    from app.slicing.validator import MachineLimits as _Limits

    validation = getattr(machine_profile, "validation_status", None)
    status = getattr(validation, "value", validation)
    if status != ValidationStatus.validated.value:
        reason = getattr(machine_profile, "validation_reason", None) or "This machine profile has not been validated on real hardware"
        raise InsufficientInformationError(
            f"The machine profile for {machine_profile.machine.vendor} "
            f"{machine_profile.machine.model} has not been validated ({reason}), "
            "so no G-code can be generated for it. Import or validate "
            "a profile first.",
            details={
                "machine_profile_id": str(machine_profile.id),
                "validation_status": status,
                "validation_reason": reason,
            },
        )

    machine = machine_profile.machine
    material = material_profile.material

    limits = _Limits.from_profile(machine_profile, nozzle_diameter_mm=nozzle_diameter_mm)

    diameter = getattr(material_profile, "material_diameter", None)
    filament_diameter = float(diameter) if diameter else 1.75

    return PrintConfiguration(
        technology=str(machine.technology.value if hasattr(machine.technology, "value") else machine.technology),
        parameters=dict(parameters),
        limits=limits,
        objective=objective,
        nozzle=NozzleSpec(
            diameter_mm=float(nozzle_diameter_mm),
            material=nozzle_material,
        ),
        machine_profile_id=str(machine_profile.id),
        material_profile_id=str(material_profile.id),
        machine_name=f"{machine.vendor} {machine.model}",
        material_name=material.display_name,
        filament_diameter_mm=filament_diameter,
        start_gcode=machine_profile.start_gcode,
        end_gcode=machine_profile.end_gcode,
        warnings=tuple(warnings or ()),
        blocked_reasons=tuple(blocked_reasons or ()),
    )
