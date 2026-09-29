"""Measurement: what the emitted G-code actually deposits.

The fidelity gap this module closes is the difference between three different
claims, which the project must never conflate:

* **REQUESTED** — what `PrintConfiguration` asked for.
* **GENERATED** — what the engine's settings file contained.
* **MEASURED** — what the G-code the engine produced can be shown to do.

A slice can be accepted by the engine, produce a plausible-looking file, and
still not do what was asked. Only measurement from the output can tell you
that, so this module reads the toolpath and reports geometry.

What is measured here is the *toolpath*, not the plastic. Nothing in this file
can tell you whether a 0.4 mm wall came out 0.4 mm wide on the bed, whether
material adhered, or whether the part fits its mating assembly. Every report
carries `physical_validation_required` for exactly that reason.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.slicing.toolpath import layer_markers
from app.slicing.validator import MachineLimits, _parse_words, iter_lines

MEASUREMENT_VERSION = "gcode-measurement-1.0.0"

#: Endpoints closer than this are treated as the same point when welding
#: segments into contours. 10 microns is far below any slicer resolution and
#: far above float noise in accumulated coordinates.
WELD_TOLERANCE_MM = 0.01


@dataclass
class Move:
    """One G0/G1 move with the state the parser had at the time."""

    line: int
    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float
    delta_e: float
    feed_mm_s: float
    z_hop: bool = False

    @property
    def length(self) -> float:
        return math.dist((self.x0, self.y0, self.z0), (self.x1, self.y1, self.z1))

    @property
    def xy_length(self) -> float:
        return math.dist((self.x0, self.y0), (self.x1, self.y1))

    @property
    def extruding(self) -> bool:
        return self.delta_e > 0

    @property
    def retracting(self) -> bool:
        return self.delta_e < 0


@dataclass
class LayerMeasurement:
    """One layer's real geometry."""

    index: int
    z_mm: float
    extruding_moves: int = 0
    travelling_moves: int = 0
    retracted_moves: int = 0
    extruded_length_mm: float = 0.0
    travel_length_mm: float = 0.0
    z_hop_count: int = 0
    #: Distinct welded closed/open contours. With infill disabled this is the
    #: observable perimeter count.
    contour_count: int = 0
    #: Longest single extruded run, a proxy for "is anything being laid down".
    longest_run_mm: float = 0.0
    #: Distinct Z heights seen in this layer, excluding pure Z hops.
    z_values: list[float] = field(default_factory=list)

    def deposited_area_mm2(self, line_width_mm: float) -> float:
        """Area laid down in this layer, assuming no overlap between lines."""
        return self.extruded_length_mm * line_width_mm

    def to_dict(self, line_width_mm: float, layer_height_mm: float) -> dict[str, Any]:
        area = self.deposited_area_mm2(line_width_mm)
        return {
            "index": self.index,
            "z_mm": round(self.z_mm, 4),
            "extruding_moves": self.extruding_moves,
            "travelling_moves": self.travelling_moves,
            "retracted_moves": self.retracted_moves,
            "extruded_length_mm": round(self.extruded_length_mm, 4),
            "travel_length_mm": round(self.travel_length_mm, 4),
            "contour_count": self.contour_count,
            "z_hop_count": self.z_hop_count,
            "deposited_area_mm2": round(area, 4),
            "deposited_volume_mm3": round(area * layer_height_mm, 4),
        }


