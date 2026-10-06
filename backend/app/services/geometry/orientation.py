"""Deterministic orientation service.

Provides candidate orientation generation, pose measurement, and validation.
No model generates orientation rotations; candidates are computed deterministically
from the geometry and scored against stability, contact area, and overhang metrics.
"""

from __future__ import annotations

from app.preparation.orientation import (
    BED_TOLERANCE_MM,
    BRIDGE_ANGLE_DEG,
    FLAT_NORMAL_Z,
    MAX_BRIDGEABLE_SPAN_MM,
    ORIENTATION_VERSION,
    OrientationCandidate,
    bed_contact_area,
    bridge_candidates,
    footprint_area,
    generate_candidates,
    overhang_metrics,
    pose_mesh,
    rotation_matrix,
    seat,
    stability_factor,
    verify_candidate,
)

__all__ = [
    "BED_TOLERANCE_MM",
    "BRIDGE_ANGLE_DEG",
    "FLAT_NORMAL_Z",
    "MAX_BRIDGEABLE_SPAN_MM",
    "ORIENTATION_VERSION",
    "OrientationCandidate",
    "bed_contact_area",
    "bridge_candidates",
    "footprint_area",
    "generate_candidates",
    "overhang_metrics",
    "pose_mesh",
    "rotation_matrix",
    "seat",
    "stability_factor",
    "verify_candidate",
]
