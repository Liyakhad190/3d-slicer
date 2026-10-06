"""Deterministic machine profile validation.

A machine profile is only considered valid if all manufacturer/community
specifications are internally consistent and satisfy hardware safety invariants:
- Build volume (x, y, z) strictly positive
- FDM: valid nozzle diameters, verified temperature envelopes, positive speeds/accel
- Output formats defined
- Motion system and technology recognized

No LLM may override these checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.models.catalog import MotionSystem, Technology, ValidationStatus


@dataclass
class ProfileValidationResult:
    is_valid: bool
    status: ValidationStatus
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _get_val(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def validate_machine_profile(
    profile: Any, machine: Any = None
) -> ProfileValidationResult:
    """Deterministically validate a machine profile for internal consistency and safety."""
    # 1. Resolve machine attributes
    if machine is None:
        machine = _get_val(profile, "machine")

    vendor = _get_val(machine, "vendor") or _get_val(profile, "vendor")
    model = _get_val(machine, "model") or _get_val(profile, "model")
    technology_raw = _get_val(machine, "technology") or _get_val(profile, "technology", "fdm")
    motion_system_raw = _get_val(machine, "motion_system") or _get_val(profile, "motion_system", "cartesian")

    if not vendor or not str(vendor).strip():
        return ProfileValidationResult(
            is_valid=False,
            status=ValidationStatus.invalid,
            reason="Missing manufacturer/vendor name.",
        )

    if not model or not str(model).strip():
        return ProfileValidationResult(
            is_valid=False,
            status=ValidationStatus.invalid,
            reason="Missing machine model name.",
        )

    try:
        technology = Technology(technology_raw.value if hasattr(technology_raw, "value") else str(technology_raw).lower())
    except ValueError:
        return ProfileValidationResult(
            is_valid=False,
            status=ValidationStatus.unsupported,
            reason=f"Unsupported technology: {technology_raw}.",
        )

    try:
        _ = MotionSystem(motion_system_raw.value if hasattr(motion_system_raw, "value") else str(motion_system_raw).lower())
    except ValueError:
        return ProfileValidationResult(
            is_valid=False,
            status=ValidationStatus.invalid,
            reason=f"Unrecognized motion system: {motion_system_raw}.",
        )

    # 2. Build volume validation
    build_volume = _get_val(profile, "build_volume")
    if not isinstance(build_volume, dict):
        return ProfileValidationResult(
            is_valid=False,
            status=ValidationStatus.invalid,
            reason="Build volume must be defined as an object containing x, y, and z dimensions.",
        )

    for axis in ("x", "y", "z"):
        val = build_volume.get(axis)
        try:
            val_f = float(val)
            if val_f <= 0:
                return ProfileValidationResult(
                    is_valid=False,
                    status=ValidationStatus.invalid,
                    reason=f"Build volume dimension {axis.upper()} must be greater than zero (got {val}).",
                )
        except (TypeError, ValueError):
            return ProfileValidationResult(
                is_valid=False,
                status=ValidationStatus.invalid,
                reason=f"Build volume dimension {axis.upper()} is missing or not a valid number.",
            )

    # 3. Technology-specific validation
    if technology == Technology.fdm:
        # Nozzles
        nozzle_diameters = _get_val(profile, "nozzle_diameters")
        if not nozzle_diameters or not isinstance(nozzle_diameters, list):
            return ProfileValidationResult(
                is_valid=False,
                status=ValidationStatus.invalid,
                reason="Nozzle diameters list must contain at least one valid nozzle diameter.",
            )

        parsed_nozzles: list[float] = []
        for d in nozzle_diameters:
            try:
                df = float(d)
                if df <= 0:
                    return ProfileValidationResult(
                        is_valid=False,
                        status=ValidationStatus.invalid,
                        reason=f"Invalid nozzle diameter {d}: must be greater than zero.",
                    )
                parsed_nozzles.append(df)
            except (TypeError, ValueError):
                return ProfileValidationResult(
                    is_valid=False,
                    status=ValidationStatus.invalid,
                    reason=f"Non-numeric nozzle diameter in profile: {d}.",
                )

        # Temperature limits
        temp_limits = _get_val(profile, "temperature_limits")
        if not isinstance(temp_limits, dict) or "nozzle" not in temp_limits:
            return ProfileValidationResult(
                is_valid=False,
                status=ValidationStatus.invalid,
                reason="Missing verified nozzle temperature envelope in temperature_limits.",
            )

        nozzle_temp = temp_limits.get("nozzle", {})
        try:
            n_min = float(nozzle_temp.get("min", 0))
            n_max = float(nozzle_temp.get("max", 0))
            if n_min < 0 or n_max <= n_min or n_max > 500:
                return ProfileValidationResult(
                    is_valid=False,
                    status=ValidationStatus.invalid,
                    reason=f"Inconsistent nozzle temperature envelope: min={n_min}°C, max={n_max}°C (expected 0 <= min < max <= 500).",
                )
        except (TypeError, ValueError):
            return ProfileValidationResult(
                is_valid=False,
                status=ValidationStatus.invalid,
                reason="Nozzle temperature limits contain non-numeric values.",
            )

        heated_bed = bool(_get_val(profile, "heated_bed", True))
        if heated_bed:
            bed_temp = temp_limits.get("bed")
            if not isinstance(bed_temp, dict):
                return ProfileValidationResult(
                    is_valid=False,
                    status=ValidationStatus.invalid,
                    reason="Heated bed is enabled but bed temperature limits are missing.",
                )
            try:
                b_min = float(bed_temp.get("min", 0))
                b_max = float(bed_temp.get("max", 0))
                if b_min < 0 or b_max < b_min or b_max > 250:
                    return ProfileValidationResult(
                        is_valid=False,
                        status=ValidationStatus.invalid,
                        reason=f"Inconsistent bed temperature envelope: min={b_min}°C, max={b_max}°C.",
                    )
            except (TypeError, ValueError):
                return ProfileValidationResult(
                    is_valid=False,
                    status=ValidationStatus.invalid,
                    reason="Bed temperature limits contain non-numeric values.",
                )

        # Speed limits
        speed_limits = _get_val(profile, "speed_limits")
        if not isinstance(speed_limits, dict):
            return ProfileValidationResult(
                is_valid=False,
                status=ValidationStatus.invalid,
                reason="Missing speed limits in machine profile.",
            )

        for required_speed in ("max_print_speed_mm_s", "max_travel_speed_mm_s", "max_acceleration_mm_s2"):
            val = speed_limits.get(required_speed)
            try:
                val_f = float(val)
                if val_f <= 0:
                    return ProfileValidationResult(
                        is_valid=False,
                        status=ValidationStatus.invalid,
                        reason=f"Speed limit {required_speed} must be positive (got {val}).",
                    )
            except (TypeError, ValueError):
                return ProfileValidationResult(
                    is_valid=False,
                    status=ValidationStatus.invalid,
                    reason=f"Speed limit {required_speed} is missing or non-numeric.",
                )

        # Flow limits (optional but if specified must be >= 0)
        vol_flow = _get_val(profile, "max_volumetric_flow_mm3s")
        if vol_flow is not None:
            try:
                if float(vol_flow) < 0:
                    return ProfileValidationResult(
                        is_valid=False,
                        status=ValidationStatus.invalid,
                        reason="Maximum volumetric flow cannot be negative.",
                    )
            except (TypeError, ValueError):
                pass

        # Machine restrictions & output formats
        restrictions = _get_val(profile, "machine_restrictions") or {}
        output_formats = restrictions.get("output_formats", ["gcode"])
        if not output_formats or not isinstance(output_formats, list):
            return ProfileValidationResult(
                is_valid=False,
                status=ValidationStatus.invalid,
                reason="Machine profile must declare at least one output format.",
            )

    elif technology in (Technology.msla, Technology.dlp):
        # Resin printers don't use FDM nozzles or heated beds
        restrictions = _get_val(profile, "machine_restrictions") or {}
        output_formats = restrictions.get("output_formats", [])
        if not output_formats:
            return ProfileValidationResult(
                is_valid=False,
                status=ValidationStatus.invalid,
                reason="Resin machine profile must declare supported output formats.",
            )

    return ProfileValidationResult(
        is_valid=True,
        status=ValidationStatus.validated,
        reason=None,
        details={
            "vendor": vendor,
            "model": model,
            "technology": technology.value,
            "build_volume": build_volume,
        },
    )
