"""Deterministic mesh validation service.

Validates 3D meshes against printability and machine geometric constraints.
No LLM or heuristic guessing is involved: all checks are derived directly
from triangle geometry via `trimesh` and the mesh analyzer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from app.services.geometry.analyzer import (
    GeometryMetrics,
    MeshAnalysisReport,
    analyse_file,
    analyze_mesh,
    load_mesh,
)

VALIDATOR_VERSION = "mesh-validation-1.0.0"


@dataclass(frozen=True)
class MeshValidationIssue:
    """A concrete defect or warning found during mesh validation."""

    code: str
    severity: str  # "critical" | "warning" | "info"
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class MeshValidationResult:
    """The deterministic verdict of a mesh validation run."""

    valid: bool
    can_slice: bool
    issues: list[MeshValidationIssue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    build_volume_fits: bool | None = None
    repair_recommended: bool = False
    validator_version: str = VALIDATOR_VERSION

    @property
    def critical_issues(self) -> list[MeshValidationIssue]:
        return [i for i in self.issues if i.severity == "critical"]

    @property
    def warning_issues(self) -> list[MeshValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "can_slice": self.can_slice,
            "build_volume_fits": self.build_volume_fits,
            "repair_recommended": self.repair_recommended,
            "critical_count": len(self.critical_issues),
            "warning_count": len(self.warning_issues),
            "issues": [i.to_dict() for i in self.issues],
            "metrics": self.metrics,
            "validator_version": self.validator_version,
        }


def validate_mesh_report(
    report: MeshAnalysisReport,
    *,
    build_volume: tuple[float, float, float] | None = None,
) -> MeshValidationResult:
    """Validate a pre-computed MeshAnalysisReport against printability rules."""
    metrics = report.metrics
    issues: list[MeshValidationIssue] = []

    # 1. Watertightness
    if not metrics.is_watertight:
        issues.append(
            MeshValidationIssue(
                code="NOT_WATERTIGHT",
                severity="critical",
                message="Mesh is not watertight (open boundaries detected). Slicers cannot determine solid interior.",
                details={"boundary_edge_count": metrics.boundary_edge_count},
            )
        )

    # 2. Non-manifold edges
    if metrics.non_manifold_edge_count > 0:
        issues.append(
            MeshValidationIssue(
                code="NON_MANIFOLD_EDGES",
                severity="critical",
                message=f"Mesh has {metrics.non_manifold_edge_count} non-manifold edges.",
                details={"non_manifold_edge_count": metrics.non_manifold_edge_count},
            )
        )

    # 3. Volume
    if metrics.volume_mm3 is None or metrics.volume_mm3 <= 0:
        issues.append(
            MeshValidationIssue(
                code="INVALID_VOLUME",
                severity="critical",
                message="Mesh does not enclose a valid positive volume.",
                details={"volume_mm3": metrics.volume_mm3},
            )
        )

    # 4. Degenerate faces
    if metrics.degenerate_face_count > 0:
        issues.append(
            MeshValidationIssue(
                code="DEGENERATE_FACES",
                severity="warning" if metrics.degenerate_face_count < 50 else "critical",
                message=f"Mesh contains {metrics.degenerate_face_count} zero-area or degenerate triangles.",
                details={"degenerate_face_count": metrics.degenerate_face_count},
            )
        )

    # 5. Inverted normals / inconsistent winding
    if not metrics.is_winding_consistent or metrics.inverted_normal_count > 0:
        issues.append(
            MeshValidationIssue(
                code="INCONSISTENT_NORMALS",
                severity="warning",
                message="Mesh has inconsistent triangle winding or inverted normals.",
                details={
                    "is_winding_consistent": metrics.is_winding_consistent,
                    "inverted_normal_count": metrics.inverted_normal_count,
                },
            )
        )

    # 6. Multi-body warning
    if metrics.body_count > 1:
        issues.append(
            MeshValidationIssue(
                code="MULTI_BODY",
                severity="info",
                message=f"Model contains {metrics.body_count} disconnected shells or bodies.",
                details={"body_count": metrics.body_count},
            )
        )

    # 7. Thin walls from analyzer findings
    for finding in report.findings:
        if finding.code == "THIN_WALL":
            issues.append(
                MeshValidationIssue(
                    code="THIN_WALL",
                    severity=finding.severity,
                    message=finding.message,
                    details={
                        "measured_value": finding.measured_value,
                        "limit_value": finding.limit_value,
                    },
                )
            )

    # 8. Build volume fit check (if provided)
    build_fits: bool | None = None
    if build_volume is not None:
        bx, by, bz = build_volume
        dims = metrics.dimensions_mm
        fits_x = dims["x"] <= bx + 1e-6
        fits_y = dims["y"] <= by + 1e-6
        fits_z = dims["z"] <= bz + 1e-6
        build_fits = bool(fits_x and fits_y and fits_z)
        if not build_fits:
            issues.append(
                MeshValidationIssue(
                    code="EXCEEDS_BUILD_VOLUME",
                    severity="critical",
                    message=(
                        f"Model dimensions ({dims['x']:.1f} x {dims['y']:.1f} x {dims['z']:.1f} mm) "
                        f"exceed the build volume ({bx:.1f} x {by:.1f} x {bz:.1f} mm)."
                    ),
                    details={
                        "model_dimensions": dims,
                        "build_volume": {"x": bx, "y": by, "z": bz},
                    },
                )
            )

    has_critical = any(i.severity == "critical" for i in issues)
    valid = len(issues) == 0
    can_slice = not has_critical
    repair_recommended = not metrics.is_watertight or metrics.non_manifold_edge_count > 0 or metrics.degenerate_face_count > 0

    return MeshValidationResult(
        valid=valid,
        can_slice=can_slice,
        issues=issues,
        metrics=metrics.to_dict(),
        build_volume_fits=build_fits,
        repair_recommended=repair_recommended,
    )


def validate_mesh(
    mesh_or_path: trimesh.Trimesh | str | Path,
    *,
    build_volume: tuple[float, float, float] | None = None,
    nozzle_diameter_mm: float = 0.4,
) -> MeshValidationResult:
    """Validate a mesh object or file path deterministically."""
    if isinstance(mesh_or_path, trimesh.Trimesh):
        mesh = mesh_or_path
        report = analyze_mesh(mesh, no_opening_mm=nozzle_diameter_mm)
    else:
        path = Path(mesh_or_path)
        import hashlib
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        report = analyse_file(str(path), input_sha256=sha, no_opening_mm=nozzle_diameter_mm)

    return validate_mesh_report(report, build_volume=build_volume)
