"""Deterministic mesh analysis.

Everything in this module is computed from the actual triangle data by
`trimesh`. There is no model guessing and no LLM involvement: these numbers are
the evidence that every later AI decision must cite.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import trimesh

ANALYZER_VERSION = "mesh-analyzer-1.0.0"
TRIMESH_VERSION = trimesh.__version__

# A triangle whose area is below this is treated as degenerate. 1e-12 mm^2 is
# far below any real feature yet well above pure float noise.
DEGENERATE_AREA_MM2 = 1e-9
#: Two vertices closer than this are duplicates.
DUPLICATE_TOLERANCE_MM = 1e-6


@dataclass(frozen=True)
class Finding:
    """One observation about the mesh, with the numbers that produced it."""

    code: str
    severity: str  # critical | warning | info
    message: str
    measured_value: float | None = None
    limit_value: float | None = None
    unit: str | None = None
    region: str | None = None
    mitigation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GeometryMetrics:
    triangle_count: int
    vertex_count: int
    face_count: int
    dimensions_mm: dict[str, float]
    bounds_min: list[float]
    bounds_max: list[float]
    volume_mm3: float | None
    surface_area_mm2: float | None
    is_watertight: bool
    is_winding_consistent: bool
    is_volume: bool
    euler_number: int
    body_count: int
    non_manifold_edge_count: int
    boundary_edge_count: int
    degenerate_face_count: int
    duplicate_vertex_count: int
    inverted_normal_count: int
    mesh_density_percent: float
    overhang_face_count: int
    overhang_area_mm2: float
    overhang_area_percent: float
    worst_overhang_deg: float
    min_feature_mm: float | None = None
    estimated_min_wall_mm: float | None = None
    min_thickness_percentile_mm: float | None = None
    bridge_span_mm: float | None = None
    floating_body_count: int = 0
    units: str = "mm"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MeshAnalysisReport:
    model_version_id: str | None
    input_sha256: str
    analyzer: str
    analyzer_version: str
    trimesh_version: str
    metrics: GeometryMetrics
    findings: list[Finding] = field(default_factory=list)
    printability_score: float = 0.0
    risk_level: str = "unknown"
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version_id": self.model_version_id,
            "input_sha256": self.input_sha256,
            "analyzer": self.analyzer,
            "analyzer_version": self.analyzer_version,
            "trimesh_version": self.trimesh_version,
            "metrics": self.metrics.to_dict(),
            "findings": [f.to_dict() for f in self.findings],
            "printability_score": self.printability_score,
            "risk_level": self.risk_level,
            "score_breakdown": self.score_breakdown,
            "duration_ms": self.duration_ms,
        }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _edge_face_counts(mesh: trimesh.Trimesh) -> tuple[np.ndarray, int, int]:
    """Return per-unique-edge face counts, plus non-manifold and boundary counts.

    An edge shared by exactly two faces is manifold. One face means a hole
    (boundary). Three or more means non-manifold geometry.
    """
    if len(mesh.faces) == 0:
        return np.array([], dtype=int), 0, 0

    edges = mesh.edges_sorted.reshape(-1, 2)
    pairs = edges[:, 0].astype(np.int64) * (len(mesh.vertices) + 1) + edges[:, 1]
    unique, counts = np.unique(pairs, return_counts=True)
    non_manifold = int(np.count_nonzero(counts > 2))
    boundary = int(np.count_nonzero(counts == 1))
    return counts, non_manifold, boundary


def _duplicate_vertices(mesh: trimesh.Trimesh) -> int:
    if len(mesh.vertices) == 0:
        return 0
    quantised = np.round(mesh.vertices / DUPLICATE_TOLERANCE_MM).astype(np.int64)
    _, counts = np.unique(quantised, axis=0, return_counts=True)
    return int((counts - 1).sum())


def _inverted_normal_count(mesh: trimesh.Trimesh) -> int:
    """Faces whose winding is inverted relative to the volume they bound.

    Only meaningful for a closed volume; an open mesh has no interior to
    disagree with, so we report 0 rather than a fabricated number.
    """
    if not mesh.is_watertight or not mesh.is_winding_consistent:
        return 0
    if mesh.volume <= 0:
        # Negative signed volume means the whole shell is inside-out.
        return len(mesh.faces)
    return 0


@dataclass(frozen=True)
class ThicknessMeasurement:
    """Inward ray-cast results. All values in mm."""

    minimum: float
    p05: float
    median: float
    sample_count: int
    #: Share of samples below `threshold`, used to distinguish a real thin
    #: wall from a handful of rays that clipped a convex edge.
    fraction_below: dict[float, float]


def _sample_thickness(
    mesh: trimesh.Trimesh, *, samples: int = 3000, seed: int = 0
) -> ThicknessMeasurement | None:
    """Measure local wall thickness by casting rays inward from the surface.

    For each sampled surface point we shoot a ray along the inward normal and
    record the distance to the far surface. Those distances are real section
    thicknesses, not estimates from curvature.

    The ray origin is pushed a small distance *inside* the solid first. Without
    that offset the origin triangle is hit at t=0, which trimesh reports as a
    zero-length hit — the previous behaviour, and the reason a solid cube came
    back with a "0.00 mm wall".
    """
    if not mesh.is_watertight or len(mesh.faces) < 4 or mesh.volume <= 0:
        return None

    try:
        points, face_index = trimesh.sample.sample_surface(mesh, samples, seed=seed)
    except Exception:  # pragma: no cover - sampler edge cases
        return None

    normals = mesh.face_normals[face_index]
    diagonal = float(np.linalg.norm(mesh.extents))
    epsilon = max(diagonal * 1e-5, 1e-4)

    # Step inside the shell so the surface triangle is behind us.
    origins = points - normals * epsilon

    try:
        locations, index_ray, _ = mesh.ray.intersects_location(
            ray_origins=origins, ray_directions=-normals, multiple_hits=True
        )
    except Exception:  # pragma: no cover - degenerate geometry
        return None

    if len(locations) == 0:
        return None

    distances = np.linalg.norm(locations - origins[index_ray], axis=1)
    finite = np.isfinite(distances) & (distances > epsilon)
    if not np.any(finite):
        return None

    # With multiple hits, keep the furthest one per ray: that is the exit
    # point of the section we are measuring.
    per_ray: dict[int, float] = {}
    for ray_index, distance in zip(index_ray[finite], distances[finite], strict=True):
        key = int(ray_index)
        if distance > per_ray.get(key, 0.0):
            per_ray[key] = float(distance)

    values = np.fromiter(per_ray.values(), dtype=float, count=len(per_ray))
    if values.size == 0:
        return None

    thresholds = (0.4, 0.6, 0.8, 1.0, 1.2, 2.0)
    fractions = {t: float(np.count_nonzero(values < t) / values.size) for t in thresholds}

    return ThicknessMeasurement(
        minimum=float(values.min()),
        p05=float(np.percentile(values, 5)),
        median=float(np.median(values)),
        sample_count=int(values.size),
        fraction_below=fractions,
    )


def _overhang_analysis(
    mesh: trimesh.Trimesh, *, threshold_deg: float = 45.0, bed_tolerance_mm: float = 0.6
) -> tuple[int, float, float, float]:
    """Count downward-facing faces beyond `threshold_deg` from vertical.

    A downward-facing face makes an overhang angle of ``asin(-n.z)`` measured
    from a vertical wall: 0 deg is a wall, 90 deg is a ceiling.

    Faces lying on the build plate are excluded. The underside of a part
    resting on the bed is the single largest downward-facing area of almost
    every model, and it is supported by definition — counting it would make
    every cube look like it needs support.
    """
    if len(mesh.faces) == 0:
        return 0, 0.0, 0.0, 0.0

    centroids_z = mesh.triangles_center[:, 2]
    bed_z = float(mesh.bounds[0][2])
    on_bed = centroids_z <= bed_z + bed_tolerance_mm

    nz = mesh.face_normals[:, 2]
    angles = np.degrees(np.arcsin(np.clip(-nz, -1.0, 1.0)))
    overhanging = (angles > threshold_deg) & (~on_bed)

    face_areas = mesh.area_faces
    # Exclude bed-contact area from the denominator so the percentage answers
    # "how much of the exposed part is overhanging?".
    reference_area = float(face_areas[~on_bed].sum()) or float(face_areas.sum())

    area = float(face_areas[overhanging].sum())
    percent = 100.0 * area / reference_area if reference_area > 0 else 0.0
    worst = float(angles[overhanging].max()) if np.any(overhanging) else 0.0

    return int(np.count_nonzero(overhanging)), area, percent, worst


def _floating_bodies(mesh: trimesh.Trimesh, *, min_area_mm2: float = 5.0) -> int:
    """Connected components too small to be a real feature.

    A part made of one body plus a 2 mm crumb is a common slicer failure, and
    it is invisible in a screenshot — only the connectivity graph shows it.
    """
    if mesh.body_count <= 1:
        return 0
    try:
        parts = mesh.split(only_watertight=False)
    except Exception:  # pragma: no cover
        return 0
    return sum(1 for part in parts if float(part.area) < min_area_mm2)


def _mesh_density_percent(metrics_volume: float | None, bbox_volume: float) -> float:
    if not metrics_volume or bbox_volume <= 0:
        return 0.0
    return 100.0 * metrics_volume / bbox_volume


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def analyze_mesh(
    mesh: trimesh.Trimesh,
    *,
    input_sha256: str,
    model_version_id: str | None = None,
    no_opening_mm: float = 0.4,
    max_overhang_deg: float = 45.0,
) -> MeshAnalysisReport:
    """Produce a full, deterministic analysis report.

    `no_opening_mm` is the nozzle diameter, used only to decide whether a
    measured thin wall is a real risk. It never changes a measurement.
    """
    findings: list[Finding] = []

    extents = mesh.extents
    dims = {"x": float(extents[0]), "y": float(extents[1]), "z": float(extents[2])}
    bounds_min = [float(v) for v in mesh.bounds[0]]
    bounds_max = [float(v) for v in mesh.bounds[1]]
    bbox_volume = float(extents[0] * extents[1] * extents[2])

    volume = float(mesh.volume) if mesh.is_volume else None
    area = float(mesh.area)

    edge_counts, non_manifold, boundary = _edge_face_counts(mesh)
    duplicate_verts = _duplicate_vertices(mesh)
    inverted = _inverted_normal_count(mesh)

    face_areas = mesh.area_faces if len(mesh.faces) else np.array([])
    degenerate = int(np.count_nonzero(face_areas < DEGENERATE_AREA_MM2)) if face_areas.size else 0

    thickness = _sample_thickness(mesh)
    thinnest = thickness.minimum if thickness else None
    p05 = thickness.p05 if thickness else None
    median_thickness = thickness.median if thickness else None
    overhang_faces, overhang_area, overhang_pct, worst_overhang = _overhang_analysis(
        mesh, threshold_deg=max_overhang_deg
    )
    floating = _floating_bodies(mesh)
    density = _mesh_density_percent(volume, bbox_volume)

    # A body-count-based bridge span estimate: the widest unsupported span is
    # approximated by the second-largest connected component's extent.
    bridge_span: float | None = None
    if mesh.body_count > 1:
        try:
            parts = sorted(mesh.split(only_watertight=False), key=lambda p: -p.area)
            if len(parts) > 1:
                bridge_span = float(parts[1].extents.max())
        except Exception:  # pragma: no cover
            bridge_span = None

    metrics = GeometryMetrics(
        triangle_count=int(len(mesh.faces)),
        vertex_count=int(len(mesh.vertices)),
        face_count=int(len(mesh.faces)),
        dimensions_mm={k: round(v, 4) for k, v in dims.items()},
        bounds_min=[round(v, 5) for v in bounds_min],
        bounds_max=[round(v, 5) for v in bounds_max],
        volume_mm3=round(volume, 4) if volume is not None else None,
        surface_area_mm2=round(area, 4),
        is_watertight=bool(mesh.is_watertight),
        is_winding_consistent=bool(mesh.is_winding_consistent),
        is_volume=bool(mesh.is_volume),
        euler_number=int(mesh.euler_number),
        body_count=int(mesh.body_count),
        non_manifold_edge_count=non_manifold,
        boundary_edge_count=boundary,
        degenerate_face_count=degenerate,
        duplicate_vertex_count=duplicate_verts,
        inverted_normal_count=inverted,
        mesh_density_percent=round(density, 2),
        min_feature_mm=round(thinnest, 4) if thinnest is not None else None,
        estimated_min_wall_mm=round(p05, 4) if p05 is not None else None,
        min_thickness_percentile_mm=round(median_thickness, 4) if median_thickness is not None else None,
        overhang_face_count=overhang_faces,
        overhang_area_mm2=round(overhang_area, 4),
        overhang_area_percent=round(overhang_pct, 3),
        worst_overhang_deg=round(worst_overhang, 2),
        bridge_span_mm=round(bridge_span, 3) if bridge_span else None,
        floating_body_count=floating,
    )

    findings.extend(
        _mesh_integrity_findings(metrics, max_overhang_deg=max_overhang_deg)
    )
    findings.extend(
        _feature_findings(metrics, no_opening_mm=no_opening_mm, thickness=thickness)
    )
    score, risk, breakdown = score_printability(findings, metrics)
    findings.extend(_advisory_findings(metrics))

    return MeshAnalysisReport(
        model_version_id=model_version_id,
        input_sha256=input_sha256,
        analyzer="trimesh",
        analyzer_version=ANALYZER_VERSION,
        trimesh_version=TRIMESH_VERSION,
        metrics=metrics,
        findings=findings,
        printability_score=score,
        risk_level=risk,
        score_breakdown=breakdown,
    )


def _mesh_integrity_findings(
    metrics: GeometryMetrics, *, max_overhang_deg: float
) -> list[Finding]:
    out: list[Finding] = []

    if not metrics.is_watertight:
        out.append(
            Finding(
                code="MESH_NOT_WATERTIGHT",
                severity="critical",
                message=(
                    "The mesh is not watertight. Slicers treat it as a solid, so "
                    "interior voids and missing faces can produce wrong toolpaths."
                ),
                measured_value=float(metrics.boundary_edge_count),
                unit="boundary edges",
                mitigation="Run the mesh repair pipeline and re-analyse before slicing.",
            )
        )
    if not metrics.is_winding_consistent:
        out.append(
            Finding(
                code="INCONSISTENT_WINDING",
                severity="critical",
                message="Face winding is inconsistent; the inside/outside of the shell is ambiguous.",
                mitigation="Repair normals before slicing.",
            )
        )
    if metrics.non_manifold_edge_count > 0:
        out.append(
            Finding(
                code="NON_MANIFOLD_EDGES",
                severity="critical",
                message="Non-manifold edges detected (an edge shared by 3+ faces).",
                measured_value=float(metrics.non_manifold_edge_count),
                unit="edges",
                mitigation="Repair the mesh; non-manifold geometry has no well-defined inside.",
            )
        )
    if metrics.inverted_normal_count > 0:
        out.append(
            Finding(
                code="INVERTED_NORMALS",
                severity="critical",
                message="The shell is inside-out (negative enclosed volume).",
                measured_value=float(metrics.inverted_normal_count),
                unit="faces",
                mitigation="Flip face winding during repair.",
            )
        )
    if metrics.degenerate_face_count > 0:
        out.append(
            Finding(
                code="DEGENERATE_FACES",
                severity="warning",
                message="Zero-area (degenerate) triangles detected.",
                measured_value=float(metrics.degenerate_face_count),
                unit="faces",
                mitigation="Repair removes zero-area faces; they are harmless but wasteful.",
            )
        )
    if metrics.duplicate_vertex_count > 0:
        out.append(
            Finding(
                code="DUPLICATE_VERTICES",
                severity="info",
                message="Coincident duplicate vertices detected.",
                measured_value=float(metrics.duplicate_vertex_count),
                unit="vertices",
                mitigation="Repair merges duplicates, which speeds up slicing.",
            )
        )
    if metrics.floating_body_count > 0:
        out.append(
            Finding(
                code="FLOATING_GEOMETRY",
                severity="warning",
                message="Small disconnected bodies detected — these may print separately.",
                measured_value=float(metrics.floating_body_count),
                unit="bodies",
                mitigation="Inspect the model; remove crumbs or confirm they are intended parts.",
            )
        )
    if metrics.overhang_face_count > 0:
        out.append(
            Finding(
                code="OVERHANGS",
                severity="warning",
                message=(
                    f"Overhangs steeper than {max_overhang_deg:.0f} deg from vertical "
                    "require support."
                ),
                measured_value=metrics.overhang_area_mm2,
                limit_value=None,
                unit="mm^2",
                mitigation="Enable support, or reorient the model to reduce unsupported area.",
            )
        )
    return out


def _feature_findings(
    metrics: GeometryMetrics,
    *,
    no_opening_mm: float,
    thickness: ThicknessMeasurement | None,
) -> list[Finding]:
    out: list[Finding] = []
    wall = metrics.estimated_min_wall_mm

    if wall is not None and thickness is not None:
        # A wall thinner than two extrusion widths (one per side) cannot be
        # produced as a solid perimeter by any slicer.
        limit = round(2 * no_opening_mm, 3)
        # Require a real share of the surface to be affected. A handful of rays
        # clipping a convex edge is an artefact of ray casting, not a defect —
        # requiring >=1% of samples keeps sharp chamfers from being reported
        # as unprintable walls.
        affected = max(
            (frac for threshold, frac in thickness.fraction_below.items() if threshold <= limit),
            default=0.0,
        )
        if wall < limit and affected >= 0.01:
            severity = "critical" if wall < no_opening_mm * 0.75 else "warning"
            out.append(
                Finding(
                    code="THIN_WALL",
                    severity=severity,
                    message=(
                        f"Thinnest measured wall is {wall:.2f} mm ({affected:.1%} of the "
                        f"surface), below the {limit:.2f} mm needed for two extrusion "
                        f"lines with a {no_opening_mm:.2f} mm nozzle."
                    ),
                    measured_value=wall,
                    limit_value=limit,
                    unit="mm",
                    mitigation=(
                        "Increase nozzle size, or thicken the wall in CAD. Reorienting "
                        "will not change a wall thickness."
                    ),
                )
            )
    if metrics.bridge_span_mm and metrics.bridge_span_mm > 3 * no_opening_mm:
        out.append(
            Finding(
                code="LONG_BRIDGE",
                severity="warning",
                message=(
                    f"Unsupported span of roughly {metrics.bridge_span_mm:.1f} mm "
                    "detected; long bridges droop."
                ),
                measured_value=metrics.bridge_span_mm,
                limit_value=round(3 * no_opening_mm, 2),
                unit="mm",
                mitigation="Enable support, or reorient so the span is supported.",
            )
        )
    return out


def _advisory_findings(metrics: GeometryMetrics) -> list[Finding]:
    """Informational notes, including genuine optimisation opportunities."""
    out: list[Finding] = []
    if metrics.mesh_density_percent and metrics.mesh_density_percent < 5:
        out.append(
            Finding(
                code="LOW_DENSITY",
                severity="info",
                message=(
                    f"The part fills only {metrics.mesh_density_percent:.1f}% of its "
                    "bounding box — a good candidate for sparse infill."
                ),
                measured_value=metrics.mesh_density_percent,
                unit="%",
                mitigation="A 10-15% gyroid or cubic infill will be visually equivalent.",
            )
        )
    if metrics.triangle_count > 2_000_000:
        out.append(
            Finding(
                code="HIGH_POLYCOUNT",
                severity="info",
                message=f"{metrics.triangle_count:,} triangles will slice slowly.",
                measured_value=float(metrics.triangle_count),
                unit="triangles",
                mitigation="Decimate the mesh if the extra detail is not needed.",
            )
        )
    if metrics.body_count > 1:
        out.append(
            Finding(
                code="MULTI_BODY",
                severity="info",
                message=f"The file contains {metrics.body_count} disconnected bodies.",
                measured_value=float(metrics.body_count),
                unit="bodies",
                mitigation="Confirm each body is intended to print where it appears.",
            )
        )
    return out


#: Severity weights. Critical findings dominate, but a wall of warnings is still
#: meaningfully worse than a single one, so counts matter too.
_SEVERITY_WEIGHT = {"critical": 34.0, "warning": 9.0, "info": 2.0}


def score_printability(
    findings: Sequence[Finding], metrics: GeometryMetrics
) -> tuple[float, str, dict[str, Any]]:
    """A transparent 0-100 score with a published breakdown.

    This is a *relative* indicator for ranking configurations, not a promise
    that a print will succeed. The breakdown is returned so the UI can show
    exactly where the points were lost.
    """
    penalty = 0.0
    by_severity: dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    contributors: list[dict[str, Any]] = []

    for finding in findings:
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
        # INFO never subtracts: it is an opportunity note, not a defect.
        if finding.severity == "info":
            continue
        cost = _SEVERITY_WEIGHT[finding.severity]
        penalty += cost
        contributors.append({"code": finding.code, "penalty": cost})

    penalty = min(penalty, 100.0)
    score = round(max(0.0, 100.0 - penalty), 1)

    if score >= 85 and by_severity["critical"] == 0:
        risk = "low"
    elif score >= 60 and by_severity["critical"] == 0:
        risk = "medium"
    else:
        risk = "high"

    breakdown = {
        "base_score": 100.0,
        "penalty_total": round(penalty, 2),
        "severity_weights": _SEVERITY_WEIGHT,
        "counts_by_severity": by_severity,
        "contributors": contributors,
        "method": (
            "100 minus the sum of severity weights "
            "(critical 34, warning 9, info 0, capped at 100). "
            "Relative indicator only; not a prediction of print success."
        ),
    }
    return score, risk, breakdown


def load_mesh(path: str, *, file_type: str | None = None) -> trimesh.Trimesh:
    """Load a mesh from disk, forcing a single merged mesh.

    `force="mesh"` concatenates multi-body files so watertightness and volume
    are evaluated over the whole part, which is what the user means by
    "the model".

    Vertex merging is mandatory rather than optional: STL and PLY are *facet*
    formats that store every triangle's corners independently, so a cube read
    without merging appears as 12 unrelated triangles — not watertight, 12
    bodies, and a volume of zero. Nothing else is auto-repaired, because
    defects like inconsistent winding are exactly what we are here to report.
    """
    scene_or_mesh = trimesh.load(path, file_type=file_type, force="mesh", process=False)
    if isinstance(scene_or_mesh, trimesh.Scene):
        meshes = [g for g in scene_or_mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not meshes:
            raise ValueError("File contains no triangle geometry.")
        scene_or_mesh = trimesh.util.concatenate(meshes)
    if not isinstance(scene_or_mesh, trimesh.Trimesh):  # pragma: no cover
        raise ValueError(f"Unsupported geometry type: {type(scene_or_mesh)!r}")
    if len(scene_or_mesh.faces) == 0:
        raise ValueError("Mesh contains no faces.")

    # Remove unreferenced vertices and weld coincident ones; do NOT fix
    # winding/normals or invert anything, as that would erase the findings.
    scene_or_mesh.merge_vertices()
    scene_or_mesh.remove_unreferenced_vertices()
    return scene_or_mesh


def analyse_file(
    path: str,
    *,
    input_sha256: str,
    model_version_id: str | None = None,
    no_opening_mm: float = 0.4,
    max_overhang_deg: float = 45.0,
    file_type: str | None = None,
) -> MeshAnalysisReport:
    mesh = load_mesh(path, file_type=file_type)
    return analyze_mesh(
        mesh,
        input_sha256=input_sha256,
        model_version_id=model_version_id,
        no_opening_mm=no_opening_mm,
        max_overhang_deg=max_overhang_deg,
    )


def summarise_for_display(report: MeshAnalysisReport) -> dict[str, Any]:
    """The compact block the UI shows under the model in the viewport."""
    m = report.metrics
    return {
        "dimensions": {
            "x": round(m.dimensions_mm["x"], 2),
            "y": round(m.dimensions_mm["y"], 2),
            "z": round(m.dimensions_mm["z"], 2),
        },
        "triangles": m.triangle_count,
        "watertight": m.is_watertight,
        "manifold": m.non_manifold_edge_count == 0 and m.is_winding_consistent,
        "volume_mm3": m.volume_mm3,
        "surface_area_mm2": m.surface_area_mm2,
        "min_wall_mm": m.estimated_min_wall_mm,
        "overhang_area_percent": m.overhang_area_percent,
        "printability_score": report.printability_score,
        "risk_level": report.risk_level,
        "finding_counts": report.score_breakdown.get("counts_by_severity", {}),
    }


def radians_to_degrees(values: Sequence[float]) -> list[float]:
    return [round(math.degrees(v), 4) for v in values]
