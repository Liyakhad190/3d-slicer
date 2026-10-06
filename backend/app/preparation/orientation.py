"""Deterministic orientation engine.

The model never invents a rotation. It picks an id from a list this module
generated, and every id carries the measurements that make the choice
arguable: contact area, overhang area, support area, height, stability and
whether the pose fits the build volume at all.

Candidate generation is deterministic. Two runs over the same mesh produce the
same candidates in the same order, so a job can be reproduced from its
fingerprint.

The candidate set is deliberately small and mostly axis aligned: a real
print-preparation tool offers poses a human would recognise ("stand it up",
"lay it flat"), not an arbitrary rotation matrix found by an optimiser. Face
aligned poses are added on top for geometry whose dominant flat face is not
axis aligned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import trimesh

from app.core.errors import ValidationFailedError
from app.core.logging import get_logger

log = get_logger(__name__)

ORIENTATION_VERSION = "orientation-engine-1.0.0"

#: Faces at least this far above the build plate cannot be bed contact.
BED_TOLERANCE_MM = 0.6
#: A face is "flat on the bed" when its normal points this squarely upwards.
FLAT_NORMAL_Z = 0.98
#: Near-horizontal downward faces are bridge candidates rather than overhangs.
BRIDGE_ANGLE_DEG = 80.0
#: The longest unsupported ceiling a slicer is assumed to bridge without
#: support material. Above this a flat roof still needs support underneath.
MAX_BRIDGEABLE_SPAN_MM = 30.0
#: How many distinct dominant normals to try to align with the bed.
_DOMINANT_NORMALS = 3
_MAX_CANDIDATES = 10


@dataclass(frozen=True)
class OrientationCandidate:
    """One pose, with the evidence that justifies choosing it."""

    id: str
    rotation_deg: tuple[float, float, float]
    strategy: str
    extents_mm: tuple[float, float, float]
    contact_area_mm2: float
    overhang_area_mm2: float
    overhang_percent: float
    support_area_mm2: float
    footprint_mm2: float
    stability: float
    height_mm: float
    fits: bool
    score: float = 0.0
    notes: list[str] = field(default_factory=list)
    rejected_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rotation_deg": [round(v, 3) for v in self.rotation_deg],
            "strategy": self.strategy,
            "extents_mm": [round(v, 3) for v in self.extents_mm],
            "contact_area_mm2": round(self.contact_area_mm2, 2),
            "overhang_area_mm2": round(self.overhang_area_mm2, 2),
            "overhang_percent": round(self.overhang_percent, 3),
            "support_area_mm2": round(self.support_area_mm2, 2),
            "footprint_mm2": round(self.footprint_mm2, 2),
            "stability": round(self.stability, 3),
            "height_mm": round(self.height_mm, 3),
            "fits": self.fits,
            "score": round(self.score, 4),
            "notes": self.notes,
            "rejected_reason": self.rejected_reason,
        }

    @property
    def evidence(self) -> list[str]:
        """Short, numeric lines the plan can cite verbatim."""
        return [
            f"rotation {self.rotation_deg[0]:g},{self.rotation_deg[1]:g},{self.rotation_deg[2]:g} deg",
            f"bed contact {self.contact_area_mm2:.0f} mm2",
            f"overhang {self.overhang_area_mm2:.0f} mm2 ({self.overhang_percent:.1f}%)",
            f"height {self.height_mm:.1f} mm",
            f"stability {self.stability:.2f}",
        ]


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #


def rotation_matrix(rotation_deg: tuple[float, float, float]) -> np.ndarray:
    """Intrinsic XYZ rotation as a homogeneous 4x4, matching the candidate convention."""
    rx, ry, rz = (np.radians(float(v)) for v in rotation_deg)
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    mx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    my = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    mz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    matrix = np.eye(4)
    matrix[:3, :3] = mz @ my @ mx
    return matrix


def seat(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Bottom face on the plate, geometric centre on the origin in X/Y."""
    placed = mesh.copy()
    lower = placed.bounds[0]
    centre = (placed.bounds[0] + placed.bounds[1]) / 2.0
    placed.apply_translation([-float(centre[0]), -float(centre[1]), -float(lower[2])])
    return placed


