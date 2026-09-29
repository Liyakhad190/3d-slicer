"""Translation from our parameter set to CuraEngine settings.

Kept separate from the optimizer so the optimizer stays readable and the
engine-specific naming lives in one place. Only scalar values are emitted; the
adapter refuses anything else.

Anything unmapped is dropped rather than guessed at: an invented Cura setting
would either be ignored silently or change the print in a way nobody reviewed.
"""

from __future__ import annotations

from typing import Any

from app.slicing.validator import MachineLimits

#: Our parameter key -> CuraEngine setting key.
SETTING_MAP: dict[str, str] = {
    "layer_height_mm": "layer_height",
    "first_layer_height_mm": "layer_height_0",
    "extrusion_width_mm": "line_width",
    "wall_count": "wall_line_count",
    "infill_density_percent": "infill_sparse_density",
    "print_speed_mm_s": "speed_print",
    "travel_speed_mm_s": "speed_travel",
    "acceleration_mm_s2": "machine_acceleration",
    "nozzle_temperature_c": "material_print_temperature",
    "bed_temperature_c": "material_bed_temperature",
    "cooling_fan_percent": "cool_fan_speed",
    "adhesion_type": "adhesion_type",
    "support_enable": "support_enable",
    "support_type": "support_type",
    "support_angle_deg": "support_angle",
    "start_gcode": "machine_start_gcode",
    "end_gcode": "machine_end_gcode",
}

#: Our adhesion names -> Cura's.
_ADHESION_MAP: dict[str, str] = {
    "none": "glass",
    "brim": "brim",
    "raft": "raft",
}

_SUPPORT_TYPE_MAP: dict[str, str] = {
    "normal": "normal",
    "tree": "tree",
    "organic": "organic",
}

_INFILL_MAP: dict[str, str] = {
    "lines": "lines",
    "grid": "grid",
    "gyroid": "gyroid",
    "triangles": "triangles",
    "trihexagon": "trihexagon",
    "cubic": "cubic",
    "crosshatch": "crosshatch",
    "lightning": "lightning",
}


def to_cura_settings(
    parameters: dict[str, Any],
    *,
    limits: MachineLimits,
    start_gcode: str | None = None,
    end_gcode: str | None = None,
) -> dict[str, Any]:
    """Map resolved parameters onto CuraEngine's settings vocabulary.

    Machine limits are applied here as a final clamp, not merely trusted from
    the optimizer, so a hand-edited or AI-proposed parameter set still cannot
    push the machine past its ratings.
    """
    settings: dict[str, Any] = {}

    # `top_bottom_layers` is a count in our schema and a *thickness in mm* in
    # Cura 5.x (`top_bottom_thickness`). Sending a count there silently produced
    # a 0.3 mm top shell, so the conversion is explicit.
    top_bottom_layers = parameters.get("top_bottom_layers")
    layer_height = float(parameters.get("layer_height_mm") or 0.2)
    if top_bottom_layers is not None:
        settings["top_bottom_thickness"] = round(float(top_bottom_layers) * layer_height, 4)

    for our_key, cura_key in SETTING_MAP.items():
        if our_key not in parameters:
            continue
        value = parameters[our_key]

        if our_key == "infill_pattern":
            settings[cura_key] = _INFILL_MAP.get(str(value).lower(), "gyroid")
        elif our_key == "support_type":
            settings[cura_key] = _SUPPORT_TYPE_MAP.get(str(value).lower(), "normal")
        elif our_key == "adhesion_type":
            settings[cura_key] = _ADHESION_MAP.get(str(value).lower(), "glass")
        elif our_key == "layer_height_mm":
            settings[cura_key] = min(float(value), limits.nozzle_diameter_mm * 0.75)
        elif our_key == "print_speed_mm_s":
            settings[cura_key] = min(float(value), limits.max_print_speed_mm_s or float(value))
        elif our_key == "travel_speed_mm_s":
            settings[cura_key] = min(float(value), limits.max_travel_speed_mm_s or float(value))
        elif our_key == "nozzle_temperature_c":
            settings[cura_key] = min(float(value), limits.nozzle_max_c or float(value))
        elif our_key == "bed_temperature_c":
            settings[cura_key] = min(float(value), limits.bed_max_c or float(value))
        elif our_key == "cooling_fan_percent":
            settings[cura_key] = max(0.0, min(100.0, float(value)))
        elif our_key == "acceleration_mm_s2":
            settings[cura_key] = float(value)
        else:
            settings[cura_key] = value

    # `retraction` is a dict in our schema and two scalars in Cura's.
    retraction = parameters.get("retraction")
    if isinstance(retraction, dict):
        settings["retraction_amount"] = float(retraction.get("distance_mm", 0.8))
        settings["retraction_speed"] = float(retraction.get("speed_mm_s", 40))

    # Machine geometry. These are hard machine constraints: the build volume the
    # validator checks the toolpath against must be the same one the slicer was
    # told about, or the two disagree and the safety check is meaningless.
    settings["machine_width"] = limits.build_x
    settings["machine_depth"] = limits.build_y
    settings["machine_height"] = limits.build_z
    # The slicer is told the same coordinate frame the validator will check the
    # toolpath against, derived from one profile field so the two cannot
    # disagree about where the machine is.
    settings["machine_center_is_zero"] = limits.origin_convention == "center"
    # `mesh_position_*` is deliberately NOT set. Cura drops the model onto the
    # build plate itself; forcing mesh_position_z=0 places the mesh centred on
    # the bed, so everything below Z=0 is clipped away. An explicit position
    # silently sliced away half of a 20 mm cube.

    settings.setdefault("machine_nozzle_size", limits.nozzle_diameter_mm)
    settings.setdefault("layer_height", min(limits.nozzle_diameter_mm * 0.5, 0.28))
    settings.setdefault("line_width", limits.nozzle_diameter_mm * 1.125)
    settings["magic_spiralize"] = True  # smooth the outer wall on curved surfaces
    # RepRap (Marlin/Sprinter) is the dialect family the validator checks.
    settings["machine_gcode_flavor"] = "RepRap (Marlin/Sprinter)"

    if start_gcode:
        settings["machine_start_gcode"] = start_gcode
    if end_gcode:
        settings["machine_end_gcode"] = end_gcode

    return settings
