"""Deterministic safe temperature resolution and validation.

Safety hierarchy:
1. Hard printer/hotend safety limits
2. Material hard temperature limits
3. Slicer/machine constraints
4. Verified calibration
5. Manufacturer recommended range
6. AI recommendation
7. User preference

Calculation:
SAFE_MIN = max(printer_min, material_min, slicer_min)
SAFE_MAX = min(printer_max, material_max, slicer_max)

If SAFE_MIN > SAFE_MAX:
    TEMPERATURE_RANGE_CONFLICT is returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.errors import ValidationFailedError


@dataclass
class TemperatureResolution:
    safe_min_c: float
    safe_max_c: float
    target_nozzle_temp_c: int
    first_layer_nozzle_temp_c: int
    target_bed_temp_c: int
    first_layer_bed_temp_c: int
    provenance: str
    reason: str
    printer_min_c: float
    printer_max_c: float
    material_min_c: float
    material_max_c: float
    bed_safe_min_c: float
    bed_safe_max_c: float
    is_conflict: bool = False
    conflict_code: str | None = None
    conflict_message: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe_min_c": self.safe_min_c,
            "safe_max_c": self.safe_max_c,
            "target_nozzle_temp_c": self.target_nozzle_temp_c,
            "first_layer_nozzle_temp_c": self.first_layer_nozzle_temp_c,
            "target_bed_temp_c": self.target_bed_temp_c,
            "first_layer_bed_temp_c": self.first_layer_bed_temp_c,
            "provenance": self.provenance,
            "reason": self.reason,
            "printer_min_c": self.printer_min_c,
            "printer_max_c": self.printer_max_c,
            "material_min_c": self.material_min_c,
            "material_max_c": self.material_max_c,
            "bed_safe_min_c": self.bed_safe_min_c,
            "bed_safe_max_c": self.bed_safe_max_c,
            "is_conflict": self.is_conflict,
            "conflict_code": self.conflict_code,
            "conflict_message": self.conflict_message,
            "warnings": self.warnings,
        }


def resolve_safe_temperatures(
    *,
    machine_profile: Any,
    material_profile: Any,
    user_override_nozzle: float | int | None = None,
    user_override_bed: float | int | None = None,
    ai_recommended_nozzle: float | int | None = None,
    calibrated_nozzle: float | int | None = None,
    calibration_provenance: str | None = None,
) -> TemperatureResolution:
    """Deterministically resolve nozzle and bed temperatures following safety hierarchy."""
    # 1. Printer limits
    temp_limits = getattr(machine_profile, "temperature_limits", {}) or {}
    nozzle_limits = temp_limits.get("nozzle", {}) or {}
    bed_limits = temp_limits.get("bed", {}) or {}

    printer_min = float(nozzle_limits.get("min", 0) or 0)
    printer_max = float(nozzle_limits.get("max", 0) or 300)

    # 2. Material limits
    mat_nozzle = getattr(material_profile, "nozzle_temp", {}) or {}
    mat_min = float(mat_nozzle.get("hard_min") or mat_nozzle.get("min") or 180)
    mat_max = float(mat_nozzle.get("hard_max") or mat_nozzle.get("max") or 260)
    mat_typical = float(mat_nozzle.get("typical") or mat_nozzle.get("recommended") or 210)

    # 3. Slicer / machine constraints (e.g. firmware max safe limit)
    slicer_min = 0.0
    slicer_max = float("inf")

    # If dialect has firmware max temp quirks (e.g. Marlin safety margin: maxtemp - 15)
    dialect = getattr(machine_profile, "gcode_dialect", None)
    if dialect:
        quirks = getattr(dialect, "quirks", {}) or {}
        firmware_max = quirks.get("maxtemp_nozzle_c")
        if firmware_max:
            # Firmware triggers shutdown if this limit is reached, so keep margin
            slicer_max = min(slicer_max, float(firmware_max) - 15.0)

    # Calculate SAFE_MIN and SAFE_MAX
    safe_min = max(printer_min, mat_min, slicer_min)
    safe_max = min(printer_max, mat_max, slicer_max)

    # Bed calculations
    printer_bed_min = float(bed_limits.get("min", 0) or 0)
    printer_bed_max = float(bed_limits.get("max", 0) or 120)

    mat_bed = getattr(material_profile, "bed_temp", {}) or {}
    mat_bed_min = float(mat_bed.get("min", 0) or 0)
    mat_bed_max = float(mat_bed.get("max", 0) or 120)
    mat_bed_typical = float(mat_bed.get("typical") or mat_bed.get("recommended") or 60)

    bed_safe_min = max(printer_bed_min, mat_bed_min)
    bed_safe_max = min(printer_bed_max, mat_bed_max)

    warnings: list[str] = []

    # Check for temperature range conflict
    if safe_min > safe_max:
        return TemperatureResolution(
            safe_min_c=safe_min,
            safe_max_c=safe_max,
            target_nozzle_temp_c=0,
            first_layer_nozzle_temp_c=0,
            target_bed_temp_c=0,
            first_layer_bed_temp_c=0,
            provenance="conflict",
            reason=(
                f"Printer supports {printer_min:.0f}–{printer_max:.0f} °C, but material requires "
                f"{mat_min:.0f}–{mat_max:.0f} °C (safe overlap: None)."
            ),
            printer_min_c=printer_min,
            printer_max_c=printer_max,
            material_min_c=mat_min,
            material_max_c=mat_max,
            bed_safe_min_c=bed_safe_min,
            bed_safe_max_c=bed_safe_max,
            is_conflict=True,
            conflict_code="TEMPERATURE_RANGE_CONFLICT",
            conflict_message=(
                f"The selected printer and material do not have a validated overlapping "
                f"nozzle-temperature range (Printer: {printer_min:.0f}–{printer_max:.0f} °C, "
                f"Material: {mat_min:.0f}–{mat_max:.0f} °C)."
            ),
        )

    # Hierarchy resolution:
    target_temp: int
    provenance: str
    reason: str

    if user_override_nozzle is not None:
        user_val = float(user_override_nozzle)
        if user_val < safe_min or user_val > safe_max:
            # User override exceeds safe limits: cannot accept unsafe override
            return TemperatureResolution(
                safe_min_c=safe_min,
                safe_max_c=safe_max,
                target_nozzle_temp_c=int(user_val),
                first_layer_nozzle_temp_c=int(user_val),
                target_bed_temp_c=int(mat_bed_typical),
                first_layer_bed_temp_c=int(mat_bed_typical),
                provenance="user_conflict",
                reason=f"User requested {user_val:.0f} °C, outside safe range [{safe_min:.0f}, {safe_max:.0f}] °C.",
                printer_min_c=printer_min,
                printer_max_c=printer_max,
                material_min_c=mat_min,
                material_max_c=mat_max,
                bed_safe_min_c=bed_safe_min,
                bed_safe_max_c=bed_safe_max,
                is_conflict=True,
                conflict_code="TEMPERATURE_RANGE_CONFLICT",
                conflict_message=(
                    f"Requested temperature {user_val:.0f} °C is outside the safe range "
                    f"[{safe_min:.0f}, {safe_max:.0f}] °C."
                ),
            )
        target_temp = int(round(user_val))
        provenance = "user_preference"
        reason = f"User locked nozzle temperature at {target_temp} °C (within safe range [{safe_min:.0f}, {safe_max:.0f}] °C)."

    elif calibrated_nozzle is not None:
        calib_val = float(calibrated_nozzle)
        if safe_min <= calib_val <= safe_max:
            target_temp = int(round(calib_val))
            provenance = f"calibrated:{calibration_provenance or 'verified'}"
            reason = f"Verified calibration specifies {target_temp} °C."
        else:
            # Calibrated value outside current printer/material safety envelope: clamp and warn
            target_temp = int(round(max(safe_min, min(calib_val, safe_max))))
            provenance = "calibrated_clamped"
            reason = f"Calibration ({calib_val:.0f} °C) clamped into safe range [{safe_min:.0f}, {safe_max:.0f}] °C."
            warnings.append(reason)

    elif ai_recommended_nozzle is not None:
        ai_val = float(ai_recommended_nozzle)
        if safe_min <= ai_val <= safe_max:
            target_temp = int(round(ai_val))
            provenance = "ai_recommendation"
            reason = f"AI suggestion of {target_temp} °C validated within safe range [{safe_min:.0f}, {safe_max:.0f}] °C."
        else:
            # AI recommendation outside safe range: NEVER use unvalidated guess, fallback to manufacturer recommended
            target_temp = int(round(max(safe_min, min(mat_typical, safe_max))))
            provenance = "material_recommended"
            reason = (
                f"AI suggestion {ai_val:.0f} °C was outside safe range [{safe_min:.0f}, {safe_max:.0f}] °C; "
                f"safely defaulted to manufacturer recommendation {target_temp} °C."
            )
            warnings.append(reason)

    else:
        # Default: Manufacturer recommendation clamped to safe range
        target_temp = int(round(max(safe_min, min(mat_typical, safe_max))))
        provenance = "material_recommended"
        reason = f"Material typical value {target_temp} °C within safe limits [{safe_min:.0f}, {safe_max:.0f}] °C."

    # First layer temperature
    first_layer_temp = int(min(safe_max, target_temp + 5))

    # Bed resolution
    if user_override_bed is not None:
        target_bed = int(round(float(user_override_bed)))
    else:
        target_bed = int(round(max(bed_safe_min, min(mat_bed_typical, bed_safe_max))))
    first_layer_bed = target_bed

    return TemperatureResolution(
        safe_min_c=safe_min,
        safe_max_c=safe_max,
        target_nozzle_temp_c=target_temp,
        first_layer_nozzle_temp_c=first_layer_temp,
        target_bed_temp_c=target_bed,
        first_layer_bed_temp_c=first_layer_bed,
        provenance=provenance,
        reason=reason,
        printer_min_c=printer_min,
        printer_max_c=printer_max,
        material_min_c=mat_min,
        material_max_c=mat_max,
        bed_safe_min_c=bed_safe_min,
        bed_safe_max_c=bed_safe_max,
        is_conflict=False,
        warnings=warnings,
    )