@dataclass
class MeasurementReport:
    """Whole-file measurement plus the caveat that always travels with it."""

    layers: list[LayerMeasurement] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    line_width_mm: float = 0.45
    layer_height_mm: float = 0.3
    layer_count: int = 0
    nozzle_temp_range: tuple[float, float] | None = None
    bed_temp_range: tuple[float, float] | None = None
    fan_range: tuple[float, float] | None = None
    homing_commands: int = 0
    used_layer_markers: bool = False
    bounds: dict[str, list[float]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # -- aggregates -------------------------------------------------------

    @property
    def total_extruded_length_mm(self) -> float:
        return sum(layer.extruded_length_mm for layer in self.layers)

    @property
    def total_travel_length_mm(self) -> float:
        """Path length travelled without extruding, in mm of machine travel."""
        return sum(layer.travel_length_mm for layer in self.layers)

    @property
    def total_filament_mm(self) -> float:
        """Filament consumed, in mm of 1.75 mm stock, from the E axis.

        Distinct from `total_extruded_length_mm`, which is *path length in the
        XY plane*. The two differ by roughly the ratio of the deposited
        cross-section to the filament cross-section, and confusing them turns a
        material estimate into a number that is wrong by an order of magnitude.
        """
        return sum(move.delta_e for move in self.moves if move.delta_e > 0)

    @property
    def total_deposited_area_mm2(self) -> float:
        return self.total_extruded_length_mm * self.line_width_mm

    @property
    def total_deposited_volume_mm3(self) -> float:
        return self.total_deposited_area_mm2 * self.layer_height_mm

    @property
    def retraction_count(self) -> int:
        return sum(layer.retracted_moves for layer in self.layers)

    def median_contour_count(self) -> float:
        """Contours per layer, ignoring the first and last few.

        The first layer carries skirt/brim and the top layers carry roof
        transitions, so including them skews the statistic badly.
        """
        if len(self.layers) < 5:
            return float(len(self.layers) / 2) if self.layers else 0.0
        middle = self.layers[2:-2]
        counts = sorted(layer.contour_count for layer in middle)
        if not counts:
            return 0.0
        mid = len(counts) // 2
        if len(counts) % 2 == 1:
            return float(counts[mid])
        return (counts[mid - 1] + counts[mid]) / 2.0

    def estimated_time_s(self) -> float:
        """Sum of move durations. Travel and extrusion are the only timed moves."""
        return sum(move.length / move.feed_mm_s for move in self.moves if move.feed_mm_s > 0)

    def speed_distribution(self) -> dict[str, int]:
        buckets = {
            "0-25 mm/s": 0,
            "25-50 mm/s": 0,
            "50-100 mm/s": 0,
            "100-200 mm/s": 0,
            "200+ mm/s": 0,
        }
        for move in self.moves:
            if not move.extruding or move.feed_mm_s <= 0:
                continue
            speed = move.feed_mm_s
            if speed < 25:
                buckets["0-25 mm/s"] += 1
            elif speed < 50:
                buckets["25-50 mm/s"] += 1
            elif speed < 100:
                buckets["50-100 mm/s"] += 1
            elif speed < 200:
                buckets["100-200 mm/s"] += 1
            else:
                buckets["200+ mm/s"] += 1
        return buckets

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": MEASUREMENT_VERSION,
            "physical_validation_required": True,
            "physical_validation_note": (
                "These are toolpath measurements from generated G-code. They do not "
                "establish physical print accuracy: material behaviour, adhesion, "
                "flow, cooling and calibration are not observable here."
            ),
            "inputs": {
                "line_width_mm": self.line_width_mm,
                "layer_height_mm": self.layer_height_mm,
            },
            "layer_count": self.layer_count,
            "used_layer_markers": self.used_layer_markers,
            "bounds": self.bounds,
            "totals": {
                # XY path length while extruding. NOT filament length.
                "extruded_path_length_mm": round(self.total_extruded_length_mm, 3),
                # Filament consumed, from the E axis.
                "filament_mm": round(self.total_filament_mm, 3),
                "travel_length_mm": round(self.total_travel_length_mm, 3),
                "deposited_area_mm2": round(self.total_deposited_area_mm2, 3),
                "deposited_volume_mm3": round(self.total_deposited_volume_mm3, 3),
                "retraction_count": self.retraction_count,
                "estimated_time_s": round(self.estimated_time_s(), 2),
                "homing_commands": self.homing_commands,
            },
            "nozzle_temp_range_c": list(self.nozzle_temp_range) if self.nozzle_temp_range else None,
            "bed_temp_range_c": list(self.bed_temp_range) if self.bed_temp_range else None,
            "fan_range": list(self.fan_range) if self.fan_range else None,
            "median_contour_count": self.median_contour_count(),
            "speed_distribution": self.speed_distribution(),
            "layers": [
                layer.to_dict(self.line_width_mm, self.layer_height_mm)
                for layer in self.layers
            ],
            "notes": self.notes,
        }


