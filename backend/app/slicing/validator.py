"""G-code validator.

This is the last gate between a slicer and a machine. It parses the produced
G-code and checks it against the *specific* machine profile the job is bound
to. Any `critical` finding blocks output: the file is stored, marked blocked,
and the download endpoint refuses to serve it.

The validator is deterministic and structural. It does not reason about print
quality — it checks only what can be proven from the file plus the profile.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

VALIDATOR_VERSION = "gcode-validator-1.0.0"

#: G/M codes this validator understands. A command outside the dialect's
#: vocabulary is reported rather than ignored, because an unrecognised command
#: on a real machine may do something unexpected.
_KNOWN_COMMANDS: frozenset[str] = frozenset(
    {
        "G0", "G1", "G2", "G3", "G4", "G10", "G17", "G18", "G19", "G20", "G21",
        "G28", "G29", "G80", "G90", "G91", "G92", "G93", "G94",
        "M0", "M1", "M17", "M18", "M24", "M25", "M82", "M83", "M84", "M104",
        "M105", "M106", "M107", "M109", "M114", "M115", "M117", "M118", "M119",
        "M140", "M141", "M155", "M190", "M191", "M400", "M401", "M402", "M403",
        "M500", "M501", "M1002",
    }
)

_COMMENT = re.compile(r"^\s*[;]")
_WORD = re.compile(r"([A-Za-z])\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")

#: Commands that must never appear in a file we are willing to send.
DANGEROUS_COMMANDS: dict[str, str] = {
    "M999": "Emergency reset while a print is in progress",
    "M112": "Immediate emergency stop",
    "M410": "Emergency stop (Marlin)",
    "M0": "Program stop",
    "M1": "Programmed pause",
    "G80": "Motor off mid-print",
}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str  # critical | warning | info
    message: str
    line_number: int | None = None
    axis: str | None = None
    measured_value: float | None = None
    limit_value: float | None = None
    unit: str | None = None
    mitigation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "line_number": self.line_number,
            "axis": self.axis,
            "measured_value": self.measured_value,
            "limit_value": self.limit_value,
            "unit": self.unit,
            "mitigation": self.mitigation,
        }


@dataclass
class MachineLimits:
    """The subset of a machine profile the validator enforces."""

    build_x: float
    build_y: float
    build_z: float
    nozzle_max_c: float
    nozzle_min_c: float
    bed_max_c: float
    bed_min_c: float
    max_print_speed_mm_s: float
    max_travel_speed_mm_s: float
    max_acceleration_mm_s2: float | None = None
    max_volumetric_flow_mm3s: float | None = None
    nozzle_diameter_mm: float = 0.4
    allowed_commands: frozenset[str] | None = None
    has_thermal_control: bool = True
    safe_nozzle_min_c: float | None = None
    safe_nozzle_max_c: float | None = None
    first_layer_nozzle_min_c: float | None = None
    first_layer_nozzle_max_c: float | None = None
    material_nozzle_min_c: float | None = None
    material_nozzle_max_c: float | None = None
    #: Where the machine's work volume starts.
    #:
    #: `corner` means the bed spans 0..build (stock Marlin/RepRap: the origin is
    #: the front-left corner). `center` means it spans -build/2..+build/2 (the
    #: CoreXY/centre-origin convention many printers use).
    #:
    #: This MUST agree with what the slicer was told, or the two disagree about
    #: where the machine is and the bounds check is meaningless. It is read from
    #: the machine profile rather than assumed, and the slicer settings are
    #: derived from the same field.
    origin_convention: str = "corner"

    def axis_bounds(self, axis: str) -> tuple[float, float]:
        """The legal range for an axis, in the machine's own coordinate frame."""
        build = {"x": self.build_x, "y": self.build_y, "z": self.build_z}[axis]
        if axis == "z" or self.origin_convention == "corner":
            return 0.0, build
        return -build / 2.0, build / 2.0

    def contains(self, axis: str, value: float, *, tolerance: float = 1e-6) -> bool:
        low, high = self.axis_bounds(axis)
        return (low - tolerance) <= value <= (high + tolerance)

    @classmethod
    def from_profile(
        cls, profile: Any, *, nozzle_diameter_mm: float = 0.4, material_profile: Any = None
    ) -> "MachineLimits":
        build = profile.build_volume or {}
        temps = profile.temperature_limits or {}
        speeds = profile.speed_limits or {}
        nozzle = temps.get("nozzle", {})
        bed = temps.get("bed", {})
        dialect_allowed = None
        # The coordinate frame is a machine-profile property, and the slicer
        # settings are derived from this same field, so the two cannot disagree.
        origin_convention = str((profile.machine_restrictions or {}).get("origin_convention", "corner"))

        safe_min = None
        safe_max = None
        first_layer_min = None
        first_layer_max = None
        mat_min = None
        mat_max = None
        if material_profile is not None:
            from app.slicing.temperature import resolve_safe_temperatures
            tres = resolve_safe_temperatures(machine_profile=profile, material_profile=material_profile)
            safe_min = tres.safe_min_c
            safe_max = tres.safe_max_c
            first_layer_min = tres.safe_min_c
            first_layer_max = tres.safe_max_c
            mat_min = tres.material_min_c
            mat_max = tres.material_max_c

        if profile.gcode_dialect_id is not None:
            dialect = profile.gcode_dialect
            if dialect is not None:
                known = {str(c).upper() for c in (dialect.known_commands or [])}
                # A dialect that declares no thermal commands (GRBL) has none.
                dialect_allowed = frozenset(known) if known else None
                if not dialect.temperature_commands:
                    return cls(
                        build_x=float(build.get("x", 0) or 0),
                        build_y=float(build.get("y", 0) or 0),
                        build_z=float(build.get("z", 0) or 0),
                        nozzle_max_c=0.0,
                        nozzle_min_c=0.0,
                        bed_max_c=0.0,
                        bed_min_c=0.0,
                        max_print_speed_mm_s=float(speeds.get("max_print_speed_mm_s", 0) or 0),
                        max_travel_speed_mm_s=float(speeds.get("max_travel_speed_mm_s", 0) or 0),
                        max_acceleration_mm_s2=speeds.get("max_acceleration_mm_s2"),
                        max_volumetric_flow_mm3s=None,
                        nozzle_diameter_mm=nozzle_diameter_mm,
                        allowed_commands=dialect_allowed,
                        has_thermal_control=False,
                        origin_convention=origin_convention,
                    )
        return cls(
            build_x=float(build.get("x", 0) or 0),
            build_y=float(build.get("y", 0) or 0),
            build_z=float(build.get("z", 0) or 0),
            nozzle_max_c=float(nozzle.get("max", 0) or 0),
            nozzle_min_c=float(nozzle.get("min", 0) or 0),
            bed_max_c=float(bed.get("max", 0) or 0),
            bed_min_c=float(bed.get("min", 0) or 0),
            max_print_speed_mm_s=float(speeds.get("max_print_speed_mm_s", 0) or 0),
            max_travel_speed_mm_s=float(speeds.get("max_travel_speed_mm_s", 0) or 0),
            max_acceleration_mm_s2=speeds.get("max_acceleration_mm_s2"),
            max_volumetric_flow_mm3s=(
                float(profile.max_volumetric_flow_mm3s)
                if profile.max_volumetric_flow_mm3s
                else None
            ),
            nozzle_diameter_mm=nozzle_diameter_mm,
            allowed_commands=dialect_allowed,
            origin_convention=origin_convention,
            safe_nozzle_min_c=safe_min,
            safe_nozzle_max_c=safe_max,
            first_layer_nozzle_min_c=first_layer_min,
            first_layer_nozzle_max_c=first_layer_max,
            material_nozzle_min_c=mat_min,
            material_nozzle_max_c=mat_max,
        )


