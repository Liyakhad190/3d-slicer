"""Printer capabilities projection over MachineProfile.

Surfaces what a 3D printer can physically do in structured form for the
optimizer, validator, and AI planner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.models.catalog import MachineProfile, ValidationStatus


@dataclass(frozen=True)
class PrinterCapabilities:
    """Structured, immutable projection of a machine's physical capabilities."""

    vendor: str
    model: str
    technology: str
    motion_system: str
    build_volume: tuple[float, float, float]  # (x, y, z) in mm
    heated_bed: bool
    bed_type: str | None
    chamber: dict[str, Any]
    is_enclosed: bool
    chamber_heated: bool
    chamber_max_temp_c: float | None
    nozzle_diameters: tuple[float, ...]
    extruders: int
    nozzle_min_temp_c: float
    nozzle_max_temp_c: float
    bed_min_temp_c: float
    bed_max_temp_c: float
    max_print_speed_mm_s: float
    max_travel_speed_mm_s: float
    max_acceleration_mm_s2: float
    max_volumetric_flow_mm3s: float | None
    validation_status: str
    is_validated: bool
    machine_restrictions: dict[str, Any] = field(default_factory=dict)
    kinematics: str | None = None

    @classmethod
    def from_profile(cls, profile: MachineProfile, machine: Any = None) -> PrinterCapabilities:
        """Project a MachineProfile ORM row or object into PrinterCapabilities."""
        m = machine or getattr(profile, "machine", None)
        vendor = str(getattr(m, "vendor", "") or getattr(profile, "vendor", "") or "Unknown")
        model = str(getattr(m, "model", "") or getattr(profile, "model", "") or "Unknown")
        tech_val = getattr(m, "technology", None)
        tech_str = tech_val.value if hasattr(tech_val, "value") else str(tech_val or "fdm")

        motion_val = getattr(m, "motion_system", None)
        motion_str = motion_val.value if hasattr(motion_val, "value") else str(motion_val or "cartesian")

        bv = profile.build_volume or {}
        volume = (
            float(bv.get("x", 200.0)),
            float(bv.get("y", 200.0)),
            float(bv.get("z", 200.0)),
        )

        chamber_data = dict(profile.chamber or {})
        chamber_type = str(chamber_data.get("type", "open")).lower()
        is_enclosed = chamber_type in {"enclosed", "sealed"} or bool(chamber_data.get("enclosed", False))
        chamber_heated = bool(chamber_data.get("heated", False))
        chamber_max_temp = float(chamber_data["max_temp_c"]) if chamber_data.get("max_temp_c") is not None else None

        nozzles = tuple(float(d) for d in (profile.nozzle_diameters or [0.4]))

        t_limits = profile.temperature_limits or {}
        nozzle_limits = t_limits.get("nozzle", {})
        bed_limits = t_limits.get("bed", {})
        nozzle_min = float(nozzle_limits.get("min", 0.0))
        nozzle_max = float(nozzle_limits.get("max", 260.0))
        bed_min = float(bed_limits.get("min", 0.0))
        bed_max = float(bed_limits.get("max", 100.0))

        s_limits = profile.speed_limits or {}
        max_print_speed = float(s_limits.get("max_print_speed_mm_s", 100.0))
        max_travel_speed = float(s_limits.get("max_travel_speed_mm_s", 150.0))
        max_accel = float(s_limits.get("max_acceleration_mm_s2", 1000.0))

        flow = float(profile.max_volumetric_flow_mm3s) if profile.max_volumetric_flow_mm3s is not None else None

        val_status = getattr(profile, "validation_status", ValidationStatus.unvalidated)
        status_str = val_status.value if hasattr(val_status, "value") else str(val_status)
        is_val = status_str == ValidationStatus.validated.value

        return cls(
            vendor=vendor,
            model=model,
            technology=tech_str,
            motion_system=motion_str,
            build_volume=volume,
            heated_bed=bool(profile.heated_bed),
            bed_type=profile.bed_type,
            chamber=chamber_data,
            is_enclosed=is_enclosed,
            chamber_heated=chamber_heated,
            chamber_max_temp_c=chamber_max_temp,
            nozzle_diameters=nozzles,
            extruders=int(profile.extruders or 1),
            nozzle_min_temp_c=nozzle_min,
            nozzle_max_temp_c=nozzle_max,
            bed_min_temp_c=bed_min,
            bed_max_temp_c=bed_max,
            max_print_speed_mm_s=max_print_speed,
            max_travel_speed_mm_s=max_travel_speed,
            max_acceleration_mm_s2=max_accel,
            max_volumetric_flow_mm3s=flow,
            validation_status=status_str,
            is_validated=is_val,
            machine_restrictions=dict(profile.machine_restrictions or {}),
            kinematics=motion_str,
        )

    def supports_nozzle(self, diameter_mm: float, tolerance: float = 1e-3) -> bool:
        """Whether this machine supports a specific nozzle diameter."""
        return any(abs(d - diameter_mm) < tolerance for d in self.nozzle_diameters)

    def supports_temperature(self, *, nozzle_c: float, bed_c: float = 0.0) -> bool:
        """Check whether requested printing temperatures fall safely within hardware limits."""
        if not (self.nozzle_min_temp_c <= nozzle_c <= self.nozzle_max_temp_c):
            return False
        if self.heated_bed and not (self.bed_min_temp_c <= bed_c <= self.bed_max_temp_c):
            return False
        return True

    def fits_build_volume(self, extents_xyz_mm: tuple[float, float, float]) -> bool:
        """Check if an object with dimensions (x, y, z) fits inside the build volume."""
        return (
            extents_xyz_mm[0] <= self.build_volume[0] + 1e-6
            and extents_xyz_mm[1] <= self.build_volume[1] + 1e-6
            and extents_xyz_mm[2] <= self.build_volume[2] + 1e-6
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "model": self.model,
            "technology": self.technology,
            "motion_system": self.motion_system,
            "build_volume_mm": {
                "x": self.build_volume[0],
                "y": self.build_volume[1],
                "z": self.build_volume[2],
            },
            "heated_bed": self.heated_bed,
            "bed_type": self.bed_type,
            "chamber": self.chamber,
            "is_enclosed": self.is_enclosed,
            "chamber_heated": self.chamber_heated,
            "chamber_max_temp_c": self.chamber_max_temp_c,
            "nozzle_diameters": list(self.nozzle_diameters),
            "extruders": self.extruders,
            "temperature_limits": {
                "nozzle": {"min": self.nozzle_min_temp_c, "max": self.nozzle_max_temp_c},
                "bed": {"min": self.bed_min_temp_c, "max": self.bed_max_temp_c},
            },
            "speed_limits": {
                "max_print_speed_mm_s": self.max_print_speed_mm_s,
                "max_travel_speed_mm_s": self.max_travel_speed_mm_s,
                "max_acceleration_mm_s2": self.max_acceleration_mm_s2,
            },
            "max_volumetric_flow_mm3s": self.max_volumetric_flow_mm3s,
            "validation_status": self.validation_status,
            "is_validated": self.is_validated,
            "machine_restrictions": self.machine_restrictions,
        }
