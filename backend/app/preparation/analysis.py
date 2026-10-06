"""Deterministic print preparation analysis.

This is the layer the language model never touches. It runs the mesh
analyzer, enumerates and measures every orientation candidate, checks the
recommended pose against the machine's own coordinate frame, and probes what
the selected engine can actually do. Everything it produces is a measurement
or a fact about the engine - no prediction, no prose, no opinion about what
the user should print.

The result is the single input the planner turns into an LLM prompt *and* the
single source of truth every later validator re-reads. The checks in
`validators.py` do not re-derive any of this: they read it back, so the numbers
shown in the "AI PRINT CHECK" are necessarily the numbers that were used to
decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import trimesh

from app.core.errors import ValidationFailedError
from app.core.logging import get_logger
from app.preparation.capabilities import EngineCapabilities, probe_capabilities
from app.preparation.orientation import (
    OrientationCandidate,
    bridge_candidates,
    generate_candidates,
    overhang_metrics,
    pose_mesh,
    seat,
)
from app.preparation.placement import BedPlacementResult, verify_bed_placement
from app.services.geometry.analyzer import MeshAnalysisReport, analyze_mesh, load_mesh
from app.slicing.validator import MachineLimits

log = get_logger(__name__)

ANALYSIS_VERSION = "print-preparation-analysis-1.0.0"
DEFAULT_MAX_OVERHANG_DEG = 45.0
DEFAULT_ORIENTATION_LIMIT = 10
#: A bed contact patch smaller than this is not a usable first layer.
MIN_CONTACT_AREA_MM2 = 1.0


@dataclass(frozen=True)
class SupportRequirement:
    """Where support material would be needed at a given pose."""

    required: bool
    overhang_area_mm2: float
    overhang_percent: float
    support_area_mm2: float
    longest_bridge_mm: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "overhang_area_mm2": round(self.overhang_area_mm2, 2),
            "overhang_percent": round(self.overhang_percent, 3),
            "support_area_mm2": round(self.support_area_mm2, 2),
            "longest_bridge_mm": round(self.longest_bridge_mm, 3),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AdhesionAssessment:
    """How risky the first layer is, measured from the seated pose."""

    contact_area_mm2: float
    footprint_area_mm2: float
    contact_ratio: float
    stability: float
    recommended: str
    reason: str
    candidates: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contact_area_mm2": round(self.contact_area_mm2, 2),
            "footprint_area_mm2": round(self.footprint_area_mm2, 2),
            "contact_ratio": round(self.contact_ratio, 4),
            "stability": round(self.stability, 4),
            "recommended": self.recommended,
            "reason": self.reason,
            "candidates": list(self.candidates),
        }


@dataclass(frozen=True)
class PreAnalysis:
    """Everything deterministic, decided before a single token is generated."""

    version: str
    mesh_path: str
    input_sha256: str
    report: MeshAnalysisReport
    capabilities: EngineCapabilities
    limits: MachineLimits
    #: Every measured pose, best score first.
    candidates: list[OrientationCandidate]
    #: The id the analysis recommends. Still only a recommendation.
    recommended_candidate_id: str
    selected: OrientationCandidate
    placement: BedPlacementResult
    support: SupportRequirement
    adhesion: AdhesionAssessment
    bridges: list[dict[str, float | bool]]
    #: The mesh, already seated on the origin, so a *different* pose the model
    #: picks can be re-measured without reloading the file. Not serialised.
    mesh: Any = None
    #: Findings from the mesh analyzer that block a print outright.
    blocking_findings: list[str] = field(default_factory=list)
    max_overhang_deg: float = DEFAULT_MAX_OVERHANG_DEG
    orientation_limit: int = DEFAULT_ORIENTATION_LIMIT
    #: Display name of the machine this was measured against, for the prompt.
    machine_name: str = "machine"

    # -- derived views ----------------------------------------------------

    def candidate_by_id(self, candidate_id: str) -> OrientationCandidate | None:
        return next((c for c in self.candidates if c.id == candidate_id), None)

    @property
    def fits(self) -> bool:
        return self.placement.passed

    @property
    def geometry_summary(self) -> dict[str, Any]:
        m = self.report.metrics
        return {
            "dimensions_mm": {k: round(v, 3) for k, v in m.dimensions_mm.items()},
            "triangle_count": m.triangle_count,
            "volume_mm3": round(m.volume_mm3, 2) if m.volume_mm3 else None,
            "surface_area_mm2": round(m.surface_area_mm2, 2) if m.surface_area_mm2 else None,
            "is_watertight": m.is_watertight,
            "is_winding_consistent": m.is_winding_consistent,
            "body_count": m.body_count,
            "non_manifold_edge_count": m.non_manifold_edge_count,
            "boundary_edge_count": m.boundary_edge_count,
            "degenerate_face_count": m.degenerate_face_count,
            "min_wall_mm": (
                round(m.estimated_min_wall_mm, 4)
                if m.estimated_min_wall_mm is not None
                else None
            ),
            "printability_score": round(self.report.printability_score, 3),
            "risk_level": self.report.risk_level,
            "finding_counts": self.report.score_breakdown.get("counts_by_severity", {}),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_version": self.version,
            "mesh_path": self.mesh_path,
            "input_sha256": self.input_sha256,
            "max_overhang_deg": self.max_overhang_deg,
            "geometry": self.geometry_summary,
            "capabilities": self.capabilities.to_dict(),
            "machine": {
                "build_volume_mm": {
                    "x": self.limits.build_x,
                    "y": self.limits.build_y,
                    "z": self.limits.build_z,
                },
                "origin_convention": self.limits.origin_convention,
                "nozzle_diameter_mm": self.limits.nozzle_diameter_mm,
                "temperature_limits": {
                    "nozzle_min_c": self.limits.nozzle_min_c,
                    "nozzle_max_c": self.limits.nozzle_max_c,
                    "bed_min_c": self.limits.bed_min_c,
                    "bed_max_c": self.limits.bed_max_c,
                },
                "speed_limits": {
                    "max_print_speed_mm_s": self.limits.max_print_speed_mm_s,
                    "max_travel_speed_mm_s": self.limits.max_travel_speed_mm_s,
                    "max_acceleration_mm_s2": self.limits.max_acceleration_mm_s2,
                    "max_volumetric_flow_mm3s": self.limits.max_volumetric_flow_mm3s,
                },
            },
            "orientation": {
                "recommended_candidate_id": self.recommended_candidate_id,
                "candidates": [c.to_dict() for c in self.candidates],
            },
            "selected_pose": self.selected.to_dict(),
            "placement": self.placement.to_dict(),
            "support": self.support.to_dict(),
            "adhesion": self.adhesion.to_dict(),
            "bridges": self.bridges,
            "blocking_findings": self.blocking_findings,
            "findings": [f.to_dict() for f in self.report.findings],
        }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _support_requirement(
    mesh: trimesh.Trimesh, *, max_overhang_deg: float, threshold_ratio: float = 0.02
) -> SupportRequirement:
    """Decide whether this pose needs support, from measured areas only.

    The rule is mechanical: overhang above the angle threshold that is not
    bridged within `MAX_BRIDGE` is unsupported surface. Where the slicer can
    bridge instead of supporting is engine behaviour, so the bridge list is
    reported alongside and the plan states both numbers.
    """
    overhang_area, percent, _worst = overhang_metrics(mesh, threshold_deg=max_overhang_deg)
    bridges = bridge_candidates(mesh)
    longest = bridges[0]["span_mm"] if bridges else 0.0

    if overhang_area <= MIN_CONTACT_AREA_MM2:
        return SupportRequirement(
            required=False,
            overhang_area_mm2=overhang_area,
            overhang_percent=percent,
            support_area_mm2=0.0,
            longest_bridge_mm=longest,
            reason=(
                f"Overhang area {overhang_area:.1f} mm2 is below the detection "
                f"floor at {max_overhang_deg:g} deg."
            ),
        )

    # Everything the slicer cannot bridge has to be supported. A ceiling is
    # only free of support while the gap it must cross stays short, so the
    # bridge list is consulted per region rather than as a whole.
    bridgeable_area = sum(
        float(item["area_mm2"]) for item in bridges if bool(item["bridgeable"])
    )
    unsupported_area = max(overhang_area - bridgeable_area, 0.0)
    total_area = float(mesh.area) or 1.0
    ratio = unsupported_area / total_area

    required = ratio > threshold_ratio
    if required:
        reason = (
            f"{unsupported_area:.0f} mm2 ({100.0 * ratio:.1f}% of the surface) is "
            f"unsupported at {max_overhang_deg:g} deg; longest span {longest:.1f} mm."
        )
    else:
        reason = (
            f"Unsupported area {unsupported_area:.0f} mm2 ({100.0 * ratio:.1f}%) is within "
            "the threshold the slicer can bridge."
        )
    return SupportRequirement(
        required=required,
        overhang_area_mm2=overhang_area,
        overhang_percent=percent,
        support_area_mm2=unsupported_area,
        longest_bridge_mm=longest,
        reason=reason,
    )


def _adhesion_assessment(selected: OrientationCandidate) -> AdhesionAssessment:
    """Choose an adhesion recommendation from measured contact, not vibes."""
    footprint = max(selected.footprint_mm2, 1e-6)
    ratio = selected.contact_area_mm2 / footprint

    if selected.contact_area_mm2 < MIN_CONTACT_AREA_MM2:
        recommended = "raft"
        reason = (
            f"Bed contact is {selected.contact_area_mm2:.1f} mm2 - effectively no "
            "first layer. A raft is the only reliable start."
        )
        candidates = ("raft", "brim", "none")
    elif ratio < 0.05 or selected.stability < 0.35:
        recommended = "brim"
        reason = (
            f"Contact covers {100.0 * ratio:.1f}% of the footprint with stability "
            f"{selected.stability:.2f}; a brim widens the anchor."
        )
        candidates = ("brim", "raft", "none")
    elif ratio < 0.25:
        recommended = "brim"
        reason = (
            f"Contact covers {100.0 * ratio:.1f}% of the footprint; a brim reduces "
            "corner lift on a narrow base."
        )
        candidates = ("brim", "none", "raft")
    else:
        recommended = "none"
        reason = (
            f"Contact covers {100.0 * ratio:.1f}% of the footprint with stability "
            f"{selected.stability:.2f}; extra adhesion is unnecessary."
        )
        candidates = ("none", "brim", "skirt")

    return AdhesionAssessment(
        contact_area_mm2=selected.contact_area_mm2,
        footprint_area_mm2=selected.footprint_mm2,
        contact_ratio=ratio,
        stability=selected.stability,
        recommended=recommended,
        reason=reason,
        candidates=candidates,
    )


def _blocking_findings(report: MeshAnalysisReport) -> list[str]:
    """Analyzer findings that stop a job before the model is consulted."""
    blockers: list[str] = []
    for finding in report.findings:
        if finding.severity != "critical":
            continue
        blockers.append(f"{finding.code}: {finding.message}")
    m = report.metrics
    if not m.is_watertight:
        blockers.append("GEOMETRY_NOT_WATERTIGHT: The mesh has holes; slicing it is unsafe.")
    if m.body_count > 1:
        blockers.append(
            f"GEOMETRY_MULTIPLE_BODIES: The mesh contains {m.body_count} separate bodies."
        )
    return blockers


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def run_pre_analysis(
    mesh_path: str | Path,
    *,
    limits: MachineLimits,
    adapter: Any,
    input_sha256: str,
    max_overhang_deg: float = DEFAULT_MAX_OVERHANG_DEG,
    orientation_limit: int = DEFAULT_ORIENTATION_LIMIT,
    no_opening_mm: float = 0.4,
    file_type: str | None = None,
) -> PreAnalysis:
    """Measure the model against the machine and the engine.

    Raises only for conditions where no honest analysis is possible: an
    unreadable mesh, or a machine with no build volume. Every other problem
    (does not fit, needs support, has holes) is *reported*, because the plan
    has to be able to say them out loud.
    """
    path = Path(mesh_path)
    if not path.is_file():
        raise ValidationFailedError(
            f"Model file not found: {path}",
            code="MODEL_FILE_NOT_FOUND",
            details={"path": str(path)},
        )
    if limits.build_x <= 0 or limits.build_y <= 0 or limits.build_z <= 0:
        raise ValidationFailedError(
            "The machine profile has no build volume, so placement cannot be checked.",
            code="BUILD_VOLUME_UNKNOWN",
            details={
                "build_volume": [limits.build_x, limits.build_y, limits.build_z],
            },
        )

    mesh = load_mesh(str(path), file_type=file_type)
    if len(mesh.faces) == 0:
        raise ValidationFailedError(
            "The model contains no triangles.",
            code="GEOMETRY_EMPTY",
            details={"path": str(path)},
        )

    report = analyze_mesh(
        mesh,
        input_sha256=input_sha256,
        no_opening_mm=no_opening_mm,
        max_overhang_deg=max_overhang_deg,
    )
    capabilities = probe_capabilities(adapter)

    # Orientation is measured on the model as imported. The analyzer's own
    # overhang numbers describe the same pose, so the two agree by construction.
    seated = seat(mesh)
    build_volume = (limits.build_x, limits.build_y, limits.build_z)
    candidates = generate_candidates(
        seated,
        build_volume=build_volume,
        max_overhang_deg=max_overhang_deg,
        limit=orientation_limit,
    )
    if not candidates:
        raise ValidationFailedError(
            "No orientation candidates could be generated for this model.",
            code="ORIENTATION_GENERATION_FAILED",
            details={"triangles": int(len(mesh.faces))},
        )

    recommended = candidates[0]
    selected = recommended
    placement = verify_bed_placement(pose_mesh(seated, selected.rotation_deg), limits)

    pose = pose_mesh(seated, selected.rotation_deg)
    support = _support_requirement(pose, max_overhang_deg=max_overhang_deg)
    adhesion = _adhesion_assessment(selected)

    analysis = PreAnalysis(
        version=ANALYSIS_VERSION,
        mesh_path=str(path),
        input_sha256=input_sha256,
        report=report,
        capabilities=capabilities,
        limits=limits,
        candidates=candidates,
        recommended_candidate_id=recommended.id,
        selected=selected,
        placement=placement,
        support=support,
        adhesion=adhesion,
        bridges=bridge_candidates(pose),
        mesh=seated,
        blocking_findings=_blocking_findings(report),
        max_overhang_deg=max_overhang_deg,
        orientation_limit=orientation_limit,
    )
    log.info(
        "preparation.analysed",
        sha=input_sha256[:12],
        candidates=len(candidates),
        recommended=recommended.id,
        fits=placement.passed,
        support=support.required,
        blocking=len(analysis.blocking_findings),
    )
    return analysis


# --------------------------------------------------------------------------- #
# re-evaluating a pose the model actually chose
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PoseFacts:
    """Everything that is true *about one orientation*, for the validators.

    The `PreAnalysis` carries these facts for its recommended pose, but the
    model is free to choose a different candidate — and support requirement,
    bed contact and placement all change with the pose. Re-measuring the chosen
    one is what stops the plan being checked against the wrong orientation.
    """

    candidate: OrientationCandidate
    support: SupportRequirement
    adhesion: AdhesionAssessment
    placement: BedPlacementResult
    bridges: list[dict[str, float | bool]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "support": self.support.to_dict(),
            "adhesion": self.adhesion.to_dict(),
            "placement": self.placement.to_dict(),
            "bridges": self.bridges,
        }


def evaluate_pose(analysis: PreAnalysis, candidate_id: str | None) -> PoseFacts:
    """Measure the pose the plan selected, or fall back to the recommendation.

    An id the generator never produced resolves to the recommended pose: the
    orientation check reports that failure separately, and every other check
    still needs *some* measured pose to compare against.
    """
    candidate = analysis.candidate_by_id(candidate_id) if candidate_id else None
    if candidate is None or candidate.id == analysis.selected.id:
        return PoseFacts(
            candidate=analysis.selected,
            support=analysis.support,
            adhesion=analysis.adhesion,
            placement=analysis.placement,
            bridges=analysis.bridges,
        )
    if analysis.mesh is None:  # pragma: no cover - defensive
        return PoseFacts(
            candidate=analysis.selected,
            support=analysis.support,
            adhesion=analysis.adhesion,
            placement=analysis.placement,
            bridges=analysis.bridges,
        )

    pose = pose_mesh(analysis.mesh, candidate.rotation_deg)
    support = _support_requirement(pose, max_overhang_deg=analysis.max_overhang_deg)
    return PoseFacts(
        candidate=candidate,
        support=support,
        adhesion=_adhesion_assessment(candidate),
        placement=verify_bed_placement(pose, analysis.limits),
        bridges=bridge_candidates(pose),
    )