def pose_mesh(mesh: trimesh.Trimesh, rotation_deg: tuple[float, float, float]) -> trimesh.Trimesh:
    """Rotate about the model's centre, then re-seat on the plate."""
    posed = mesh.copy()
    centre = (posed.bounds[0] + posed.bounds[1]) / 2.0
    posed.apply_translation(-centre)
    posed.apply_transform(rotation_matrix(rotation_deg))
    return seat(posed)


def bed_contact_area(mesh: trimesh.Trimesh) -> float:
    """Area of flat faces actually resting on the build plate.

    The face's outward normal points *down* when it rests on the plate (the
    solid is above it), so the test is on the magnitude of the Z component, not
    its sign. Top faces are excluded by the height test instead.
    """
    if len(mesh.faces) == 0:
        return 0.0
    centroids = mesh.triangles_center
    bed_z = float(mesh.bounds[0][2])
    on_bed = centroids[:, 2] <= bed_z + BED_TOLERANCE_MM
    flat = np.abs(mesh.face_normals[:, 2]) >= FLAT_NORMAL_Z
    keep = on_bed & flat
    return float(mesh.area_faces[keep].sum())


def footprint_area(mesh: trimesh.Trimesh) -> float:
    """XY footprint area of the pose, used to normalise stability."""
    extents = mesh.extents
    return float(max(extents[0], 1e-6) * max(extents[1], 1e-6))


def overhang_metrics(
    mesh: trimesh.Trimesh, *, threshold_deg: float = 45.0
) -> tuple[float, float, float]:
    """(overhang area, overhang percent, worst angle) for a seated pose.

    Same convention as the mesh analyzer: a face's overhang angle is measured
    from vertical, and faces resting on the plate are excluded because the bed
    supports them by definition.
    """
    if len(mesh.faces) == 0:
        return 0.0, 0.0, 0.0
    bed_z = float(mesh.bounds[0][2])
    on_bed = mesh.triangles_center[:, 2] <= bed_z + BED_TOLERANCE_MM
    nz = mesh.face_normals[:, 2]
    angles = np.degrees(np.arcsin(np.clip(-nz, -1.0, 1.0)))
    overhanging = (angles > threshold_deg) & (~on_bed)
    areas = mesh.area_faces
    reference = float(areas[~on_bed].sum()) or float(areas.sum())
    area = float(areas[overhanging].sum())
    percent = 100.0 * area / reference if reference > 0 else 0.0
    worst = float(angles[overhanging].max()) if np.any(overhanging) else 0.0
    return area, percent, worst