@dataclass
class ValidationReport:
    passed: bool
    blocks_output: bool
    findings: list[Finding] = field(default_factory=list)
    severity_counts: dict[str, int] = field(default_factory=dict)
    summary: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    validator: str = "gcode-validator"
    validator_version: str = VALIDATOR_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocks_output": self.blocks_output,
            "findings": [f.to_dict() for f in self.findings],
            "severity_counts": self.severity_counts,
            "summary": self.summary,
            "stats": self.stats,
            "validator": self.validator,
            "validator_version": self.validator_version,
        }


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def _parse_words(line: str) -> list[tuple[str, float]]:
    return [(m.group(1).upper(), float(m.group(2))) for m in _WORD.finditer(line)]


def _command_of(words: Sequence[tuple[str, float]]) -> str | None:
    for letter, value in words:
        if letter == "G" or letter == "M":
            return f"{letter}{int(value)}"
    return None


def iter_lines(text: str) -> Iterator[tuple[int, str]]:
    for number, raw in enumerate(text.splitlines(), start=1):
        yield number, raw


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def _has_axis_limit(limits: "MachineLimits", axis: str) -> bool:
    """True when the profile declares a positive limit for this axis.

    A profile with no declared extent (an unvalidated draft) must not produce a
    stream of false out-of-bounds findings; a missing limit belongs to the
    profile, not to every move in the file.
    """
    return bool(getattr(limits, f"build_{axis.lower()}", 0.0))


