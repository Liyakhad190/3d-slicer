"""Toolpath parsing: G-code in, a renderable toolpath out.

The 3D viewer needs geometry, and a G-code file is the only record of what the
machine will actually do. This module produces a compact, renderable structure
from the file the slicer actually emitted — not from the model, and not from
what we hoped the slicer would do.

Coordinates are returned in millimetres in the machine's own frame, so the
viewer can draw the build plate to scale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.slicing.validator import MachineLimits, iter_lines, _parse_words

TOOLPATH_VERSION = "toolpath-parser-1.0.0"

#: Cap on returned segments, so a 2 million move file cannot be shipped to a
#: browser. The viewer shows what it can and says how much was elided.
DEFAULT_MAX_SEGMENTS = 60_000

_PRECISION = 3


@dataclass
class ToolpathSegment:
    kind: str  # extrude | travel | retract
    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float
    layer: int | None = None
    e: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "from": [round(self.x0, _PRECISION), round(self.y0, _PRECISION), round(self.z0, _PRECISION)],
            "to": [round(self.x1, _PRECISION), round(self.y1, _PRECISION), round(self.z1, _PRECISION)],
            "layer": self.layer,
            "e": round(self.e, 4),
        }


@dataclass
class Toolpath:
    segments: list[ToolpathSegment] = field(default_factory=list)
    layer_count: int = 0
    layer_z: list[float] = field(default_factory=list)
    bounds: dict[str, list[float]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    elided: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": TOOLPATH_VERSION,
            "layer_count": self.layer_count,
            "layer_z": [round(z, _PRECISION) for z in self.layer_z],
            "bounds": self.bounds,
            "stats": self.stats,
            "segments": [s.to_dict() for s in self.segments],
            "segments_elided": self.elided,
        }


def parse_toolpath(
    text: str,
    limits: MachineLimits,
    *,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
) -> Toolpath:
    """Turn G-code into a renderable toolpath.

    Layer boundaries come from the slicer's own `;LAYER:` markers when it emits
    them, and are inferred from Z changes only when it does not.

    Inferring from every Z change looks equivalent and is not: Z-hop moves change
    Z without starting a layer, so a 67-layer print reported 253 layers and the
    viewer's layer scrubber became useless.
    """
    path = Toolpath()
    x = y = z = 0.0
    e_absolute = 0.0
    e_relative = True
    in_relative_xyz = False
    layer = 0
    layer_z: list[float] = []
    seen_layer_z: set[float] = set()

    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")
    extrusions = travels = retractions = 0
    total_extrusion_mm = 0.0

    markers = layer_markers(text)
    marker_by_line = {line: index for index, line in markers}
    use_markers = bool(markers)

    for line_number, raw in iter_lines(text):
        if use_markers and line_number in marker_by_line:
            layer = marker_by_line[line_number]

        body = raw.split(";", 1)[0].strip()
        if not body:
            continue
        # The comment carries the layer index; strip it before parsing.
        words = _parse_words(body)
        if not words:
            continue

        command = None
        for letter, value in words:
            if letter in {"G", "M"}:
                command = f"{letter}{int(value)}"
                break
        params = {letter: value for letter, value in words if letter not in {"G", "M"}}

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

        if command not in {"G0", "G1"}:
            continue

        start = (x, y, z)
        new_x = x + params["X"] if (in_relative_xyz and "X" in params) else params.get("X", x)
        new_y = y + params["Y"] if (in_relative_xyz and "Y" in params) else params.get("Y", y)
        new_z = z + params["Z"] if (in_relative_xyz and "Z" in params) else params.get("Z", z)

        delta_e = 0.0
        if "E" in params:
            if e_relative:
                delta_e = params["E"]
                e_absolute += delta_e
            else:
                delta_e = params["E"] - e_absolute
                e_absolute = params["E"]

        kind: str | None = None
        if delta_e > 0:
            kind = "extrude"
            extrusions += 1
            total_extrusion_mm += delta_e
        elif delta_e < 0:
            kind = "retract"
            retractions += 1
        else:
            kind = "travel"
            travels += 1

        # A new Z height starts a new layer only when the slicer gave no
        # markers. With markers, `layer` is already authoritative.
        if not use_markers and abs(new_z - z) > 1e-9 and round(new_z, 3) not in seen_layer_z:
            seen_layer_z.add(round(new_z, 3))
            layer_z.append(new_z)

        moved = (new_x, new_y, new_z) != start
        if kind and moved:
            if len(path.segments) < max_segments:
                path.segments.append(
                    ToolpathSegment(
                        kind=kind, x0=x, y0=y, z0=z, x1=new_x, y1=new_y, z1=new_z,
                        layer=layer, e=delta_e,
                    )
                )
            else:
                path.elided += 1

        x, y, z = new_x, new_y, new_z
        min_x, max_x = min(min_x, x), max(max_x, x)
        min_y, max_y = min(min_y, y), max(max_y, y)
        min_z, max_z = min(min_z, z), max(max_z, z)

    if use_markers:
        # Trust the slicer's own layer numbering; record the print Z for each.
        path.layer_count = max(markers, key=lambda item: item[0])[0] + 1
        by_layer: dict[int, list[float]] = {}
        for segment in path.segments:
            if segment.layer is not None:
                by_layer.setdefault(segment.layer, []).append(segment.z0)
        layer_z = []
        for index in range(path.layer_count):
            values = by_layer.get(index)
            layer_z.append(round(max(values), _PRECISION) if values else 0.0)
    else:
        path.layer_count = len(layer_z)

    path.layer_z = layer_z
    path.bounds = {
        "min": [round(min_x, 3), round(min_y, 3), round(min_z, 3)]
        if min_x != float("inf")
        else [0.0, 0.0, 0.0],
        "max": [round(max_x, 3), round(max_y, 3), round(max_z, 3)]
        if max_x != float("-inf")
        else [0.0, 0.0, 0.0],
    }
    path.stats = {
        "extrusion_moves": extrusions,
        "travel_moves": travels,
        "retractions": retractions,
        "filament_extruded_mm": round(total_extrusion_mm, 3),
        "build_volume": {"x": limits.build_x, "y": limits.build_y, "z": limits.build_z},
    }
    return path


_LAYER_MARKER = re.compile(r"^;\s*LAYER:\s*(\d+)", re.IGNORECASE)


def layer_markers(text: str) -> list[tuple[int, int]]:
    """`(layer_index, line_number)` for every `;LAYER:` comment, if present."""
    out: list[tuple[int, int]] = []
    for number, line in iter_lines(text):
        match = _LAYER_MARKER.match(line)
        if match:
            out.append((int(match.group(1)), number))
    return out