def bridge_candidates(
    mesh: trimesh.Trimesh,
    *,
    angle_deg: float = BRIDGE_ANGLE_DEG,
    max_span_mm: float = MAX_BRIDGEABLE_SPAN_MM,
) -> list[dict[str, float | bool]]:
    """Near-horizontal unsupported regions, grouped into spans.

    A face pointing almost straight down (`overhang angle >= 80 deg`) that is
    not on the bed is a ceiling. Connected ceilings are grouped, and each group
    is measured two ways: `span_mm` is the *shortest* distance across it —
    the direction filament would actually travel — and `bridgeable` says
    whether that distance is within what a slicer bridges unsupported.

    A large flat roof is not "no support needed" just because it is flat; it is
    only support-free while the gap it has to cross stays short.
    """
    if len(mesh.faces) == 0:
        return []
    bed_z = float(mesh.bounds[0][2])
    on_bed = mesh.triangles_center[:, 2] <= bed_z + BED_TOLERANCE_MM
    nz = mesh.face_normals[:, 2]
    angles = np.degrees(np.arcsin(np.clip(-nz, -1.0, 1.0)))
    ceiling = (angles >= angle_deg) & (~on_bed)
    if not np.any(ceiling):
        return []

    indices = np.flatnonzero(ceiling)
    adjacency = mesh.face_adjacency
    present = np.zeros(len(mesh.faces), dtype=bool)
    present[indices] = True
    keep = present[adjacency[:, 0]] & present[adjacency[:, 1]]

    import scipy.sparse as sp  # noqa: PLC0415 - trimesh already depends on scipy
    from scipy.sparse.csgraph import connected_components

    n = len(mesh.faces)
    if not np.any(keep):
        groups = [[int(i)] for i in indices]
    else:
        edges = adjacency[keep]
        graph = sp.coo_matrix(
            (np.ones(len(edges)), (edges[:, 0], edges[:, 1])), shape=(n, n)
        )
        count, labels = connected_components(graph, directed=False)
        groups = []
        for label in range(count):
            members = np.flatnonzero(labels == label)
            # Isolated nodes of *non*-ceiling faces are their own components;
            # only components made entirely of ceiling faces count.
            if members.size and bool(present[members].all()):
                groups.append(members.tolist())

    out: list[dict[str, float | bool]] = []
    for members in groups:
        triangles = mesh.triangles[members]
        flat = triangles.reshape(-1, 3)
        dx = float(np.ptp(flat[:, 0]))
        dy = float(np.ptp(flat[:, 1]))
        span = min(dx, dy) if min(dx, dy) > 1e-6 else max(dx, dy)
        out.append(
            {
                "span_mm": round(span, 3),
                "extent_x_mm": round(dx, 3),
                "extent_y_mm": round(dy, 3),
                "area_mm2": round(float(mesh.area_faces[members].sum()), 2),
                "bridgeable": bool(span <= max_span_mm),
            }
        )
    out.sort(key=lambda item: -float(item["span_mm"]))
    return out[:8]


def stability_factor(mesh: trimesh.Trimesh, contact_area_mm2: float) -> float:
    """0..1 tip-over resistance: contact radius over centre-of-mass height."""
    try:
        com = mesh.center_mass
    except Exception:  # pragma: no cover - non-volume meshes
        com = mesh.centroid
    height = abs(float(com[2]))
    if height <= 1e-6:
        return 1.0
    radius = float(np.sqrt(max(contact_area_mm2, 0.0) / np.pi))
    return float(max(0.0, min(1.0, radius / height)))


# --------------------------------------------------------------------------- #
# candidate generation
# --------------------------------------------------------------------------- #


def _axis_aligned_rotations() -> list[tuple[float, float, float]]:
    return [
        (0.0, 0.0, 0.0),
        (90.0, 0.0, 0.0),
        (-90.0, 0.0, 0.0),
        (0.0, 90.0, 0.0),
        (0.0, -90.0, 0.0),
        (180.0, 0.0, 0.0),
        (0.0, 0.0, 90.0),
        (0.0, 0.0, -90.0),
    ]


def _dominant_normals(mesh: trimesh.Trimesh, limit: int = _DOMINANT_NORMALS) -> list[np.ndarray]:
    """Normals of the largest planar regions, area weighted and de-duplicated."""
    if len(mesh.faces) == 0:
        return []
    normals = mesh.face_normals
    areas = mesh.area_faces
    # Quantise to a coarse grid so a curved surface contributes its dominant
    # directions rather than hundreds of unique unit vectors.
    keys = np.round(normals, 1)
    buckets: dict[tuple[float, float, float], float] = {}
    direction: dict[tuple[float, float, float], np.ndarray] = {}
    for normal, area in zip(keys, areas, strict=False):
        key = (float(normal[0]), float(normal[1]), float(normal[2]))
        buckets[key] = buckets.get(key, 0.0) + float(area)
        direction[key] = normal
    ranked = sorted(buckets.items(), key=lambda item: -item[1])
    out: list[np.ndarray] = []
    for key, _area in ranked:
        normal = direction[key]
        if not np.isfinite(normal).all() or np.linalg.norm(normal) < 1e-9:
            continue
        if any(float(np.dot(normal, existing)) > 0.95 for existing in out):
            continue
        out.append(normal / np.linalg.norm(normal))
        if len(out) >= limit:
            break
    return out


