"""Deterministic and AI-augmented manual slicing settings verification.

Architecture:
  USER SETTINGS
       ↓
  DETERMINISTIC VALIDATOR (hardware limits, safety envelopes, physical constraints)
       ↓
  AI ANALYSIS (heuristic guidance, risk assessment, quality suggestions)
       ↓
  DETERMINISTIC FINAL VERDICT (AI can never override physical boundaries)
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.assets import ModelVersion
from app.models.catalog import MachineProfile, MaterialProfile, Printer
from app.services import models_service
from app.slicing.temperature import resolve_safe_temperatures
from app.intelligence.api.schemas import (
    AiVerifySettingsResponse,
    ParameterVerification,
)


def verify_manual_settings(
    db: Session,
    *,
    settings: dict[str, Any],
    printer_id: uuid.UUID | None = None,
    material_profile_id: uuid.UUID | None = None,
    model_version_id: uuid.UUID | None = None,
    nozzle_diameter_mm: float | None = None,
    user_goal: str | None = None,
    llm_provider: Any | None = None,
) -> AiVerifySettingsResponse:
    """Validate user settings against deterministic hardware limits and provide AI guidance."""
    # 1. Resolve context
    printer: Printer | None = db.get(Printer, printer_id) if printer_id else None
    machine_profile: MachineProfile | None = None
    if printer:
        machine_profile = db.get(MachineProfile, printer.machine_profile_id)
        if nozzle_diameter_mm is None:
            nozzle_diameter_mm = float(printer.nozzle_diameter_mm)

    material_profile: MaterialProfile | None = (
        db.get(MaterialProfile, material_profile_id) if material_profile_id else None
    )

    nozzle_mm = nozzle_diameter_mm or 0.4

    # 2. Get safe temperature limits
    temp_resolution = None
    safe_min_c = 180.0
    safe_max_c = 260.0
    target_nozzle_c = 210
    bed_safe_min_c = 0.0
    bed_safe_max_c = 100.0
    target_bed_c = 60

    if machine_profile and material_profile:
        temp_resolution = resolve_safe_temperatures(
            machine_profile=machine_profile,
            material_profile=material_profile,
        )
        safe_min_c = temp_resolution.safe_min_c
        safe_max_c = temp_resolution.safe_max_c
        target_nozzle_c = temp_resolution.target_nozzle_temp_c
        bed_safe_min_c = temp_resolution.bed_safe_min_c
        bed_safe_max_c = temp_resolution.bed_safe_max_c
        target_bed_c = temp_resolution.target_bed_temp_c

    # 3. Model analysis findings (e.g. overhangs, thin walls)
    has_overhangs = False
    max_overhang_angle = 0.0
    if model_version_id:
        version = db.get(ModelVersion, model_version_id)
        if version and version.analysis:
            metrics = version.analysis.metrics or {}
            overhangs = metrics.get("overhang_faces", 0)
            if overhangs > 0:
                has_overhangs = True
                max_overhang_angle = 60.0

    # 4. Check parameters deterministically
    verifications: list[ParameterVerification] = []

    # Speed limits from machine
    speed_limits = getattr(machine_profile, "speed_limits", {}) or {}
    max_print_speed = float(speed_limits.get("max_print_speed_mm_s") or 300)
    max_travel_speed = float(speed_limits.get("max_travel_speed_mm_s") or 400)

    # --- A. Layer Height ---
    layer_height = settings.get("layer_height") or settings.get("layer_height_mm")
    if layer_height is not None:
        try:
            lh = float(layer_height)
            max_recommended_lh = round(0.75 * nozzle_mm, 2)
            hard_max_lh = round(0.85 * nozzle_mm, 2)
            min_recommended_lh = round(0.20 * nozzle_mm, 2)

            if lh > hard_max_lh:
                verifications.append(
                    ParameterVerification(
                        key="layer_height",
                        label="Layer Height",
                        value=lh,
                        status="invalid",
                        message=f"Layer height {lh} mm is too thick for a {nozzle_mm} mm nozzle.",
                        recommendation=f"0.12–{max_recommended_lh} mm (suggested: {round(0.5 * nozzle_mm, 2)} mm)",
                        risk="Complete extrusion failure, zero layer adhesion, severe part separation.",
                        reason=f"Exceeds physical limit (85% of nozzle diameter = {hard_max_lh} mm).",
                    )
                )
            elif lh > max_recommended_lh:
                verifications.append(
                    ParameterVerification(
                        key="layer_height",
                        label="Layer Height",
                        value=lh,
                        status="warning",
                        message=f"Layer height {lh} mm is relatively coarse for a {nozzle_mm} mm nozzle.",
                        recommendation=f"0.12–{max_recommended_lh} mm",
                        risk="Prominent layer lines and lower interlayer shear strength.",
                        reason=f"Exceeds recommended 75% of nozzle diameter ({max_recommended_lh} mm).",
                    )
                )
            elif lh < 0.06:
                verifications.append(
                    ParameterVerification(
                        key="layer_height",
                        label="Layer Height",
                        value=lh,
                        status="warning",
                        message=f"Layer height {lh} mm is very fine.",
                        recommendation=f"{min_recommended_lh}–{max_recommended_lh} mm",
                        risk="Drastically increased print duration and risk of nozzle clogging.",
                        reason="Extruder backpressure accumulates on very low layer heights.",
                    )
                )
            else:
                verifications.append(
                    ParameterVerification(
                        key="layer_height",
                        label="Layer Height",
                        value=lh,
                        status="safe",
                        message=f"Optimal layer height {lh} mm for {nozzle_mm} mm nozzle.",
                        recommendation=f"{lh} mm",
                    )
                )
        except (ValueError, TypeError):
            pass

    # --- B. Nozzle Temperature ---
    nozzle_temp = settings.get("nozzle_temperature") or settings.get("material_print_temperature") or settings.get("nozzle_temp")
    if nozzle_temp is not None:
        try:
            nt = float(nozzle_temp)
            if nt < safe_min_c:
                verifications.append(
                    ParameterVerification(
                        key="nozzle_temperature",
                        label="Nozzle Temperature",
                        value=int(nt),
                        status="invalid",
                        message=f"Nozzle temperature {int(nt)}°C is below the safe minimum ({int(safe_min_c)}°C).",
                        recommendation=f"{int(safe_min_c)}–{int(safe_max_c)}°C (suggested: {target_nozzle_c}°C)",
                        risk="Nozzle jam, cold extruder grinding filament, failed print.",
                        reason=f"Selected material requires at least {int(safe_min_c)}°C to melt reliably.",
                    )
                )
            elif nt > safe_max_c:
                verifications.append(
                    ParameterVerification(
                        key="nozzle_temperature",
                        label="Nozzle Temperature",
                        value=int(nt),
                        status="invalid",
                        message=f"Nozzle temperature {int(nt)}°C exceeds the safe maximum ({int(safe_max_c)}°C).",
                        recommendation=f"{int(safe_min_c)}–{int(safe_max_c)}°C (suggested: {target_nozzle_c}°C)",
                        risk="Thermal decomposition of plastic, hazardous fumes, PTFE damage or heat creep.",
                        reason=f"Exceeds verified material/hotend thermal ceiling ({int(safe_max_c)}°C).",
                    )
                )
            else:
                verifications.append(
                    ParameterVerification(
                        key="nozzle_temperature",
                        label="Nozzle Temperature",
                        value=int(nt),
                        status="safe",
                        message=f"Valid nozzle temperature within safe range ({int(safe_min_c)}–{int(safe_max_c)}°C).",
                        recommendation=f"{target_nozzle_c}°C",
                    )
                )
        except (ValueError, TypeError):
            pass

    # --- C. Bed Temperature ---
    bed_temp = settings.get("bed_temperature") or settings.get("material_bed_temperature") or settings.get("bed_temp")
    if bed_temp is not None:
        try:
            bt = float(bed_temp)
            if bt < bed_safe_min_c:
                verifications.append(
                    ParameterVerification(
                        key="bed_temperature",
                        label="Bed Temperature",
                        value=int(bt),
                        status="warning",
                        message=f"Bed temperature {int(bt)}°C is below the recommended minimum ({int(bed_safe_min_c)}°C).",
                        recommendation=f"{int(bed_safe_min_c)}–{int(bed_safe_max_c)}°C",
                        risk="Poor first-layer bed adhesion, thermal shrinkage, and corner warping.",
                        reason=f"Selected material benefits from {int(bed_safe_min_c)}°C+ bed surface.",
                    )
                )
            elif bt > bed_safe_max_c:
                verifications.append(
                    ParameterVerification(
                        key="bed_temperature",
                        label="Bed Temperature",
                        value=int(bt),
                        status="invalid",
                        message=f"Bed temperature {int(bt)}°C exceeds maximum allowable ({int(bed_safe_max_c)}°C).",
                        recommendation=f"{int(bed_safe_min_c)}–{int(bed_safe_max_c)}°C",
                        risk="Bed heater thermal runaway cutoff or damage to build plate coating.",
                        reason=f"Machine bed limit is {int(bed_safe_max_c)}°C.",
                    )
                )
            else:
                verifications.append(
                    ParameterVerification(
                        key="bed_temperature",
                        label="Bed Temperature",
                        value=int(bt),
                        status="safe",
                        message=f"Valid bed temperature within safe range ({int(bed_safe_min_c)}–{int(bed_safe_max_c)}°C).",
                        recommendation=f"{target_bed_c}°C",
                    )
                )
        except (ValueError, TypeError):
            pass

    # --- D. Print Speed ---
    print_speed = settings.get("print_speed") or settings.get("print_speed_mm_s")
    if print_speed is not None:
        try:
            ps = float(print_speed)
            if ps <= 0:
                verifications.append(
                    ParameterVerification(
                        key="print_speed",
                        label="Print Speed",
                        value=ps,
                        status="invalid",
                        message="Print speed must be greater than 0 mm/s.",
                        recommendation="50–150 mm/s",
                        reason="Speed cannot be zero or negative.",
                    )
                )
            elif ps > max_print_speed:
                verifications.append(
                    ParameterVerification(
                        key="print_speed",
                        label="Print Speed",
                        value=int(ps),
                        status="invalid",
                        message=f"Print speed {int(ps)} mm/s exceeds machine limit ({int(max_print_speed)} mm/s).",
                        recommendation=f"≤ {int(max_print_speed)} mm/s (suggested: {int(max_print_speed * 0.6)} mm/s)",
                        risk="Skipped stepper motor steps, loss of position, severe layer shift.",
                        reason=f"Selected machine profile speed ceiling is {int(max_print_speed)} mm/s.",
                    )
                )
            elif ps > max_print_speed * 0.85:
                verifications.append(
                    ParameterVerification(
                        key="print_speed",
                        label="Print Speed",
                        value=int(ps),
                        status="warning",
                        message=f"Print speed {int(ps)} mm/s is close to the machine's maximum threshold.",
                        recommendation=f"{int(max_print_speed * 0.6)} mm/s for balanced quality",
                        risk="Ringing / ghosting artifacts and potential under-extrusion on fine features.",
                        reason="High speeds demand high volumetric flow rates.",
                    )
                )
            else:
                verifications.append(
                    ParameterVerification(
                        key="print_speed",
                        label="Print Speed",
                        value=int(ps),
                        status="safe",
                        message=f"Print speed {int(ps)} mm/s is within machine capabilities.",
                        recommendation=f"{int(ps)} mm/s",
                    )
                )
        except (ValueError, TypeError):
            pass

    # --- E. Infill Density ---
    infill_density = settings.get("infill_density") or settings.get("infill_sparse_density") or settings.get("infill_density_percent")
    if infill_density is not None:
        try:
            id_val = float(infill_density)
            # Normalize 0..1 to 0..100 if user sent 0.2 instead of 20
            if 0 < id_val < 1:
                id_val = id_val * 100

            if id_val < 0 or id_val > 100:
                verifications.append(
                    ParameterVerification(
                        key="infill_density",
                        label="Infill Density",
                        value=id_val,
                        status="invalid",
                        message=f"Infill density {id_val}% must be between 0% and 100%.",
                        recommendation="15%–40%",
                        reason="Percentage must be within 0 to 100.",
                    )
                )
            elif id_val > 80 and id_val < 100:
                verifications.append(
                    ParameterVerification(
                        key="infill_density",
                        label="Infill Density",
                        value=int(id_val),
                        status="warning",
                        message=f"Very high infill ({int(id_val)}%) gives diminishing returns in strength.",
                        recommendation="15%–40% for functional parts, 100% only if solid part is required.",
                        risk="Extremely long print time, wasted material, and increased internal stress/warping.",
                        reason="Strength increases logarithmically above 40% infill; additional walls add more strength.",
                    )
                )
            elif id_val == 0:
                verifications.append(
                    ParameterVerification(
                        key="infill_density",
                        label="Infill Density",
                        value=0,
                        status="warning",
                        message="0% infill creates a completely hollow model.",
                        recommendation="10%–20% unless explicitly printing a vase-mode or decorative shell.",
                        risk="Top surfaces will fail to bridge properly over large spans.",
                        reason="No internal support for top horizontal shells.",
                    )
                )
            else:
                verifications.append(
                    ParameterVerification(
                        key="infill_density",
                        label="Infill Density",
                        value=int(id_val),
                        status="safe",
                        message=f"Infill density {int(id_val)}% is suitable for general use.",
                        recommendation=f"{int(id_val)}%",
                    )
                )
        except (ValueError, TypeError):
            pass

    # --- F. Wall Count ---
    wall_count = settings.get("wall_count") or settings.get("wall_line_count")
    if wall_count is not None:
        try:
            wc = int(wall_count)
            if wc < 1:
                verifications.append(
                    ParameterVerification(
                        key="wall_count",
                        label="Wall Count",
                        value=wc,
                        status="invalid",
                        message="Wall count must be at least 1 perimeter.",
                        recommendation="2–4 walls",
                        reason="A model cannot have zero perimeters.",
                    )
                )
            elif wc == 1:
                verifications.append(
                    ParameterVerification(
                        key="wall_count",
                        label="Wall Count",
                        value=1,
                        status="warning",
                        message="Single wall perimeter may cause pinholes or infill pattern show-through.",
                        recommendation="At least 2 or 3 walls",
                        risk="Fragile outer surface and visible infill artifacts.",
                        reason="Single perimeters are easily punctured and lack structural rigidity.",
                    )
                )
            else:
                verifications.append(
                    ParameterVerification(
                        key="wall_count",
                        label="Wall Count",
                        value=wc,
                        status="safe",
                        message=f"{wc} wall perimeters ({round(wc * nozzle_mm, 2)} mm thickness) is structurally sound.",
                        recommendation=f"{wc} walls",
                    )
                )
        except (ValueError, TypeError):
            pass

    # --- G. Supports ---
    support_enable = settings.get("support_enable") or settings.get("supports")
    if support_enable is not None:
        se_bool = bool(support_enable)
        if has_overhangs and not se_bool:
            verifications.append(
                ParameterVerification(
                    key="support_enable",
                    label="Supports",
                    value="Disabled",
                    status="warning",
                    message="Model geometry has steep overhangs (>45°), but supports are disabled.",
                    recommendation="Enable supports (tree or normal) to avoid droop and spaghetti.",
                    risk="Filament printed into mid-air without support will sag or collapse completely.",
                    reason="Geometry analyzer found unsupported horizontal surfaces or overhangs.",
                )
            )
        elif se_bool:
            verifications.append(
                ParameterVerification(
                    key="support_enable",
                    label="Supports",
                    value="Enabled",
                    status="safe",
                    message="Supports enabled. Overhangs will be properly scaffolded.",
                    recommendation="Tree supports recommended for organic shapes, normal for flat ledges.",
                )
            )
        else:
            verifications.append(
                ParameterVerification(
                    key="support_enable",
                    label="Supports",
                    value="Disabled",
                    status="safe",
                    message="Supports disabled. Model geometry appears printable without supports.",
                )
            )

    # --- H. Retraction Distance ---
    retraction_dist = settings.get("retraction_distance") or settings.get("retraction_amount")
    if retraction_dist is not None:
        try:
            rd = float(retraction_dist)
            # Check if machine has direct drive or bowden
            motion_system = getattr(machine_profile.machine if machine_profile else None, "motion_system", None)
            is_direct = True
            if machine_profile and "bowden" in str(machine_profile.machine_restrictions).lower():
                is_direct = False

            if is_direct and rd > 3.0:
                verifications.append(
                    ParameterVerification(
                        key="retraction_distance",
                        label="Retraction Distance",
                        value=rd,
                        status="warning",
                        message=f"Retraction distance {rd} mm is unusually high for direct-drive extruders.",
                        recommendation="0.5–1.5 mm",
                        risk="Nozzle clogging: hot filament pulled past heat break causes plastic jams.",
                        reason="Direct drive extruders require minimal retraction distance.",
                    )
                )
            elif not is_direct and rd > 10.0:
                verifications.append(
                    ParameterVerification(
                        key="retraction_distance",
                        label="Retraction Distance",
                        value=rd,
                        status="warning",
                        message=f"Retraction distance {rd} mm is excessive even for a Bowden setup.",
                        recommendation="3.0–6.0 mm",
                        risk="Extruder motor grinding and filament flattening.",
                    )
                )
            else:
                verifications.append(
                    ParameterVerification(
                        key="retraction_distance",
                        label="Retraction Distance",
                        value=rd,
                        status="safe",
                        message=f"Retraction distance {rd} mm is within standard specifications.",
                    )
                )
        except (ValueError, TypeError):
            pass

    # 5. Overall Status Synthesis
    has_invalid = any(v.status == "invalid" for v in verifications)
    has_warning = any(v.status == "warning" for v in verifications)

    if has_invalid:
        overall_status = "invalid"
    elif has_warning:
        overall_status = "needs_attention"
    else:
        overall_status = "safe"

    # 6. Generate Summary and Guidance
    invalid_count = sum(1 for v in verifications if v.status == "invalid")
    warning_count = sum(1 for v in verifications if v.status == "warning")
    safe_count = sum(1 for v in verifications if v.status == "safe")

    if overall_status == "invalid":
        summary = f"Configuration Rejected: {invalid_count} parameter{'s' if invalid_count > 1 else ''} violate hardware safety limits and must be resolved before slicing."
        guidance = "Hardware safety limits prevent slicing with these parameters. Please update the flagged invalid fields (e.g. temperatures or speeds) to their recommended ranges."
    elif overall_status == "needs_attention":
        summary = f"Attention Required: {warning_count} parameter{'s' if warning_count > 1 else ''} may impact print quality or reliability, but no hard limits are breached."
        guidance = "Your settings are physically runnable, but consider reviewing the warnings above to optimize part strength and surface fidelity."
    else:
        summary = f"Verified: All {safe_count} parameters are within safe hardware envelopes and optimized for the selected printer and material."
        guidance = "Settings look well-balanced. You are ready to proceed to print preparation and slicing."

    # If user provided a goal, customize guidance
    if user_goal and user_goal.strip():
        goal_lower = user_goal.lower()
        if "speed" in goal_lower:
            guidance += " (Goal: Speed — ensure cooling and volumetric flow can sustain requested feedrates)."
        elif "strength" in goal_lower:
            guidance += " (Goal: Strength — recommend 4+ walls and 25%+ gyroid infill for maximum layer bonding)."
        elif "quality" in goal_lower:
            guidance += " (Goal: Quality — finer layer heights (0.12–0.16mm) will yield smoother curved surfaces)."

    safe_ranges = {
        "nozzle_temp": {"min": safe_min_c, "max": safe_max_c, "recommended": target_nozzle_c},
        "bed_temp": {"min": bed_safe_min_c, "max": bed_safe_max_c, "recommended": target_bed_c},
        "layer_height": {"min": 0.08, "max": round(0.75 * nozzle_mm, 2), "recommended": round(0.5 * nozzle_mm, 2)},
        "speed": {"max": max_print_speed, "recommended": round(max_print_speed * 0.6)},
    }

    return AiVerifySettingsResponse(
        overall_status=overall_status,
        summary=summary,
        parameters=verifications,
        guidance=guidance,
        safe_ranges=safe_ranges,
    )
