"""Translation from our parameter set to CuraEngine settings.

Kept separate from the optimizer so the optimizer stays readable and the
engine-specific naming lives in exactly one place.

**Which Cura settings are actually operative**

Every name in `OPERATIVE_SETTINGS` below was verified by slicing a calibration
model with the setting varied and measuring the deposited geometry from the
resulting G-code (`app/slicing/measurement.py`). The distinction matters more
than it looks, because Cura exposes two families of settings:

* **Primitive settings** the engine reads directly. These work.
* **Derived settings** the *GUI* computes for the user. The headless engine
  ignores them completely — it accepts the value, reports no error, and prints
  the default.

The first version of this file used the derived names. It produced valid,
plausible G-code that silently ignored the requested infill density, pattern
and top/bottom thickness. Nothing in the file said so, because from the
outside the settings had been applied.

| Our parameter | Wrong (derived, inert) | Correct (primitive, verified) |
| --- | --- | --- |
| `infill_density_percent` | `infill_sparse_density` | `infill_line_distance` (mm) |
| `infill_pattern` | `infill_sparse_pattern` | `infill_pattern` |
| `top_bottom_layers` | `top_bottom_thickness` | `top_layers` (+ `bottom_layers`) |
| extrusion width for infill | `infill_sparse_thickness` | `infill_line_width` |

`bottom_layers` is additionally inert in Cura 5.13 — `top_layers` governs both
shells. Both are emitted so the behaviour is correct here and stays correct on a
build where they are independent.

Anything unmapped is still dropped rather than guessed at, and `material_diameter`
is always set explicitly: the schema's placeholder of 2.85 mm would make every
E value in the file wrong for real 1.75 mm filament.
"""

from __future__ import annotations

from typing import Any

from app.slicing.validator import MachineLimits

#: Our parameter key -> CuraEngine setting key, for primitive settings only.
SETTING_MAP: dict[str, str] = {
    "layer_height_mm": "layer_height",
    "first_layer_height_mm": "layer_height_0",
    "extrusion_width_mm": "line_width",
    "wall_count": "wall_line_count",
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
    # --- override keys the print plan resolves to directly -------------------
    # Every one of these was confirmed present in fdmprinter.def.json (Cura
    # 5.13) before it was mapped; unknown keys are dropped by the adapter, so
    # a guessed name would silently do nothing.
    "first_layer_speed_mm_s": "speed_layer_0",
    "first_layer_temperature_c": "material_print_temperature_layer_0",
    "first_layer_bed_temperature_c": "material_bed_temperature_layer_0",
    "first_layer_fan_percent": "cool_fan_speed_0",
    "outer_wall_speed_mm_s": "speed_wall_0",
    "inner_wall_speed_mm_s": "speed_wall_x",
    "infill_speed_mm_s": "speed_infill",
    "support_speed_mm_s": "speed_support",
    "wall_acceleration_mm_s2": "acceleration_wall",
    "travel_acceleration_mm_s2": "acceleration_travel",
    "brim_width_mm": "brim_width",
    "min_layer_time_s": "cool_min_layer_time",
    "z_hop_mm": "retraction_hop",
    "support_density_percent": "support_infill_rate",
    "support_pattern": "support_pattern",
    "support_interface_enable": "support_interface_enable",
    "support_interface_height_mm": "support_interface_height",
    "support_interface_density_percent": "support_interface_density",
    "top_layers": "top_layers",
    "bottom_layers": "bottom_layers",
}

#: Override keys whose value is bounded by a machine rating rather than by the
#: setting's own range. Only *new* keys are listed: the clamps that already
#: existed are left exactly as they were, because existing behaviour is
#: covered by the test suite.
_SPEED_OVERRIDE_KEYS = (
    "first_layer_speed_mm_s",
    "outer_wall_speed_mm_s",
    "inner_wall_speed_mm_s",
    "infill_speed_mm_s",
    "support_speed_mm_s",
)
_ACCEL_OVERRIDE_KEYS = (
    "wall_acceleration_mm_s2",
    "travel_acceleration_mm_s2",
)
_PERCENT_OVERRIDE_KEYS = (
    "first_layer_fan_percent",
    "support_density_percent",
    "support_interface_density_percent",
)

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

#: Filament diameters the platform will accept. 2.85 mm is Cura's schema
#: placeholder, not a real filament; silently emitting it would mis-scale every
#: extrusion command.
SUPPORTED_FILAMENT_DIAMETERS_MM: frozenset[float] = frozenset(
    {1.75, 2.85, 3.00}
)
DEFAULT_FILAMENT_DIAMETER_MM = 1.75

#: Above this line distance the engine effectively stops laying infill. Used for
#: a 0% request, which has no finite distance.
_NO_INFILL_LINE_DISTANCE_MM = 100.0


def infill_line_distance_mm(
    density_percent: float, line_width_mm: float
) -> float:
    """Convert an infill density percentage to a line distance in mm.

    `density = line_width / line_distance`, verified against the schema
    defaults (0.4 mm lines at 20% gives the 2.0 mm default distance).

    A 0% request has no finite distance, so the sentinel above is used; 100%
    is 0, which the engine reads as solid.
    """
    density = max(0.0, min(100.0, float(density_percent)))
    if density <= 0.0:
        return _NO_INFILL_LINE_DISTANCE_MM
    if density >= 100.0:
        return 0.0
    return round(float(line_width_mm) * 100.0 / density, 4)