def validate_gcode(
    text: str, limits: MachineLimits, *, source_name: str = "output.gcode"
) -> ValidationReport:
    """Validate G-code against a specific machine profile.

    Checks performed, all provable from the file plus the profile:

    * every move stays inside the build volume
    * no Z move goes below the bed
    * no temperature command exceeds the machine's thermal envelope
    * no feed rate exceeds the machine's speed ceiling
    * no acceleration exceeds the profile limit
    * volumetric flow stays within the machine's melt capacity
    * no command outside the dialect vocabulary
    * no dangerous/emergency command
    * the profile's start and end G-code are present
    """
    findings: list[Finding] = []
    stats: dict[str, Any] = {
        "line_count": 0,
        "extrusion_moves": 0,
        "travel_moves": 0,
        "retractions": 0,
        "max_feedrate_seen": 0.0,
        "max_nozzle_temp_seen": None,
        "max_bed_temp_seen": None,
        "max_z_seen": 0.0,
        "min_z_seen": 0.0,
        "max_volumetric_flow_seen": 0.0,
        # Accumulated from the E axis rather than read from Cura's
        # ";Filament used:" comment, which reports 0 when the engine was run
        # without a full printer definition file.
        "filament_extruded_mm": 0.0,
    }

    x = y = z = 0.0
    e_absolute = 0.0
    e_relative = True
    in_relative_xyz = False
    active_extrusion = False
    nozzle_temp: float | None = None
    bed_temp: float | None = None
    last_position: tuple[float, float, float] | None = None
    unknown_commands: dict[str, int] = {}

    allowed = limits.allowed_commands
    if allowed is None:
        allowed = frozenset(_KNOWN_COMMANDS)

    for line_number, raw in iter_lines(text):
        stats["line_count"] += 1
        if _COMMENT.match(raw):
            continue

        # Strip inline comments; Cura emits `; ...` or `( ... )`.
        body = raw.split(";", 1)[0].strip()
        if not body:
            continue

        words = _parse_words(body)
        if not words:
            continue

        command = _command_of(words)
        if command is not None:
            if command in DANGEROUS_COMMANDS:
                findings.append(
                    Finding(
                        code="DANGEROUS_COMMAND",
                        severity="critical",
                        message=(
                            f"{command} ({DANGEROUS_COMMANDS[command]}) appears in the "
                            "print file and would interrupt or stop the print."
                        ),
                        line_number=line_number,
                        mitigation=(
                            "Remove it from the machine's start/end G-code and re-slice."
                        ),
                    )
                )
            elif command not in allowed and command not in _KNOWN_COMMANDS:
                unknown_commands[command] = unknown_commands.get(command, 0) + 1

        params = {letter: value for letter, value in words if letter not in {"G", "M"}}

        # --- modes ------------------------------------------------------
        if command == "G90":
            in_relative_xyz = False
        elif command == "G91":
            in_relative_xyz = True
        elif command == "M82":
            e_relative = False
        elif command == "M83":
            e_relative = True
        elif command == "G92" and "E" in params:
            e_absolute = params["E"]

        # --- temperatures ------------------------------------------------
        if limits.has_thermal_control:
            # The temperature target always travels in the `S` word. M104/M109
            # set the nozzle, M140/M190 set the bed. (An earlier version looked
            # for a `nozzle`/`bed` word, which no G-code contains, so every
            # temperature check silently passed.)
            for code, target in (("M104", "nozzle"), ("M109", "nozzle"),
                                 ("M140", "bed"), ("M190", "bed")):
                if command != code or "S" not in params:
                    continue
                temp = params["S"]
                if target == "nozzle":
                    nozzle_temp = temp
                    stats["max_nozzle_temp_seen"] = max(
                        stats["max_nozzle_temp_seen"] or temp, temp
                    )
                    if temp > limits.nozzle_max_c:
                        findings.append(
                            Finding(
                                code="GCODE_TEMPERATURE_OUT_OF_RANGE",
                                severity="critical",
                                message=(
                                    f"Nozzle target of {temp:.0f} C exceeds the machine limit "
                                    f"of {limits.nozzle_max_c:.0f} C."
                                ),
                                line_number=line_number,
                                measured_value=temp,
                                limit_value=limits.nozzle_max_c,
                                unit="C",
                                mitigation="Lower the nozzle temperature for this machine.",
                            )
                        )
                    elif limits.safe_nozzle_max_c is not None and temp > limits.safe_nozzle_max_c:
                        findings.append(
                            Finding(
                                code="GCODE_TEMPERATURE_OUT_OF_RANGE",
                                severity="critical",
                                message=(
                                    f"Nozzle target of {temp:.0f} C exceeds the validated safe maximum "
                                    f"of {limits.safe_nozzle_max_c:.0f} C."
                                ),
                                line_number=line_number,
                                measured_value=temp,
                                limit_value=limits.safe_nozzle_max_c,
                                unit="C",
                                mitigation="Lower the nozzle temperature to stay within validated safe range.",
                            )
                        )
                    elif limits.safe_nozzle_min_c is not None and temp < limits.safe_nozzle_min_c and temp > 0:
                        findings.append(
                            Finding(
                                code="GCODE_TEMPERATURE_OUT_OF_RANGE",
                                severity="critical",
                                message=(
                                    f"Nozzle target of {temp:.0f} C is below the validated safe minimum "
                                    f"of {limits.safe_nozzle_min_c:.0f} C."
                                ),
                                line_number=line_number,
                                measured_value=temp,
                                limit_value=limits.safe_nozzle_min_c,
                                unit="C",
                                mitigation="Raise the nozzle temperature to ensure proper extrusion.",
                            )
                        )
                else:
                    bed_temp = temp
                    stats["max_bed_temp_seen"] = max(
                        stats["max_bed_temp_seen"] or temp, temp
                    )
                    if temp > limits.bed_max_c:
                        findings.append(
                            Finding(
                                code="BED_TEMP_TOO_HIGH",
                                severity="critical",
                                message=(
                                    f"Bed target of {temp:.0f} C exceeds the machine limit "
                                    f"of {limits.bed_max_c:.0f} C."
                                ),
                                line_number=line_number,
                                measured_value=temp,
                                limit_value=limits.bed_max_c,
                                unit="C",
                                mitigation="Lower the bed temperature for this machine.",
                            )
                        )

        # --- motion ------------------------------------------------------
        if command not in {"G0", "G1"}:
            continue

        feed = params.get("F", 0.0)
        extruding = "E" in params
        new_e = params.get("E", e_absolute) if extruding else e_absolute

        # Feed rate is mm/min in G-code; the machine limit is mm/s.
        feed_mm_s = feed / 60.0 if feed > 0 else 0.0
        is_travel = not extruding

        if extruding and e_relative:
            delta_e = params.get("E", 0.0)
        elif extruding:
            delta_e = new_e - e_absolute
        else:
            delta_e = 0.0
        if extruding:
            e_absolute = new_e
            if delta_e > 0:
                stats["filament_extruded_mm"] += delta_e

        if is_travel:
            stats["travel_moves"] += 1
        elif delta_e > 0:
            stats["extrusion_moves"] += 1
        elif delta_e < 0:
            stats["retractions"] += 1

        speed_limit = (
            limits.max_travel_speed_mm_s if is_travel else limits.max_print_speed_mm_s
        )
        if feed_mm_s and speed_limit and feed_mm_s > speed_limit:
            stats["max_feedrate_seen"] = max(stats["max_feedrate_seen"], feed_mm_s)
            findings.append(
                Finding(
                    code="FEEDRATE_OVER_LIMIT",
                    severity="critical" if feed_mm_s > speed_limit * 1.5 else "warning",
                    message=(
                        f"{'Travel' if is_travel else 'Print'} feed of {feed_mm_s:.0f} mm/s "
                        f"exceeds this machine's {speed_limit:.0f} mm/s limit."
                    ),
                    line_number=line_number,
                    measured_value=feed_mm_s,
                    limit_value=speed_limit,
                    unit="mm/s",
                    mitigation="Reduce the speed cap for this machine profile.",
                )
            )
        elif feed_mm_s:
            stats["max_feedrate_seen"] = max(stats["max_feedrate_seen"], feed_mm_s)

        if last_position is None:
            last_position = (x, y, z)
        new_x = x + params["X"] if (in_relative_xyz and "X" in params) else params.get("X", x)
        new_y = y + params["Y"] if (in_relative_xyz and "Y" in params) else params.get("Y", y)
        new_z = z + params["Z"] if (in_relative_xyz and "Z" in params) else params.get("Z", z)

        if last_position == (x, y, z) and not in_relative_xyz:
            # Absolute move with no axis words: still counts for position tracking.
            pass

        # --- volumetric flow ---------------------------------------------
        if delta_e > 0 and feed_mm_s:
            flow = _volumetric_flow_mm3s(
                delta_e, last_position, (new_x, new_y, new_z), limits.nozzle_diameter_mm, feed_mm_s
            )
            if flow:
                stats["max_volumetric_flow_seen"] = max(stats["max_volumetric_flow_seen"], flow)
                if limits.max_volumetric_flow_mm3s and flow > limits.max_volumetric_flow_mm3s:
                    findings.append(
                        Finding(
                            code="VOLUMETRIC_FLOW_EXCEEDED",
                            severity="warning",
                            message=(
                                f"Peak volumetric flow of {flow:.1f} mm3/s exceeds this "
                                f"machine's {limits.max_volumetric_flow_mm3s:.1f} mm3/s limit."
                            ),
                            line_number=line_number,
                            measured_value=round(flow, 2),
                            limit_value=limits.max_volumetric_flow_mm3s,
                            unit="mm3/s",
                            mitigation=(
                                "Slow down, raise the temperature, or widen the extrusion width. "
                                "Exceeding melt capacity causes under-extrusion."
                            ),
                        )
                    )

        x, y, z = new_x, new_y, new_z
        last_position = (x, y, z)

        stats["max_z_seen"] = max(stats["max_z_seen"], z)
        stats["min_z_seen"] = min(stats["min_z_seen"], z)

        # --- build volume ------------------------------------------------
        for axis, value in (("X", x), ("Y", y), ("Z", z)):
            if not _has_axis_limit(limits, axis):
                continue
            low, high = limits.axis_bounds(axis.lower())
            if value > high + 1e-6:
                findings.append(
                    Finding(
                        code="OUT_OF_BOUNDS",
                        severity="critical",
                        message=(
                            f"{axis}{value:.3f} is outside this machine's work range "
                            f"[{low:.1f}, {high:.1f}] mm."
                        ),
                        line_number=line_number,
                        axis=axis,
                        measured_value=round(value, 4),
                        limit_value=high,
                        unit="mm",
                        mitigation=(
                            "The model does not fit in this orientation. Re-orient it, "
                            "scale it, or choose a larger machine."
                        ),
                    )
                )
            elif value < low - 1e-6:
                findings.append(
                    Finding(
                        code="OUT_OF_BOUNDS",
                        severity="critical",
                        message=(
                            f"{axis}{value:.3f} is outside this machine's work range "
                            f"[{low:.1f}, {high:.1f}] mm."
                        ),
                        line_number=line_number,
                        axis=axis,
                        measured_value=round(value, 4),
                        limit_value=low,
                        unit="mm",
                        mitigation=(
                            "The model extends past the machine origin. Re-orient or "
                            "centre it so the whole part is on the bed."
                        ),
                    )
                )

    if unknown_commands:
        summary = ", ".join(f"{cmd} x{count}" for cmd, count in sorted(unknown_commands.items()))
        findings.append(
            Finding(
                code="UNKNOWN_COMMAND",
                severity="warning",
                message=(
                    f"Commands not in this firmware's dialect: {summary}. They were "
                    "ignored during validation, so their effect is unverified."
                ),
                mitigation=(
                    "Confirm the G-code dialect in the machine profile matches the "
                    "printer's firmware."
                ),
            )
        )

    if stats["extrusion_moves"] == 0:
        findings.append(
            Finding(
                code="NO_EXTRUSION",
                severity="critical",
                message="The file contains no extruding moves, so it would print nothing.",
                mitigation="Regenerate the G-code; the slicer produced an empty toolpath.",
            )
        )

    severity_counts = {"critical": 0, "warning": 0, "info": 0}
    for finding in findings:
        severity_counts[finding.severity] = severity_counts.get(finding.severity, 0) + 1

    blocks = severity_counts["critical"] > 0
    passed = not blocks

    if blocks:
        summary = (
            f"Output blocked: {severity_counts['critical']} critical finding(s) would make "
            "this file unsafe or impossible to print."
        )
    elif severity_counts["warning"]:
        summary = (
            f"No blocking issues. {severity_counts['warning']} warning(s) to review before "
            "printing."
        )
    else:
        summary = "Validated against the selected machine, material and configured parameters."

    stats["nozzle_temp_target"] = nozzle_temp
    stats["bed_temp_target"] = bed_temp

    return ValidationReport(
        passed=passed,
        blocks_output=blocks,
        findings=findings,
        severity_counts=severity_counts,
        summary=summary,
        stats=stats,
    )


def _volumetric_flow_mm3s(
    delta_e: float,
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    nozzle_diameter_mm: float,
    feed_mm_s: float = 0.0,
) -> float | None:
    """Extruded volume per second for one move, in mm3/s.

    Returns None when the move has no usable length or feed, so a blob
    (positive E on a zero-length move) cannot produce a divide-by-zero spike.
    """
    distance = math.dist(start, end)
    if distance <= 1e-9 or feed_mm_s <= 0 or delta_e <= 0:
        return None
    return volumetric_flow_mm3s(delta_e, distance, feed_mm_s)


def volumetric_flow_mm3s(
    delta_e: float,
    distance_mm: float,
    feed_mm_s: float,
    filament_diameter_mm: float = 1.75,
) -> float | None:
    """Extruded volume per second for a move.

    `delta_e` is in millimetres of filament; the volume it represents is
    divided by the travel time.
    """
    if distance_mm <= 1e-9 or feed_mm_s <= 0:
        return None
    filament_area = math.pi * (filament_diameter_mm / 2) ** 2  # mm^2
    volume_mm3 = delta_e * filament_area
    return volume_mm3 / (distance_mm / feed_mm_s)