def _alignment_rotations(mesh: trimesh.Trimesh) -> list[tuple[float, float, float]]:
    """Rotations that lay a dominant flat face onto the bed."""
    rotations: list[tuple[float, float, float]] = []
    for normal in _dominant_normals(mesh):
        target = np.array([0.0, 0.0, -1.0])
        if float(np.dot(normal, target)) > 0.999:
            continue
        if float(np.dot(normal, target)) < -0.999:
            matrix = rotation_matrix((180.0, 0.0, 0.0))[:3, :3]
        else:
            matrix = trimesh.geometry.align_vectors(normal, target)[:3, :3]
        angles = _matrix_to_euler(matrix)
        rounded = tuple(round(v, 1) for v in angles)
        if rounded in rotations or rounded == (0.0, 0.0, 0.0):
            continue
        rotations.append(rounded)  # type: ignore[arg-type]
    return rotations


def _matrix_to_euler(matrix: np.ndarray) -> tuple[float, float, float]:
    """XYZ euler angles from a rotation matrix, in degrees."""
    sy = -float(matrix[2, 0])
    sy = max(-1.0, min(1.0, sy))
    if abs(sy) < 0.99999:
        ry = np.arcsin(sy)
        rx = np.arctan2(matrix[2, 1], matrix[2, 2])
        rz = np.arctan2(matrix[1, 0], matrix[0, 0])
    else:
        ry = np.copysign(np.pi / 2, sy)
        rx = np.arctan2(-matrix[1, 2], matrix[1, 1])
        rz = 0.0
    return (float(np.degrees(rx)), float(np.degrees(ry)), float(np.degrees(rz)))


def _strategy_for(rotation: tuple[float, float, float]) -> str:
    if rotation == (0.0, 0.0, 0.0):
        return "as_imported"
    rx, ry, rz = rotation
    if abs(rx) in (90.0, 180.0) or abs(ry) in (90.0, 180.0):
        return "largest_face_down"
    if rz:
        return "yaw_to_bed"
    return "face_aligned"


def generate_candidates(
    mesh: trimesh.Trimesh,
    *,
    build_volume: tuple[float, float, float],
    max_overhang_deg: float = 45.0,
    limit: int = _MAX_CANDIDATES,
) -> list[OrientationCandidate]:
    """Measure every candidate pose and rank the ones that fit."""
    rotations: list[tuple[float, float, float]] = []
    for rotation in _axis_aligned_rotations() + _alignment_rotations(mesh):
        if rotation not in rotations:
            rotations.append(rotation)

    bx, by, bz = build_volume
    measured: list[OrientationCandidate] = []

    for rotation in rotations[: max(limit * 2, limit)]:
        posed = pose_mesh(mesh, rotation)
        extents = tuple(float(v) for v in posed.extents)
        contact = bed_contact_area(posed)
        overhang_area, overhang_percent, _worst = overhang_metrics(
            posed, threshold_deg=max_overhang_deg
        )
        foot = footprint_area(posed)
        stability = stability_factor(posed, contact)
        fits = (extents[0] <= bx + 1e-6 and extents[1] <= by + 1e-6 and extents[2] <= bz + 1e-6)

        notes: list[str] = []
        reason: str | None = None
        if not fits:
            reason = (
                f"Extents {extents[0]:.1f} x {extents[1]:.1f} x {extents[2]:.1f} mm "
                f"exceed the {bx:.0f} x {by:.0f} x {bz:.0f} mm build volume."
            )
            notes.append(reason)

        measured.append(
            OrientationCandidate(
                id=_candidate_id(rotation),
                rotation_deg=rotation,
                strategy=_strategy_for(rotation),
                extents_mm=extents,
                contact_area_mm2=contact,
                overhang_area_mm2=overhang_area,
                overhang_percent=overhang_percent,
                support_area_mm2=overhang_area,
                footprint_mm2=foot,
                stability=stability,
                height_mm=extents[2],
                fits=fits,
                notes=notes,
                rejected_reason=reason,
            )
        )

    scored = _score(measured)
    scored.sort(key=lambda c: (-c.score, c.id))
    return scored[:limit]


