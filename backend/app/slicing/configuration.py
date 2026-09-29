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
    if status == ValidationStatus.unvalidated.value:
        raise InsufficientInformationError(
            f"The machine profile for {machine_profile.machine.vendor} "
            f"{machine_profile.machine.model} has not been validated on real "
            "hardware, so no G-code can be generated for it. Import or validate "
            "a profile first.",
            details={
                "machine_profile_id": str(machine_profile.id),
                "validation_status": status,
            },
        )

    machine = machine_profile.machine
    material = material_profile.material
    dialect = getattr(machine_profile, "gcode_dialect", None)

    limits = _Limits.from_profile(machine_profile, nozzle_diameter_mm=nozzle_diameter_mm)

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
        start_gcode=machine_profile.start_gcode,
        end_gcode=machine_profile.end_gcode,
        warnings=tuple(warnings or ()),
        blocked_reasons=tuple(blocked_reasons or ()),
    )