def _welded_contour_count(
    moves: Sequence[Move], *, tolerance: float = WELD_TOLERANCE_MM
) -> int:
    """Count distinct chains of extruded segments within one layer.

    Extrusion endpoints arrive as independently rounded decimals, so segments
    that meet do not share a bit-exact coordinate. Endpoint welding on a small
    tolerance recovers the contours; a union-find over the welded graph then
    gives the count.

    With infill density 0, a box sliced with N walls produces exactly N closed
    contours, which makes this a direct measurement of the wall count.
    """
    parent: dict[int, int] = {}

    def find(node: int) -> int:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Bucket endpoints on a grid so welding stays near-linear.
    grid: dict[tuple[int, int, int], int] = {}
    quant = 1.0 / tolerance

    def node_for(point: tuple[float, float, float]) -> int:
        key = (
            int(round(point[0] * quant)),
            int(round(point[1] * quant)),
            int(round(point[2] * quant)),
        )
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    neighbour = grid.get((key[0] + dx, key[1] + dy, key[2] + dz))
                    if neighbour is not None:
                        return neighbour
        node = find(-1) if parent else 0
        node = len(parent)
        grid[key] = node
        parent[node] = node
        return node

    for move in moves:
        if not move.extruding or move.xy_length <= 0:
            continue
        a = node_for((move.x0, move.y0, move.z0))
        b = node_for((move.x1, move.y1, move.z1))
        if a != b:
            union(a, b)

    roots = {find(node) for node in parent}
    return len(roots)