def _candidate_id(rotation: tuple[float, float, float]) -> str:
    parts = ["rot", *(f"{int(round(v)):+d}" if abs(v - round(v)) < 1e-6 else f"{v:+.1f}" for v in rotation)]
    return "_".join(parts)


def _score(candidates: list[OrientationCandidate]) -> list[OrientationCandidate]:
    """Weighted rank over the measurable objectives.

    Weights are fixed rather than learned: overhang reduction is what support
    material costs, contact area is what adhesion and tip-over risk cost, and
    height is what print time costs. Nothing here reads the user's objective,
    because the same evidence should score the same way every time and the
    *choice* between them is the model's to make.
    """
    usable = [c for c in candidates if c.fits]
    if not usable:
        # Nothing fits. Every candidate keeps score 0 and keeps its
        # `rejected_reason`, so the caller sees a refusal rather than a ranking.
        return list(candidates)

    max_overhang = max((c.overhang_area_mm2 for c in usable), default=1.0) or 1.0
    max_contact = max((c.contact_area_mm2 for c in usable), default=1.0) or 1.0
    max_height = max((c.height_mm for c in usable), default=1.0) or 1.0

    out: list[OrientationCandidate] = []
    for candidate in candidates:
        if not candidate.fits:
            out.append(candidate)
            continue
        overhang_score = 1.0 - (candidate.overhang_area_mm2 / max_overhang)
        contact_score = candidate.contact_area_mm2 / max_contact
        height_score = 1.0 - (candidate.height_mm / max_height)
        score = (
            0.40 * overhang_score
            + 0.25 * contact_score
            + 0.20 * height_score
            + 0.15 * candidate.stability
        )
        out.append(
            OrientationCandidate(
                id=candidate.id,
                rotation_deg=candidate.rotation_deg,
                strategy=candidate.strategy,
                extents_mm=candidate.extents_mm,
                contact_area_mm2=candidate.contact_area_mm2,
                overhang_area_mm2=candidate.overhang_area_mm2,
                overhang_percent=candidate.overhang_percent,
                support_area_mm2=candidate.support_area_mm2,
                footprint_mm2=candidate.footprint_mm2,
                stability=candidate.stability,
                height_mm=candidate.height_mm,
                fits=candidate.fits,
                score=round(score, 4),
                notes=candidate.notes,
                rejected_reason=candidate.rejected_reason,
            )
        )
    return out


def verify_candidate(
    candidate_id: str, candidates: list[OrientationCandidate]
) -> OrientationCandidate:
    """Refuse an id the deterministic analysis did not produce.

    This is the gate that stops the model inventing a rotation: the id must
    exist in the generated set *and* that pose must fit the build volume.
    """
    match = next((c for c in candidates if c.id == candidate_id), None)
    if match is None:
        known = [c.id for c in candidates]
        raise ValidationFailedError(
            f"Orientation '{candidate_id}' was not one of the analysed candidates.",
            code="ORIENTATION_UNKNOWN",
            details={"requested": candidate_id, "available": known},
        )
    if not match.fits:
        raise ValidationFailedError(
            f"Orientation '{candidate_id}' does not fit the build volume: "
            f"{match.rejected_reason}",
            code="ORIENTATION_DOES_NOT_FIT",
            details={"candidate": match.to_dict()},
        )
    return match