def to_cura_settings(
    parameters: dict[str, Any],
    *,
    limits: MachineLimits,
    start_gcode: str | None = None,
    end_gcode: str | None = None,
    filament_diameter_mm: float = DEFAULT_FILAMENT_DIAMETER_MM,
) -> dict[str, Any]:
    """Map resolved parameters onto CuraEngine's operative settings.

    Machine limits are applied here as a final clamp, not merely trusted from
    the optimizer, so a hand-edited or AI-proposed parameter set still cannot
    push the machine past its ratings.
    """
    settings: dict[str, Any] = {}

    line_width = float(
        parameters.get("extrusion_width_mm") or limits.nozzle_diameter_mm * 1.125
    )
    layer_height = float(parameters.get("layer_height_mm") or 0.2)

    # --- infill, via the primitive controls --------------------------------
    infill_pattern = parameters.get("infill_pattern")
    if infill_pattern is not None:
        settings["infill_pattern"] = _INFILL_MAP.get(
            str(infill_pattern).lower(), "gyroid"
        )
    settings["infill_line_width"] = round(line_width, 4)

    infill_density = parameters.get("infill_density_percent")
    if infill_density is not None:
        settings["infill_line_distance"] = infill_line_distance_mm(
            infill_density, line_width
        )

    # --- top/bottom shell, via the layer count ------------------------------
    top_bottom_layers = parameters.get("top_bottom_layers")
    if top_bottom_layers is not None:
        count = max(0, int(top_bottom_layers))
        # `top_layers` governs both shells in Cura 5.13; `bottom_layers` is
        # emitted too so the intent is explicit and stays correct if a future
        # build separates them.
        settings["top_layers"] = count
        settings["bottom_layers"] = count

    # --- direct mappings ----------------------------------------------------
    for our_key, cura_key in SETTING_MAP.items():
        if our_key not in parameters:
            continue
        value = parameters[our_key]

        if our_key == "support_type":
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
        elif our_key in _SPEED_OVERRIDE_KEYS:
            settings[cura_key] = min(
                float(value), limits.max_print_speed_mm_s or float(value)
            )
        elif our_key in _ACCEL_OVERRIDE_KEYS:
            settings[cura_key] = min(
                float(value), limits.max_acceleration_mm_s2 or float(value)
            )
        elif our_key in _PERCENT_OVERRIDE_KEYS:
            settings[cura_key] = max(0.0, min(100.0, float(value)))
        elif our_key == "first_layer_temperature_c":
            settings[cura_key] = min(float(value), limits.nozzle_max_c or float(value))
        elif our_key == "first_layer_bed_temperature_c":
            settings[cura_key] = min(float(value), limits.bed_max_c or float(value))
        elif our_key == "brim_width_mm":
            settings[cura_key] = max(0.0, float(value))
        elif our_key == "min_layer_time_s":
            settings[cura_key] = max(0.0, float(value))
        elif our_key == "support_interface_height_mm":
            settings[cura_key] = max(0.0, float(value))
        elif our_key == "z_hop_mm":
            hop = max(0.0, float(value))
            settings[cura_key] = hop
            # Cura ignores the distance unless the feature is switched on; a
            # plan that asks for a hop without the flag would change nothing.
            settings["retraction_hop_enabled"] = hop > 0.0
        else:
            settings[cura_key] = value

    # `first_layer_line_width_mm` is a distance, Cura's control is a factor of
    # the overall line width. Dividing here keeps the conversion next to the
    # mapping instead of leaking a Cura unit into the plan vocabulary.
    first_layer_line_width = parameters.get("first_layer_line_width_mm")
    if first_layer_line_width is not None and line_width > 0:
        settings["initial_layer_line_width_factor"] = round(
            max(0.0, float(first_layer_line_width)) * 100.0 / line_width, 3
        )

    # `retraction` is a dict in our schema and two scalars in Cura's.
    retraction = parameters.get("retraction")
    if isinstance(retraction, dict):
        settings["retraction_amount"] = float(retraction.get("distance_mm", 0.8))
        settings["retraction_speed"] = float(retraction.get("speed_mm_s", 40))

    # --- machine geometry ---------------------------------------------------
    settings["machine_width"] = limits.build_x
    settings["machine_depth"] = limits.build_y
    settings["machine_height"] = limits.build_z
    settings["machine_center_is_zero"] = limits.origin_convention == "center"
    # `mesh_position_*` is deliberately NOT set: Cura drops the model onto the
    # plate itself, and forcing mesh_position_z=0 clips everything below Z=0.

    # --- filament -----------------------------------------------------------
    # Always set. The schema default of 2.85 mm is a placeholder, and E values
    # are computed from it: leaving it in place mis-scales every extrusion.
    diameter = float(filament_diameter_mm or DEFAULT_FILAMENT_DIAMETER_MM)
    if diameter not in SUPPORTED_FILAMENT_DIAMETERS_MM:
        raise ValueError(
            f"Unsupported filament diameter {diameter} mm. "
            f"Known: {sorted(SUPPORTED_FILAMENT_DIAMETERS_MM)}"
        )
    settings["material_diameter"] = diameter

    # --- values every profile needs ------------------------------------------
    settings.setdefault("machine_nozzle_size", limits.nozzle_diameter_mm)
    settings.setdefault("layer_height", min(limits.nozzle_diameter_mm * 0.5, 0.28))
    settings.setdefault("line_width", line_width)
    settings["magic_spiralize"] = True
    settings["machine_gcode_flavor"] = "RepRap (Marlin/Sprinter)"

    if start_gcode:
        settings["machine_start_gcode"] = start_gcode
    if end_gcode:
        settings["machine_end_gcode"] = end_gcode

    return settings