def measure_gcode(
    text: str,
    limits: MachineLimits,
    *,
    line_width_mm: float = 0.45,
    layer_height_mm: float = 0.3,
) -> MeasurementReport:
    """Parse G-code and measure what it actually deposits, layer by layer.

    Layer boundaries come from `;LAYER:` markers when the slicer emits them, and
    from Z changes otherwise. Z-hop moves are counted separately and never open
    a new layer: a hop is a vertical move at the end of a retract, not printed
    material.
    """
    report = MeasurementReport(
        line_width_mm=line_width_mm, layer_height_mm=layer_height_mm
    )

    markers = layer_markers(text)
    marker_by_line = {line: index for index, line in markers}
    use_markers = bool(markers)
    report.used_layer_markers = use_markers

    x = y = z = 0.0
    e_absolute = 0.0
    e_relative = True
    in_relative_xyz = False
    layer_index = 0
    extruding_feed_mm_s = 0.0

    nozzle_temps: list[float] = []
    bed_temps: list[float] = []
    fans: list[float] = []

    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")

    def ensure_layer(index: int, at_z: float) -> LayerMeasurement:
        while len(report.layers) <= index:
            report.layers.append(LayerMeasurement(index=len(report.layers), z_mm=at_z))
        return report.layers[index]

    for line_number, raw in iter_lines(text):
        if use_markers and line_number in marker_by_line:
            layer_index = marker_by_line[line_number]

        body = raw.split(";", 1)[0].strip()
        if not body:
            continue
        words = _parse_words(body)
        if not words:
            continue

        command = None
        for letter, value in words:
            if letter in {"G", "M"}:
                command = f"{letter}{int(value)}"
                break
        params = {letter: value for letter, value in words if letter not in {"G", "M"}}

        # --- non-motion state --------------------------------------------
        if command == "G90":
            in_relative_xyz = False
            continue
        if command == "G91":
            in_relative_xyz = True
            continue
        if command == "M82":
            e_relative = False
            continue
        if command == "M83":
            e_relative = True
            continue
        if command == "G92" and "E" in params:
            e_absolute = params["E"]
            continue
        if command in {"G28"}:
            report.homing_commands += 1
            # A homing move returns to the machine origin; tracking it as a
            # normal move would drag the toolpath bounds to 0,0,0.
            x = y = z = 0.0
            continue
        if command in {"M104", "M109"} and "S" in params:
            nozzle_temps.append(params["S"])
            continue
        if command in {"M140", "M190"} and "S" in params:
            bed_temps.append(params["S"])
            continue
        if command == "M106" and "S" in params:
            fans.append(params["S"])
            continue
        if command == "M107":
            fans.append(0.0)
            continue

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

        feed = params.get("F", 0.0)
        # A feed word persists until the next one, which is the G-code rule.
        if feed > 0:
            extruding_feed_mm_s = feed / 60.0

        is_z_only = abs(new_x - x) < 1e-9 and abs(new_y - y) < 1e-9 and abs(new_z - z) > 1e-9
        z_hop = bool(is_z_only and delta_e == 0)

        move = Move(
            line=line_number,
            x0=x, y0=y, z0=z,
            x1=new_x, y1=new_y, z1=new_z,
            delta_e=delta_e,
            feed_mm_s=extruding_feed_mm_s,
            z_hop=z_hop,
        )

        layer = ensure_layer(layer_index, new_z)

        if z_hop:
            layer.z_hop_count += 1
        elif move.extruding:
            layer.extruding_moves += 1
            layer.extruded_length_mm += move.xy_length
            layer.longest_run_mm = max(layer.longest_run_mm, move.xy_length)
        elif move.retracting:
            layer.retracted_moves += 1
        elif move.xy_length > 0:
            layer.travelling_moves += 1
            layer.travel_length_mm += move.xy_length

        if move.length > 0 or move.extruding:
            report.moves.append(move)

        # Without markers, a Z change that carries material starts a new layer.
        if not use_markers and not z_hop and abs(new_z - z) > 1e-9 and layer.extruding_moves == 0:
            layer_index = len(report.layers)
            ensure_layer(layer_index, new_z)
            layer = report.layers[layer_index]
            layer.z_mm = new_z

        if move.xy_length > 0:
            min_x, max_x = min(min_x, new_x), max(max_x, new_x)
            min_y, max_y = min(min_y, new_y), max(max_y, new_y)
        if abs(new_z - z) > 1e-9:
            min_z, max_z = min(min_z, new_z), max(max_z, new_z)

        x, y, z = new_x, new_y, new_z

    # Contour counts are the per-layer observable; computing them after the
    # walk keeps the hot path allocation-free.
    for index, layer in enumerate(report.layers):
        layer_moves = [m for m in report.moves if m.extruding and abs(m.z0 - layer.z_mm) < 1e-6]
        layer.contour_count = _welded_contour_count(layer_moves) if layer_moves else 0

    report.layer_count = len(report.layers)
    report.nozzle_temp_range = (min(nozzle_temps), max(nozzle_temps)) if nozzle_temps else None
    report.bed_temp_range = (min(bed_temps), max(bed_temps)) if bed_temps else None
    report.fan_range = (min(fans), max(fans)) if fans else None
    report.bounds = {
        "min": [round(min_x, 4), round(min_y, 4), round(min_z, 4)]
        if min_x != float("inf")
        else [0.0, 0.0, 0.0],
        "max": [round(max_x, 4), round(max_y, 4), round(max_z, 4)]
        if max_x != float("-inf")
        else [0.0, 0.0, 0.0],
    }

    report.notes.append(
        f"{'Layer markers' if use_markers else 'Z-change inference'} used for layer boundaries."
    )
    if not use_markers:
        report.notes.append(
            "This slicer emits no ;LAYER: markers, so layers were inferred from Z "
            "changes. Layer count is less trustworthy in that case."
        )
    return report


def measure_layer_crossings(
    moves: Iterable[Move], *, at_z: float, axis: str = "x", tolerance: float = 0.02
) -> list[float]:
    """Coordinates where extruded segments cross a plane, for one layer.

    A direct way to count perimeter loops: a cube with N walls produces N+2
    distinct crossings along a line through its cross-section.
    """
    hits: list[float] = []
    for move in moves:
        if not move.extruding or abs(move.z0 - at_z) > tolerance:
            continue
        if axis == "x":
            if abs(move.y0 - move.y1) < 1e-9:
                continue
            t = (0.0 - move.y0) / (move.y1 - move.y0)
            if 0.0 <= t <= 1.0:
                hits.append(move.x0 + t * (move.x1 - move.x0))
        else:
            if abs(move.x0 - move.x1) < 1e-9:
                continue
            t = (0.0 - move.x0) / (move.x1 - move.x0)
            if 0.0 <= t <= 1.0:
                hits.append(move.y0 + t * (move.y1 - move.y0))
    # Collapse near-duplicate crossings from overlapping segments.
    merged: list[float] = []
    for value in sorted(hits):
        if not merged or abs(value - merged[-1]) > tolerance:
            merged.append(value)
    return merged
