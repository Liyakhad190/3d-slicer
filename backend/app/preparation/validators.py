"""Deterministic validation of an AI print plan.

The rule this module implements is one sentence long: **the model may propose,
the deterministic system must validate.** Everything a plan claims is checked
against something that was measured or read from a profile — the mesh analyzer,
the orientation engine, the machine's thermal envelope, the material's ranges,
the engine's own settings schema. Nothing is taken on the model's word.

Three outcomes are possible for every checklist category:

``PASS``
    The claim agrees with the measurement.
``WARNING``
    The claim is usable but disagrees with something. The measurement wins,
    the disagreement is recorded as a `Normalisation`, and the user sees it.
``FAILED``
    The claim would produce an unsafe or unprintable job. It is never applied
    silently; it either triggers a replan or blocks the job.

``UNSUPPORTED`` and ``NOT_REQUIRED`` are the model's own claims, and both are
verified: "not required" is checked against the measurement, "unsupported" is
checked against the engine's schema.

Each `Check` also carries whether it is *correctable* (a better answer could
fix it, so a replan is worth attempting) and *downgradable* (after the final
attempt the platform would rather print with a recorded correction than refuse
entirely). Temperature and geometry are never downgraded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.logging import get_logger
from app.intelligence.schemas import Normalisation
from app.preparation.analysis import MIN_CONTACT_AREA_MM2, PoseFacts, PreAnalysis, evaluate_pose
from app.preparation.schemas import CHECKLIST, AiPrintPlan
from app.slicing.cura_settings import _INFILL_MAP

log = get_logger(__name__)

VALIDATOR_VERSION = "print-plan-validator-1.0.0"

CheckStatus = Literal["PASS", "WARNING", "FAILED", "NOT_REQUIRED", "UNSUPPORTED"]
Objective = Literal["quality", "strength", "speed", "material", "balanced"]

#: Material constraints the plan is allowed to claim it honoured. Anything
#: else is not a fact about a material and is dropped as unsupported.
HONOURABLE_MATERIAL_FACTS: frozenset[str] = frozenset(
    {
        "temperature_range",
        "nozzle_temperature",
        "bed_temperature",
        "first_layer_temperature",
        "max_volumetric_flow",
        "print_speed",
        "retraction",
        "layer_height_range",
        "cooling",
        "adhesion",
        "abrasiveness",
        "enclosure",
        "diameter",
        "flexibility",
    }
)

#: Adhesion modes this platform can express as a canonical parameter.
ADHESION_MODES = frozenset({"none", "brim", "raft"})


@dataclass
class Check:
    """One checklist row, with the numbers that produced its status."""

    category: str
    status: CheckStatus
    code: str
    reason: str
    evidence: list[str] = field(default_factory=list)
    #: A better answer from the model could fix this.
    correctable: bool = False
    #: After the last attempt the platform accepts the claim with a recorded
    #: correction rather than refusing the job. Temperature and geometry are
    #: never downgradable.
    downgradable: bool = False
    #: Values the deterministic resolver substitutes for the model's, or `None`.
    normalisation: Normalisation | list[Normalisation] | None = None

    @property
    def corrections(self) -> list[Normalisation]:
        """Every correction this check contributes, as a flat list."""
        if self.normalisation is None:
            return []
        if isinstance(self.normalisation, list):
            return list(self.normalisation)
        return [self.normalisation]

    @property
    def failed(self) -> bool:
        return self.status == "FAILED"

    @property
    def warned(self) -> bool:
        return self.status == "WARNING"

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "status": self.status,
            "code": self.code,
            "reason": self.reason,
            "evidence": self.evidence,
            "correctable": self.correctable,
            "downgradable": self.downgradable,
            "normalisations": [n.to_dict() for n in self.corrections],
        }


@dataclass
class ValidationOutcome:
    """Every check, plus the parameters the plan is allowed to set."""

    checks: list[Check]
    #: Keys the existing optimizer receives as `locked`.
    locked: dict[str, Any] = field(default_factory=dict)
    #: Canonical keys the plan owns directly, merged over the optimizer output.
    overrides: dict[str, Any] = field(default_factory=dict)
    normalisations: list[Normalisation] = field(default_factory=list)
    version: str = VALIDATOR_VERSION

    # -- views -----------------------------------------------------------

    @property
    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    @property
    def warning_checks(self) -> list[Check]:
        return [c for c in self.checks if c.warned]

    @property
    def passed(self) -> bool:
        return not self.failed_checks

    @property
    def can_replan(self) -> bool:
        """A replan is only worth attempting if every failure is fixable."""
        return bool(self.failed_checks) and all(c.correctable for c in self.failed_checks)

    @property
    def blocking_codes(self) -> list[str]:
        return [c.code for c in self.failed_checks if not c.correctable]

    def category(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.category == name), None)

    def finalise(self) -> ValidationOutcome:
        """Last attempt: downgrade what we are willing to correct, keep the rest.

        Returns a new outcome. The checks that survive as `FAILED` are the ones
        the platform refuses to print under — a temperature outside the safe
        band, a model that does not fit, a mesh with holes.
        """
        if not self.failed_checks:
            return self

        kept: list[Check] = []
        corrections = list(self.normalisations)
        locked = dict(self.locked)
        overrides = dict(self.overrides)

        for check in self.checks:
            if check.failed and check.downgradable:
                fixes = check.corrections
                kept.append(
                    Check(
                        category=check.category,
                        status="WARNING",
                        code=check.code,
                        reason=f"{check.reason} Corrected deterministically on the final attempt.",
                        evidence=check.evidence,
                        correctable=False,
                        downgradable=False,
                        normalisation=fixes,
                    )
                )
                corrections.extend(fixes)
                for fix in fixes:
                    _apply_override(locked, overrides, fix)
            else:
                kept.append(check)

        return ValidationOutcome(
            checks=kept,
            locked=locked,
            overrides=overrides,
            normalisations=corrections,
            version=self.version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "validator_version": self.version,
            "passed": self.passed,
            "can_replan": self.can_replan,
            "summary": {
                "total": len(self.checks),
                "pass": sum(1 for c in self.checks if c.status == "PASS"),
                "warning": len(self.warning_checks),
                "failed": len(self.failed_checks),
                "not_required": sum(1 for c in self.checks if c.status == "NOT_REQUIRED"),
                "unsupported": sum(1 for c in self.checks if c.status == "UNSUPPORTED"),
            },
            "checks": [c.to_dict() for c in self.checks],
            "locked": _jsonable(self.locked),
            "overrides": _jsonable(self.overrides),
            "normalisations": [n.to_dict() for n in self.normalisations],
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _apply_override(
    locked: dict[str, Any], overrides: dict[str, Any], correction: Normalisation | None
) -> None:
    """Move a corrected value into the parameter set it belongs to.

    Only a key the plan actually proposed can be corrected, so a check that
    found a problem with a value the resolver never emitted changes nothing.
    """
    if correction is None:
        return
    if isinstance(correction.applied_value, dict):
        for target in (locked, overrides):
            existing = target.get(correction.field)
            if isinstance(existing, dict):
                existing.update(correction.applied_value)
                return
        return
    if not isinstance(correction.applied_value, (int, float, bool, str)):
        return
    for target in (locked, overrides):
        if correction.field in target:
            target[correction.field] = correction.applied_value
            return


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    if not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def _in_band(value: float, low: float | None, high: float | None, tolerance: float = 1e-6) -> bool:
    if low is not None and value < low - tolerance:
        return False
    if high is not None and value > high + tolerance:
        return False
    return True


def _band_text(low: float | None, high: float | None, unit: str = "C") -> str:
    if low is not None and high is not None:
        return f"{low:.0f}..{high:.0f} {unit}"
    if low is not None:
        return f">= {low:.0f} {unit}"
    if high is not None:
        return f"<= {high:.0f} {unit}"
    return "no limit recorded"


def _claimed_required(plan: AiPrintPlan, category: str) -> bool:
    return plan.category(category).status == "required"


def _unsupported(plan: AiPrintPlan, category: str) -> bool:
    return plan.category(category).status == "unsupported"


# --------------------------------------------------------------------------- #
# the checks
# --------------------------------------------------------------------------- #


def _check_model_validity(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    m = analysis.report.metrics
    evidence = [
        f"{m.triangle_count} triangles, {m.body_count} body/bodies",
        f"watertight = {m.is_watertight}, winding consistent = {m.is_winding_consistent}",
        f"{m.non_manifold_edge_count} non-manifold edges, {m.boundary_edge_count} boundary edges",
        f"printability score {analysis.report.printability_score:.2f} ({analysis.report.risk_level})",
    ]
    if analysis.blocking_findings:
        return Check(
            category="model_validity",
            status="FAILED",
            code="GEOMETRY_UNPRINTABLE",
            reason=(
                "The mesh cannot be printed as it stands: "
                + "; ".join(analysis.blocking_findings[:3])
            ),
            evidence=evidence,
            correctable=False,
            downgradable=False,
        )
    return Check(
        category="model_validity",
        status="PASS",
        code="GEOMETRY_OK",
        reason=(
            f"Measured {m.triangle_count} triangles, watertight={m.is_watertight}, "
            f"{m.body_count} body/bodies."
        ),
        evidence=evidence,
    )


def _check_orientation(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.orientation
    if cat.status == "unsupported":
        return Check(
            category="orientation",
            status="UNSUPPORTED",
            code="ORIENTATION_UNSUPPORTED",
            reason=cat.reason or "Orientation was declared unsupported.",
            evidence=[f"{len(analysis.candidates)} candidate poses were measured."],
        )

    candidate_id = cat.candidate_id or analysis.recommended_candidate_id
    candidate = analysis.candidate_by_id(candidate_id)
    if candidate is None:
        known = [c.id for c in analysis.candidates]
        return Check(
            category="orientation",
            status="FAILED",
            code="ORIENTATION_UNKNOWN",
            reason=(
                f"Orientation '{candidate_id}' is not one of the {len(known)} poses the "
                "analysis measured."
            ),
            evidence=[f"measured candidates: {', '.join(known[:6])}"],
            correctable=True,
            downgradable=False,
        )
    if not candidate.fits:
        return Check(
            category="orientation",
            status="FAILED",
            code="ORIENTATION_DOES_NOT_FIT",
            reason=candidate.rejected_reason
            or f"Orientation '{candidate_id}' does not fit the build volume.",
            evidence=candidate.evidence,
            correctable=True,
            downgradable=False,
        )

    evidence = candidate.evidence
    if candidate.id != analysis.recommended_candidate_id:
        recommended = analysis.candidate_by_id(analysis.recommended_candidate_id)
        if recommended is not None:
            evidence.append(
                f"analysis recommended {recommended.id} (score {recommended.score:.2f}) "
                f"over {candidate.id} (score {candidate.score:.2f})"
            )
    return Check(
        category="orientation",
        status="PASS",
        code="ORIENTATION_MEASURED",
        reason=(
            f"Pose {candidate.id} was measured: {candidate.contact_area_mm2:.0f} mm2 of bed "
            f"contact, {candidate.overhang_percent:.1f}% overhang, {candidate.height_mm:.1f} mm tall."
        ),
        evidence=evidence,
    )


def _check_bed_placement(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    placement = facts.placement
    evidence = [
        f"bounds {placement.bounds['min']} -> {placement.bounds['max']}",
        f"build volume {placement.build_volume_mm} ({placement.origin_convention} origin)",
    ] + [v.message for v in placement.violations[:3]]

    if placement.passed:
        return Check(
            category="bed_placement",
            status="PASS",
            code="BED_PLACEMENT_OK",
            reason=placement.summary,
            evidence=evidence,
        )
    return Check(
        category="bed_placement",
        status="FAILED",
        code="BED_PLACEMENT_INVALID",
        reason=placement.summary,
        evidence=evidence,
        correctable=False,
        downgradable=False,
    )


def _check_layer_height(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.layer_height
    if cat.status == "unsupported":
        return Check(
            category="layer_height", status="UNSUPPORTED", code="LAYER_HEIGHT_UNSUPPORTED",
            reason=cat.reason or "Layer height was declared unsupported.", evidence=[],
        )
    nozzle = analysis.limits.nozzle_diameter_mm
    ceiling = nozzle * 0.75
    value = cat.value_mm
    if value is None:
        return Check(
            category="layer_height",
            status="NOT_REQUIRED",
            code="LAYER_HEIGHT_DEFERRED",
            reason="No layer height proposed; the optimizer will resolve it from the nozzle and material.",
            evidence=[f"nozzle {nozzle:.2f} mm -> ceiling {ceiling:.3f} mm"],
        )
    evidence = [
        f"proposed {value:.3f} mm",
        f"nozzle {nozzle:.2f} mm -> bonding ceiling {ceiling:.3f} mm (75%)",
    ]
    if value > ceiling + 1e-9:
        return Check(
            category="layer_height",
            status="FAILED",
            code="LAYER_HEIGHT_EXCEEDS_NOZZLE",
            reason=(
                f"A {value:.3f} mm layer is above the {ceiling:.3f} mm ceiling for a "
                f"{nozzle:.2f} mm nozzle; the layer cannot bond."
            ),
            evidence=evidence,
            correctable=True,
            downgradable=True,
            normalisation=Normalisation(
                field="layer_height_mm",
                model_value=value,
                applied_value=round(ceiling, 4),
                reason=f"above the {ceiling:.3f} mm bonding ceiling for a {nozzle:.2f} mm nozzle",
            ),
        )
    return Check(
        category="layer_height",
        status="PASS",
        code="LAYER_HEIGHT_OK",
        reason=f"{value:.3f} mm is within the {ceiling:.3f} mm ceiling for a {nozzle:.2f} mm nozzle.",
        evidence=evidence,
    )


def _check_walls(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.walls
    if cat.status == "unsupported":
        return Check(
            category="walls", status="UNSUPPORTED", code="WALLS_UNSUPPORTED",
            reason=cat.reason or "Walls were declared unsupported.", evidence=[],
        )
    if cat.count is None:
        return Check(
            category="walls", status="NOT_REQUIRED", code="WALLS_DEFERRED",
            reason="No wall count proposed; the optimizer will resolve it.",
            evidence=[f"line width ~{analysis.limits.nozzle_diameter_mm * 1.125:.3f} mm"],
        )
    line_width = analysis.limits.nozzle_diameter_mm * 1.125
    evidence = [f"proposed {cat.count} walls at ~{line_width:.3f} mm line width"]
    if cat.count < 2:
        return Check(
            category="walls",
            status="FAILED",
            code="WALL_COUNT_TOO_LOW",
            reason=(
                f"{cat.count} perimeter(s) cannot form a sealed shell; at least 2 are needed "
                "for a watertight solid."
            ),
            evidence=evidence,
            correctable=True,
            downgradable=True,
            normalisation=Normalisation(
                field="wall_count", model_value=cat.count, applied_value=2,
                reason="fewer than the 2 perimeters a sealed shell needs",
            ),
        )
    return Check(
        category="walls",
        status="PASS",
        code="WALL_COUNT_OK",
        reason=f"{cat.count} perimeters give a {cat.count * line_width:.2f} mm shell.",
        evidence=evidence,
    )


def _check_top_bottom(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.top_bottom
    if cat.status == "unsupported":
        return Check(
            category="top_bottom", status="UNSUPPORTED", code="TOP_BOTTOM_UNSUPPORTED",
            reason=cat.reason or "Top/bottom shells were declared unsupported.", evidence=[],
        )
    if cat.top_layers is None and cat.bottom_layers is None:
        return Check(
            category="top_bottom", status="NOT_REQUIRED", code="TOP_BOTTOM_DEFERRED",
            reason="No shell thickness proposed; the optimizer will resolve it.",
            evidence=[f"layer height ~{_round(plan.layer_height.value_mm, 3)} mm"],
        )
    top = cat.top_layers if cat.top_layers is not None else 0
    bottom = cat.bottom_layers if cat.bottom_layers is not None else 0
    layer = plan.layer_height.value_mm or 0.2
    evidence = [
        f"{top} top / {bottom} bottom layers",
        f"~{top * layer:.2f} mm top skin, ~{bottom * layer:.2f} mm bottom skin",
    ]
    if top == 0 or bottom == 0:
        return Check(
            category="top_bottom",
            status="WARNING",
            code="SHELL_MISSING",
            reason=(
                f"{'Top' if top == 0 else 'Bottom'} shell is 0 layers, so that face prints "
                "as exposed infill."
            ),
            evidence=evidence,
            correctable=True,
            downgradable=True,
        )
    return Check(
        category="top_bottom",
        status="PASS",
        code="SHELL_OK",
        reason=f"{top} top / {bottom} bottom solid layers at {layer:.3f} mm.",
        evidence=evidence,
    )


def _check_infill(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.infill
    if cat.status == "unsupported":
        return Check(
            category="infill", status="UNSUPPORTED", code="INFILL_UNSUPPORTED",
            reason=cat.reason or "Infill was declared unsupported.", evidence=[],
        )
    if cat.density_percent is None and cat.pattern is None:
        return Check(
            category="infill", status="NOT_REQUIRED", code="INFILL_DEFERRED",
            reason="No infill proposed; the optimizer will resolve it from the objective.",
            evidence=[],
        )
    evidence: list[str] = []
    if cat.density_percent is not None:
        evidence.append(f"density {cat.density_percent:.1f}%")
    if cat.pattern is not None:
        evidence.append(f"pattern {cat.pattern} (known: {', '.join(sorted(_INFILL_MAP))})")
    if cat.pattern is not None and str(cat.pattern).lower() not in _INFILL_MAP:
        return Check(
            category="infill",
            status="FAILED",
            code="INFILL_PATTERN_UNKNOWN",
            reason=f"Infill pattern '{cat.pattern}' is not one this platform can express.",
            evidence=evidence,
            correctable=True,
            downgradable=True,
            normalisation=Normalisation(
                field="infill_pattern", model_value=cat.pattern, applied_value="gyroid",
                reason="pattern is not in the supported set",
            ),
        )
    return Check(
        category="infill",
        status="PASS",
        code="INFILL_OK",
        reason=(
            f"{cat.density_percent:.1f}% {cat.pattern} infill."
            if cat.density_percent is not None and cat.pattern
            else "Infill proposal is within range."
        ),
        evidence=evidence,
    )


def _check_supports(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.supports
    feature = analysis.capabilities
    if not feature.supports("support") and feature.status("support") == "unsupported":
        return Check(
            category="supports",
            status="UNSUPPORTED",
            code="SUPPORT_ENGINE_UNSUPPORTED",
            reason=feature.reason("support"),
            evidence=[f"engine {feature.engine_key} {feature.version or ''}"],
        )
    if cat.status == "unsupported":
        return Check(
            category="supports", status="UNSUPPORTED", code="SUPPORT_DECLINED",
            reason=cat.reason or "Supports were declared unsupported.", evidence=[],
        )

    evidence = [
        f"measured overhang {facts.support.overhang_area_mm2:.0f} mm2 "
        f"({facts.support.overhang_percent:.1f}%)",
        f"unsupported area {facts.support.support_area_mm2:.0f} mm2",
        f"longest span {facts.support.longest_bridge_mm:.1f} mm",
        facts.support.reason,
    ]
    if cat.density_percent is not None:
        evidence.append(f"proposed density {cat.density_percent:.1f}%")

    if facts.support.required and not cat.enabled:
        return Check(
            category="supports",
            status="FAILED",
            code="SUPPORT_REQUIRED_BUT_DISABLED",
            reason=(
                "Support is required at this orientation but the plan disables it: "
                + facts.support.reason
            ),
            evidence=evidence,
            correctable=True,
            downgradable=True,
            normalisation=Normalisation(
                field="support_enable",
                model_value=False,
                applied_value=True,
                reason=facts.support.reason,
            ),
        )
    if not facts.support.required and cat.enabled:
        return Check(
            category="supports",
            status="WARNING",
            code="SUPPORT_UNNECESSARY",
            reason=(
                f"Support is enabled but the measurement finds none is needed: "
                f"{facts.support.reason}"
            ),
            evidence=evidence,
            correctable=True,
            downgradable=True,
            normalisation=Normalisation(
                field="support_enable",
                model_value=True,
                applied_value=False,
                reason=facts.support.reason,
            ),
        )
    if cat.status == "not_required" and facts.support.required:
        return Check(
            category="supports",
            status="FAILED",
            code="SUPPORT_REQUIRED_BUT_DECLINED",
            reason=f"The plan declares support not required, but it is: {facts.support.reason}",
            evidence=evidence,
            correctable=True,
            downgradable=True,
            normalisation=Normalisation(
                field="support_enable", model_value=False, applied_value=True,
                reason=facts.support.reason,
            ),
        )
    return Check(
        category="supports",
        status="PASS",
        code="SUPPORTS_OK",
        reason=(
            f"Support {'enabled' if cat.enabled else 'disabled'} agrees with the "
            f"measurement: {facts.support.reason}"
        ),
        evidence=evidence,
    )


def _check_support_interface(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.support_interface
    caps = analysis.capabilities
    if caps.status("support_interface") == "unsupported":
        return Check(
            category="support_interface",
            status="UNSUPPORTED",
            code="SUPPORT_INTERFACE_UNSUPPORTED",
            reason=caps.reason("support_interface"),
            evidence=[f"engine {caps.engine_key}"],
        )
    if not cat.enabled:
        return Check(
            category="support_interface",
            status="NOT_REQUIRED",
            code="SUPPORT_INTERFACE_OFF",
            reason=cat.reason or "No support interface requested.",
            evidence=[f"support {'on' if plan.supports.enabled else 'off'} at this pose"],
        )
    evidence = [
        f"height {cat.height_mm} mm" if cat.height_mm else "height not specified",
        f"density {cat.density_percent}%" if cat.density_percent is not None else "density not specified",
        f"engine setting: {', '.join(caps.features['support_interface'].settings)}",
    ]
    return Check(
        category="support_interface",
        status="PASS",
        code="SUPPORT_INTERFACE_OK",
        reason="Support interface requested and the engine exposes it.",
        evidence=evidence,
    )


def _adhesion_mode(plan: AiPrintPlan) -> str:
    if plan.raft.enabled:
        return "raft"
    if plan.brim.enabled:
        return "brim"
    return "none"


def _check_brim(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.brim
    caps = analysis.capabilities
    if caps.status("brim") == "unsupported":
        return Check(
            category="brim", status="UNSUPPORTED", code="BRIM_UNSUPPORTED",
            reason=caps.reason("brim"), evidence=[f"engine {caps.engine_key}"],
        )
    if not cat.enabled:
        recommended = facts.adhesion.recommended
        if recommended == "brim":
            return Check(
                category="brim",
                status="FAILED",
                code="BRIM_RECOMMENDED_BUT_DISABLED",
                reason=f"A brim is recommended and the plan disables it: {facts.adhesion.reason}",
                evidence=[
                    f"contact {facts.adhesion.contact_area_mm2:.0f} mm2 of "
                    f"{facts.adhesion.footprint_area_mm2:.0f} mm2 footprint",
                    f"stability {facts.adhesion.stability:.2f}",
                    facts.adhesion.reason,
                ],
                correctable=True,
                downgradable=True,
                normalisation=Normalisation(
                    field="adhesion_type", model_value="none", applied_value="brim",
                    reason=facts.adhesion.reason,
                ),
            )
        return Check(
            category="brim", status="NOT_REQUIRED", code="BRIM_OFF",
            reason=cat.reason or "No brim requested.",
            evidence=[
                f"contact {facts.adhesion.contact_area_mm2:.0f} mm2, stability "
                f"{facts.adhesion.stability:.2f}",
            ],
        )
    width = cat.width_mm
    evidence = [f"requested width {width} mm" if width else "width not specified"]
    if width is not None and width <= 0:
        return Check(
            category="brim", status="FAILED", code="BRIM_WIDTH_INVALID",
            reason=f"A brim width of {width} mm would add nothing.",
            evidence=evidence, correctable=True, downgradable=True,
            normalisation=Normalisation(
                field="brim_width_mm", model_value=width, applied_value=5.0,
                reason="a brim needs a positive width",
            ),
        )
    return Check(
        category="brim", status="PASS", code="BRIM_ON",
        reason="Brim requested.", evidence=evidence,
    )


def _check_raft(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.raft
    caps = analysis.capabilities
    if caps.status("adhesion_type") == "unsupported":
        return Check(
            category="raft", status="UNSUPPORTED", code="RAFT_UNSUPPORTED",
            reason=caps.reason("adhesion_type"), evidence=[f"engine {caps.engine_key}"],
        )
    if not cat.enabled:
        recommended = facts.adhesion.recommended
        if recommended == "raft":
            return Check(
                category="raft",
                status="FAILED",
                code="RAFT_RECOMMENDED_BUT_DISABLED",
                reason=f"A raft is required and the plan disables it: {facts.adhesion.reason}",
                evidence=[
                    f"bed contact {facts.adhesion.contact_area_mm2:.1f} mm2",
                    facts.adhesion.reason,
                ],
                correctable=True,
                downgradable=True,
                normalisation=Normalisation(
                    field="adhesion_type", model_value="none", applied_value="raft",
                    reason=facts.adhesion.reason,
                ),
            )
        return Check(
            category="raft", status="NOT_REQUIRED", code="RAFT_OFF",
            reason=cat.reason or "No raft requested.",
            evidence=[f"bed contact {facts.adhesion.contact_area_mm2:.1f} mm2"],
        )
    if facts.adhesion.contact_area_mm2 >= MIN_CONTACT_AREA_MM2:
        return Check(
            category="raft",
            status="WARNING",
            code="RAFT_UNNECESSARY",
            reason=(
                f"A raft is requested but the part already has "
                f"{facts.adhesion.contact_area_mm2:.0f} mm2 of bed contact."
            ),
            evidence=[facts.adhesion.reason],
            correctable=True,
            downgradable=True,
        )
    return Check(
        category="raft", status="PASS", code="RAFT_ON",
        reason="Raft requested.",
        evidence=[f"bed contact {facts.adhesion.contact_area_mm2:.1f} mm2"],
    )


def _check_bed_adhesion(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.bed_adhesion
    measured = facts.adhesion
    evidence = [
        f"contact {measured.contact_area_mm2:.1f} mm2 of {measured.footprint_area_mm2:.0f} mm2",
        f"contact ratio {measured.contact_ratio * 100:.1f}%",
        f"stability {measured.stability:.2f}",
        f"measurement recommends {measured.recommended}",
        measured.reason,
    ]
    if cat.status == "not_required":
        if measured.recommended != "none":
            return Check(
                category="bed_adhesion",
                status="FAILED",
                code="ADHESION_RISK_DECLINED",
                reason=f"The plan declares adhesion not required, but it is: {measured.reason}",
                evidence=evidence,
                correctable=True,
                downgradable=True,
                normalisation=Normalisation(
                    field="adhesion_type", model_value="none", applied_value=measured.recommended,
                    reason=measured.reason,
                ),
            )
        return Check(
            category="bed_adhesion",
            status="NOT_REQUIRED",
            code="ADHESION_NOT_REQUIRED",
            reason=measured.reason,
            evidence=evidence,
        )

    plan_mode = _adhesion_mode(plan)
    risk = cat.risk
    expected_risk = (
        "HIGH_RISK"
        if measured.contact_area_mm2 < MIN_CONTACT_AREA_MM2
        else "MEDIUM_RISK"
        if (measured.contact_ratio < 0.25 or measured.stability < 0.35)
        else "LOW_RISK"
    )
    evidence.append(f"risk claimed {risk}, measured {expected_risk}")

    if risk == "LOW_RISK" and expected_risk != "LOW_RISK":
        return Check(
            category="bed_adhesion",
            status="WARNING",
            code="ADHESION_RISK_UNDERSTATED",
            reason=(
                f"The plan claims LOW_RISK but the measurement says {expected_risk}: "
                f"{measured.reason}"
            ),
            evidence=evidence,
            correctable=True,
            downgradable=True,
            normalisation=Normalisation(
                field="adhesion_type",
                model_value=plan_mode,
                applied_value=measured.recommended,
                reason=measured.reason,
            ),
        )
    if plan_mode == "none" and measured.recommended != "none":
        return Check(
            category="bed_adhesion",
            status="WARNING",
            code="ADHESION_MISSING",
            reason=f"{measured.recommended} is recommended and the plan adds none: {measured.reason}",
            evidence=evidence,
            correctable=True,
            downgradable=True,
            normalisation=Normalisation(
                field="adhesion_type", model_value="none", applied_value=measured.recommended,
                reason=measured.reason,
            ),
        )
    return Check(
        category="bed_adhesion",
        status="PASS",
        code="ADHESION_OK",
        reason=f"{risk} matches the measurement: {measured.reason}",
        evidence=evidence,
    )


def _temperature_range(analysis: PreAnalysis) -> tuple[float | None, float | None, float | None, float | None]:
    limits = analysis.limits
    nozzle_low = limits.safe_nozzle_min_c if limits.safe_nozzle_min_c is not None else limits.nozzle_min_c
    nozzle_high = limits.safe_nozzle_max_c if limits.safe_nozzle_max_c is not None else limits.nozzle_max_c
    bed_low = limits.bed_min_c
    bed_high = limits.bed_max_c
    return nozzle_low, nozzle_high, bed_low, bed_high


def _check_temperature(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.temperature
    nozzle_low, nozzle_high, bed_low, bed_high = _temperature_range(analysis)
    evidence: list[str] = [
        f"nozzle band {_band_text(nozzle_low, nozzle_high)}",
        f"bed band {_band_text(bed_low, bed_high)}",
    ]

    proposals: list[tuple[str, int | None, float | None, float | None, str]] = [
        ("nozzle_c", cat.nozzle_c, nozzle_low, nozzle_high, "nozzle"),
        ("bed_c", cat.bed_c, bed_low, bed_high, "bed"),
        ("first_layer_nozzle_c", cat.first_layer_nozzle_c, nozzle_low, nozzle_high, "nozzle"),
        ("first_layer_bed_c", cat.first_layer_bed_c, bed_low, bed_high, "bed"),
    ]

    failures: list[Check] = []
    supplied: list[str] = []
    for name, value, low, high, label in proposals:
        if value is None:
            continue
        supplied.append(f"{name}={value} C")
        if not _in_band(float(value), low, high):
            failures.append(
                Check(
                    category="temperature",
                    status="FAILED",
                    code="AI_TEMPERATURE_OUT_OF_RANGE",
                    reason=(
                        f"{name} = {value} C is outside this machine/material's "
                        f"{label} band {_band_text(low, high)}."
                    ),
                    evidence=evidence + supplied,
                    # Correctable: the model can propose a value inside the band.
                    # Never downgradable: a wrong temperature is not something the
                    # platform will paper over, because the G-code validator would
                    # reject the output anyway.
                    correctable=True,
                    downgradable=False,
                )
            )

    if failures:
        return failures[0]

    if not supplied:
        return Check(
            category="temperature",
            status="NOT_REQUIRED",
            code="TEMPERATURE_FROM_PROFILE",
            reason="No temperatures proposed; they will be resolved from the material profile.",
            evidence=evidence,
        )
    return Check(
        category="temperature",
        status="PASS",
        code="TEMPERATURE_OK",
        reason="All proposed temperatures are inside the validated band.",
        evidence=evidence + supplied,
    )


def _check_cooling(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.cooling
    caps = analysis.capabilities
    if caps.status("cooling_fan") == "unsupported":
        return Check(
            category="cooling", status="UNSUPPORTED", code="COOLING_UNSUPPORTED",
            reason=caps.reason("cooling_fan"), evidence=[f"engine {caps.engine_key}"],
        )
    if cat.fan_percent is None and cat.min_layer_time_s is None:
        return Check(
            category="cooling", status="NOT_REQUIRED", code="COOLING_DEFERRED",
            reason="No cooling proposed; the optimizer will resolve it from the material.",
            evidence=[],
        )
    evidence = []
    if cat.fan_percent is not None:
        evidence.append(f"fan {cat.fan_percent}%")
    if cat.min_layer_time_s is not None:
        evidence.append(f"min layer time {cat.min_layer_time_s:.1f} s")
    if cat.fan_percent is not None and not 0 <= cat.fan_percent <= 100:
        return Check(
            category="cooling", status="FAILED", code="COOLING_OUT_OF_RANGE",
            reason=f"Fan speed {cat.fan_percent}% is not a percentage.",
            evidence=evidence, correctable=True, downgradable=True,
            normalisation=Normalisation(
                field="cooling_fan_percent", model_value=cat.fan_percent,
                applied_value=max(0, min(100, int(cat.fan_percent))),
                reason="fan speed must be 0..100%",
            ),
        )
    return Check(
        category="cooling", status="PASS", code="COOLING_OK",
        reason="Cooling proposal is within range.", evidence=evidence,
    )


def _check_speed(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.speed
    limits = analysis.limits
    print_max = limits.max_print_speed_mm_s
    travel_max = limits.max_travel_speed_mm_s
    evidence = [
        f"machine print ceiling {print_max} mm/s",
        f"machine travel ceiling {travel_max} mm/s",
    ]
    over: list[str] = []
    values: list[str] = []
    fixes: list[Normalisation] = []
    for name, value, ceiling, key in (
        ("print_mm_s", cat.print_mm_s, print_max, "print_speed_mm_s"),
        ("outer_wall_mm_s", cat.outer_wall_mm_s, print_max, "outer_wall_speed_mm_s"),
        ("inner_wall_mm_s", cat.inner_wall_mm_s, print_max, "inner_wall_speed_mm_s"),
        ("infill_mm_s", cat.infill_mm_s, print_max, "infill_speed_mm_s"),
        ("first_layer_mm_s", cat.first_layer_mm_s, print_max, "first_layer_speed_mm_s"),
        ("support_mm_s", cat.support_mm_s, print_max, "support_speed_mm_s"),
        ("travel_mm_s", cat.travel_mm_s, travel_max, "travel_speed_mm_s"),
    ):
        if value is None or ceiling is None:
            continue
        values.append(f"{name}={value}")
        if float(value) > float(ceiling) + 1e-6:
            over.append(f"{name} {value} > {ceiling}")
            fixes.append(
                Normalisation(
                    field=key,
                    model_value=float(value),
                    applied_value=round(float(ceiling), 2),
                    reason=f"this machine's ceiling for {name} is {ceiling} mm/s",
                )
            )
    evidence.extend(values)

    if over:
        return Check(
            category="speed",
            status="FAILED",
            code="AI_SPEED_ABOVE_MACHINE_LIMIT",
            reason="Proposed speeds exceed this machine: " + "; ".join(over),
            evidence=evidence,
            correctable=True,
            downgradable=True,
            normalisation=fixes,
        )
    if not values:
        return Check(
            category="speed", status="NOT_REQUIRED", code="SPEED_DEFERRED",
            reason="No speeds proposed; the optimizer will resolve them from the objective.",
            evidence=evidence,
        )
    return Check(
        category="speed", status="PASS", code="SPEED_OK",
        reason="All proposed speeds are within the machine's ceilings.",
        evidence=evidence,
    )


def _check_flow(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.flow
    limits = analysis.limits
    ceiling = limits.max_volumetric_flow_mm3s
    line_width = plan.first_layer.line_width_mm or (
        analysis.limits.nozzle_diameter_mm * 1.125
    )
    layer = plan.layer_height.value_mm or 0.2
    speed = plan.speed.print_mm_s
    evidence = [
        f"line width {line_width:.3f} mm, layer {layer:.3f} mm",
        f"machine volumetric ceiling {ceiling} mm3/s" if ceiling else "no volumetric ceiling recorded",
    ]
    if cat.volumetric_flow_mm3s is not None:
        evidence.append(f"proposed flow {cat.volumetric_flow_mm3s:.1f} mm3/s")

    if ceiling is not None and speed is not None:
        derived = float(speed) * line_width * layer
        evidence.append(f"{speed} mm/s x {line_width:.3f} x {layer:.3f} = {derived:.1f} mm3/s")
        if derived > float(ceiling) + 1e-6:
            fastest = float(ceiling) / max(line_width * layer, 1e-6)
            return Check(
                category="flow",
                status="FAILED",
                code="AI_FLOW_ABOVE_MACHINE_LIMIT",
                reason=(
                    f"{speed} mm/s at {line_width:.3f} x {layer:.3f} mm needs "
                    f"{derived:.1f} mm3/s but this machine delivers {ceiling} mm3/s."
                ),
                evidence=evidence,
                correctable=True,
                downgradable=True,
                normalisation=Normalisation(
                    field="print_speed_mm_s",
                    model_value=float(speed),
                    applied_value=round(max(0.1, fastest), 2),
                    reason=(
                        f"{ceiling} mm3/s divided by {line_width:.3f} x {layer:.3f} mm "
                        f"gives {fastest:.1f} mm/s"
                    ),
                ),
            )
    if cat.volumetric_flow_mm3s is not None and ceiling is not None:
        if float(cat.volumetric_flow_mm3s) > float(ceiling) + 1e-6:
            return Check(
                category="flow",
                status="FAILED",
                code="AI_FLOW_ABOVE_MACHINE_LIMIT",
                reason=(
                    f"Proposed {cat.volumetric_flow_mm3s:.1f} mm3/s exceeds this machine's "
                    f"{ceiling} mm3/s."
                ),
                evidence=evidence,
                correctable=True,
                downgradable=True,
            )
    if speed is None and cat.volumetric_flow_mm3s is None:
        return Check(
            category="flow", status="NOT_REQUIRED", code="FLOW_DEFERRED",
            reason="No flow figure proposed; it is implied by the resolved speeds.",
            evidence=evidence,
        )
    return Check(
        category="flow", status="PASS", code="FLOW_OK",
        reason="Proposed flow is within the machine's volumetric limit.",
        evidence=evidence,
    )


def _check_retraction(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.retraction
    caps = analysis.capabilities
    if cat.status == "unsupported":
        return Check(
            category="retraction", status="UNSUPPORTED", code="RETRACTION_DECLINED",
            reason=cat.reason or "Retraction was declared unsupported.", evidence=[],
        )
    evidence: list[str] = []
    if cat.distance_mm is not None:
        evidence.append(f"distance {cat.distance_mm} mm")
    if cat.speed_mm_s is not None:
        evidence.append(f"speed {cat.speed_mm_s} mm/s")
    if cat.z_hop_mm is not None:
        if caps.status("z_hop") == "unsupported":
            return Check(
                category="retraction",
                status="UNSUPPORTED",
                code="Z_HOP_UNSUPPORTED",
                reason=caps.reason("z_hop"),
                evidence=evidence + [f"engine {caps.engine_key}"],
            )
        evidence.append(f"z-hop {cat.z_hop_mm} mm")
    if not evidence:
        return Check(
            category="retraction", status="NOT_REQUIRED", code="RETRACTION_DEFERRED",
            reason="No retraction proposed; the material profile will supply it.",
            evidence=[],
        )
    if cat.distance_mm is not None and cat.distance_mm > 3.0:
        return Check(
            category="retraction",
            status="WARNING",
            code="RETRACTION_DISTANCE_HIGH",
            reason=(
                f"{cat.distance_mm} mm of retraction is far beyond the 0.4-1.2 mm typical "
                "range and will under-extrude after every travel move."
            ),
            evidence=evidence,
            correctable=True,
            downgradable=True,
        )
    return Check(
        category="retraction", status="PASS", code="RETRACTION_OK",
        reason="Retraction proposal is within a sane range.", evidence=evidence,
    )


def _check_first_layer(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.first_layer
    caps = analysis.capabilities
    evidence: list[str] = [
        f"bed contact {facts.adhesion.contact_area_mm2:.1f} mm2",
        f"stability {facts.adhesion.stability:.2f}",
    ]
    unsupported: list[str] = []
    if cat.speed_mm_s is not None and caps.status("first_layer_speed") == "unsupported":
        unsupported.append("first layer speed")
    if cat.fan_percent is not None and caps.status("cooling_fan") == "unsupported":
        unsupported.append("first layer fan")
    if unsupported:
        return Check(
            category="first_layer",
            status="UNSUPPORTED",
            code="FIRST_LAYER_UNSUPPORTED",
            reason=f"The engine cannot express: {', '.join(unsupported)}.",
            evidence=evidence + [caps.reason("first_layer_speed")],
        )

    if cat.speed_mm_s is None and cat.height_mm is None and cat.line_width_mm is None and cat.fan_percent is None:
        return Check(
            category="first_layer", status="NOT_REQUIRED", code="FIRST_LAYER_DEFERRED",
            reason="No first-layer settings proposed; the optimizer resolves them.",
            evidence=evidence,
        )
    if cat.speed_mm_s is not None:
        evidence.append(f"speed {cat.speed_mm_s} mm/s")
        ceiling = analysis.limits.max_print_speed_mm_s
        if ceiling is not None and float(cat.speed_mm_s) > float(ceiling) + 1e-6:
            return Check(
                category="first_layer",
                status="FAILED",
                code="FIRST_LAYER_SPEED_ABOVE_LIMIT",
                reason=f"First layer {cat.speed_mm_s} mm/s exceeds the {ceiling} mm/s ceiling.",
                evidence=evidence, correctable=True, downgradable=True,
                normalisation=Normalisation(
                    field="first_layer_speed_mm_s",
                    model_value=float(cat.speed_mm_s),
                    applied_value=round(float(ceiling), 2),
                    reason=f"this machine's ceiling is {ceiling} mm/s",
                ),
            )
    if cat.speed_mm_s is not None and float(cat.speed_mm_s) > 40.0:
        return Check(
            category="first_layer",
            status="WARNING",
            code="FIRST_LAYER_SPEED_HIGH",
            reason=(
                f"A {cat.speed_mm_s} mm/s first layer is fast for a layer that has to stick; "
                "20-30 mm/s is the usual range."
            ),
            evidence=evidence, correctable=True, downgradable=True,
        )
    if cat.height_mm is not None:
        evidence.append(f"height {cat.height_mm} mm")
    if cat.line_width_mm is not None:
        evidence.append(f"line width {cat.line_width_mm} mm")
    return Check(
        category="first_layer", status="PASS", code="FIRST_LAYER_OK",
        reason="First-layer settings are within range.", evidence=evidence,
    )


def _check_acceleration(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.acceleration
    caps = analysis.capabilities
    if caps.status("acceleration") == "unsupported":
        return Check(
            category="acceleration", status="UNSUPPORTED", code="ACCELERATION_UNSUPPORTED",
            reason=caps.reason("acceleration"), evidence=[f"engine {caps.engine_key}"],
        )
    if cat.value_mm_s2 is None and cat.wall_mm_s2 is None and cat.travel_mm_s2 is None:
        return Check(
            category="acceleration", status="NOT_REQUIRED", code="ACCELERATION_DEFERRED",
            reason="No acceleration proposed; the machine default applies.",
            evidence=[f"machine limit {analysis.limits.max_acceleration_mm_s2} mm/s^2"],
        )
    ceiling = analysis.limits.max_acceleration_mm_s2
    evidence = [f"machine limit {ceiling} mm/s^2"]
    over = []
    fixes: list[Normalisation] = []
    for name, value, key in (
        ("value_mm_s2", cat.value_mm_s2, "acceleration_mm_s2"),
        ("wall_mm_s2", cat.wall_mm_s2, "wall_acceleration_mm_s2"),
        ("travel_mm_s2", cat.travel_mm_s2, "travel_acceleration_mm_s2"),
    ):
        if value is None:
            continue
        evidence.append(f"{name}={value}")
        if ceiling is not None and float(value) > float(ceiling) + 1e-6:
            over.append(f"{name} {value} > {ceiling}")
            fixes.append(
                Normalisation(
                    field=key,
                    model_value=float(value),
                    applied_value=float(ceiling),
                    reason=f"this machine's acceleration limit is {ceiling} mm/s^2",
                )
            )
    if over:
        return Check(
            category="acceleration",
            status="FAILED",
            code="ACCELERATION_ABOVE_LIMIT",
            reason="Proposed acceleration exceeds this machine: " + "; ".join(over),
            evidence=evidence, correctable=True, downgradable=True,
            normalisation=fixes,
        )
    return Check(
        category="acceleration", status="PASS", code="ACCELERATION_OK",
        reason="Acceleration is within the machine's limit.", evidence=evidence,
    )


def _check_min_layer_time(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.min_layer_time
    caps = analysis.capabilities
    if caps.status("min_layer_time") == "unsupported":
        return Check(
            category="min_layer_time", status="UNSUPPORTED", code="MIN_LAYER_TIME_UNSUPPORTED",
            reason=caps.reason("min_layer_time"), evidence=[f"engine {caps.engine_key}"],
        )
    if cat.seconds is None:
        return Check(
            category="min_layer_time", status="NOT_REQUIRED", code="MIN_LAYER_TIME_DEFERRED",
            reason="No minimum layer time proposed; the engine default applies.",
            evidence=[],
        )
    evidence = [f"minimum layer time {cat.seconds:.1f} s"]
    if cat.seconds > 60:
        return Check(
            category="min_layer_time",
            status="WARNING",
            code="MIN_LAYER_TIME_HIGH",
            reason=(
                f"{cat.seconds:.1f} s per layer would idle the hotend on small layers; "
                "anything above 60 s usually points at a unit mistake."
            ),
            evidence=evidence, correctable=True, downgradable=True,
        )
    return Check(
        category="min_layer_time", status="PASS", code="MIN_LAYER_TIME_OK",
        reason="Minimum layer time is sane.", evidence=evidence,
    )


def _check_material_specific(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.material_specific
    honoured = [str(item).strip().lower() for item in cat.honoured if str(item).strip()]
    unknown = [item for item in honoured if item not in HONOURABLE_MATERIAL_FACTS]
    if unknown:
        return Check(
            category="material_specific",
            status="UNSUPPORTED",
            code="MATERIAL_FACT_UNKNOWN",
            reason=(
                "These are not material facts this platform tracks: " + ", ".join(unknown)
            ),
            evidence=[f"tracked: {', '.join(sorted(HONOURABLE_MATERIAL_FACTS))}"],
            correctable=True,
            downgradable=True,
            normalisation=Normalisation(
                field="material_specific.honoured",
                model_value=cat.honoured,
                applied_value=[i for i in honoured if i in HONOURABLE_MATERIAL_FACTS],
                reason="unrecognised material facts dropped",
            ),
        )
    if not honoured:
        return Check(
            category="material_specific", status="NOT_REQUIRED", code="MATERIAL_FACTS_NONE",
            reason=cat.reason or "No material constraints claimed.",
            evidence=[f"material limits applied by the resolver: nozzle "
                      f"{_band_text(*_temperature_range(analysis)[:2])}"],
        )
    return Check(
        category="material_specific",
        status="PASS",
        code="MATERIAL_FACTS_OK",
        reason="Claimed material constraints are ones the platform tracks.",
        evidence=[f"claimed: {', '.join(honoured)}"],
    )


def _check_slicer_specific(plan: AiPrintPlan, analysis: PreAnalysis, facts: PoseFacts) -> Check:
    cat = plan.slicer_specific
    caps = analysis.capabilities
    requested = [str(item).strip() for item in cat.requested_features if str(item).strip()]
    evidence = [f"engine {caps.engine_key} {caps.version or ''}"]
    if not requested:
        return Check(
            category="slicer_specific",
            status="NOT_REQUIRED",
            code="ENGINE_FEATURES_NONE",
            reason=cat.reason or "No engine-specific feature requested.",
            evidence=evidence,
        )
    unsupported = [
        item for item in requested
        if caps.status(item) != "supported"
    ]
    evidence.append(f"requested: {', '.join(requested)}")
    if unsupported:
        reasons = [caps.reason(item) for item in unsupported[:3]]
        return Check(
            category="slicer_specific",
            status="UNSUPPORTED",
            code="ENGINE_FEATURE_UNSUPPORTED",
            reason=f"The selected engine cannot provide: {', '.join(unsupported)}. {reasons[0] if reasons else ''}".strip(),
            evidence=evidence,
            correctable=True,
            downgradable=True,
            normalisation=Normalisation(
                field="slicer_specific.requested_features",
                model_value=cat.requested_features,
                applied_value=[i for i in requested if caps.status(i) == "supported"],
                reason="features the selected engine does not expose",
            ),
        )
    return Check(
        category="slicer_specific",
        status="PASS",
        code="ENGINE_FEATURES_OK",
        reason="Every requested engine feature is present in this engine's schema.",
        evidence=evidence,
    )


_CHECK_BY_CATEGORY = {
    "model_validity": _check_model_validity,
    "orientation": _check_orientation,
    "bed_placement": _check_bed_placement,
    "layer_height": _check_layer_height,
    "walls": _check_walls,
    "top_bottom": _check_top_bottom,
    "infill": _check_infill,
    "supports": _check_supports,
    "support_interface": _check_support_interface,
    "brim": _check_brim,
    "raft": _check_raft,
    "bed_adhesion": _check_bed_adhesion,
    "temperature": _check_temperature,
    "cooling": _check_cooling,
    "speed": _check_speed,
    "flow": _check_flow,
    "retraction": _check_retraction,
    "first_layer": _check_first_layer,
    "acceleration": _check_acceleration,
    "min_layer_time": _check_min_layer_time,
    "material_specific": _check_material_specific,
    "slicer_specific": _check_slicer_specific,
}


# --------------------------------------------------------------------------- #
# parameter resolution
# --------------------------------------------------------------------------- #


def resolve_parameters(plan: AiPrintPlan, analysis: PreAnalysis) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split the plan into optimizer `locked` keys and direct `overrides`.

    The optimizer understands a fixed set of canonical keys; those go in
    `locked` so the heuristics cannot overwrite what the plan decided. The rest
    of the plan's vocabulary (first-layer speed, brim width, z-hop, separate
    top/bottom shells) has no optimizer knob and is carried as an override,
    merged over the optimizer's output downstream.
    """
    locked: dict[str, Any] = {}
    overrides: dict[str, Any] = {}

    if plan.layer_height.value_mm is not None:
        locked["layer_height_mm"] = plan.layer_height.value_mm
    if plan.walls.count is not None:
        locked["wall_count"] = int(plan.walls.count)
    if plan.infill.density_percent is not None:
        locked["infill_density_percent"] = float(plan.infill.density_percent)
    if plan.infill.pattern is not None and str(plan.infill.pattern).lower() in _INFILL_MAP:
        locked["infill_pattern"] = str(plan.infill.pattern).lower()
    if plan.top_bottom.top_layers is not None and plan.top_bottom.bottom_layers is not None:
        if plan.top_bottom.top_layers == plan.top_bottom.bottom_layers:
            locked["top_bottom_layers"] = int(plan.top_bottom.top_layers)
        else:
            overrides["top_layers"] = int(plan.top_bottom.top_layers)
            overrides["bottom_layers"] = int(plan.top_bottom.bottom_layers)
    elif plan.top_bottom.top_layers is not None:
        overrides["top_layers"] = int(plan.top_bottom.top_layers)
    elif plan.top_bottom.bottom_layers is not None:
        overrides["bottom_layers"] = int(plan.top_bottom.bottom_layers)

    if plan.supports.enabled:
        locked["support_enable"] = True
        if plan.supports.density_percent is not None:
            overrides["support_density_percent"] = float(plan.supports.density_percent)
        if plan.supports.angle_deg is not None:
            locked["support_angle_deg"] = float(plan.supports.angle_deg)
        if plan.supports.pattern is not None:
            overrides["support_pattern"] = str(plan.supports.pattern)
    else:
        locked["support_enable"] = False

    if plan.support_interface.enabled:
        overrides["support_interface_enable"] = True
        if plan.support_interface.height_mm is not None:
            overrides["support_interface_height_mm"] = float(plan.support_interface.height_mm)
        if plan.support_interface.density_percent is not None:
            overrides["support_interface_density_percent"] = float(
                plan.support_interface.density_percent
            )

    mode = _adhesion_mode(plan)
    if mode in ADHESION_MODES:
        locked["adhesion_type"] = mode
    if plan.brim.enabled and plan.brim.width_mm is not None:
        overrides["brim_width_mm"] = float(plan.brim.width_mm)

    if plan.temperature.nozzle_c is not None:
        locked["nozzle_temperature_c"] = int(plan.temperature.nozzle_c)
    if plan.temperature.bed_c is not None:
        locked["bed_temperature_c"] = int(plan.temperature.bed_c)
    if plan.temperature.first_layer_nozzle_c is not None:
        overrides["first_layer_temperature_c"] = int(plan.temperature.first_layer_nozzle_c)
    if plan.temperature.first_layer_bed_c is not None:
        overrides["first_layer_bed_temperature_c"] = int(plan.temperature.first_layer_bed_c)

    if plan.cooling.fan_percent is not None:
        locked["cooling_fan_percent"] = int(plan.cooling.fan_percent)
    if plan.cooling.min_layer_time_s is not None:
        overrides["min_layer_time_s"] = float(plan.cooling.min_layer_time_s)
    if plan.min_layer_time.seconds is not None:
        overrides["min_layer_time_s"] = float(plan.min_layer_time.seconds)

    if plan.speed.print_mm_s is not None:
        locked["print_speed_mm_s"] = float(plan.speed.print_mm_s)
    if plan.speed.travel_mm_s is not None:
        locked["travel_speed_mm_s"] = float(plan.speed.travel_mm_s)
    if plan.speed.first_layer_mm_s is not None:
        overrides["first_layer_speed_mm_s"] = float(plan.speed.first_layer_mm_s)
    if plan.speed.outer_wall_mm_s is not None:
        overrides["outer_wall_speed_mm_s"] = float(plan.speed.outer_wall_mm_s)
    if plan.speed.inner_wall_mm_s is not None:
        overrides["inner_wall_speed_mm_s"] = float(plan.speed.inner_wall_mm_s)
    if plan.speed.infill_mm_s is not None:
        overrides["infill_speed_mm_s"] = float(plan.speed.infill_mm_s)
    if plan.speed.support_mm_s is not None:
        overrides["support_speed_mm_s"] = float(plan.speed.support_mm_s)

    if plan.first_layer.speed_mm_s is not None:
        overrides["first_layer_speed_mm_s"] = float(plan.first_layer.speed_mm_s)
    if plan.first_layer.height_mm is not None:
        overrides["first_layer_height_mm"] = float(plan.first_layer.height_mm)
    if plan.first_layer.line_width_mm is not None:
        overrides["first_layer_line_width_mm"] = float(plan.first_layer.line_width_mm)
    if plan.first_layer.fan_percent is not None:
        overrides["first_layer_fan_percent"] = int(plan.first_layer.fan_percent)

    if plan.acceleration.value_mm_s2 is not None:
        locked["acceleration_mm_s2"] = float(plan.acceleration.value_mm_s2)
    if plan.acceleration.wall_mm_s2 is not None:
        overrides["wall_acceleration_mm_s2"] = float(plan.acceleration.wall_mm_s2)
    if plan.acceleration.travel_mm_s2 is not None:
        overrides["travel_acceleration_mm_s2"] = float(plan.acceleration.travel_mm_s2)

    retraction: dict[str, Any] = {}
    if plan.retraction.distance_mm is not None:
        retraction["distance_mm"] = float(plan.retraction.distance_mm)
    if plan.retraction.speed_mm_s is not None:
        retraction["speed_mm_s"] = float(plan.retraction.speed_mm_s)
    if retraction:
        locked["retraction"] = retraction
    if plan.retraction.z_hop_mm is not None and analysis.capabilities.supports("z_hop"):
        overrides["z_hop_mm"] = float(plan.retraction.z_hop_mm)

    return locked, overrides


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def validate_plan(plan: AiPrintPlan, analysis: PreAnalysis) -> ValidationOutcome:
    """Run every checklist check and resolve the parameters the plan may set."""
    # Support, adhesion and placement are properties of the chosen *pose*, not
    # of the model. Re-measure the orientation the plan actually picked before
    # comparing any claim against it.
    facts = evaluate_pose(analysis, plan.orientation.candidate_id)

    checks: list[Check] = []
    for category in CHECKLIST:
        fn = _CHECK_BY_CATEGORY.get(category)
        if fn is None:  # pragma: no cover - CHECKLIST and map are kept in sync
            raise KeyError(f"No validator for checklist category {category!r}")
        checks.append(fn(plan, analysis, facts))

    locked, overrides = resolve_parameters(plan, analysis)

    # Corrections from checks that did not fail are applied now: the plan is
    # usable, and the measurement wins wherever the two disagree. Corrections
    # attached to FAILED checks are deliberately *not* applied yet — if the
    # model replans, those values disappear with the attempt; if it does not,
    # `finalise()` applies them and records why.
    normalisations: list[Normalisation] = []
    for check in checks:
        fixes = check.corrections
        if not fixes:
            continue
        normalisations.extend(fixes)
        if not check.failed:
            for fix in fixes:
                _apply_override(locked, overrides, fix)

    # A plan that claims a category without saying why is a plan that was not
    # reasoned about. Recorded, not rejected: the numbers still have to hold.
    for category in CHECKLIST:
        cat = plan.category(category)
        if cat.status == "required" and not str(cat.reason).strip() and not cat.evidence:
            normalisations.append(
                Normalisation(
                    field=category,
                    model_value={"status": cat.status, "reason": cat.reason},
                    applied_value=None,
                    reason="category marked required with no reason and no evidence",
                )
            )

    outcome = ValidationOutcome(
        checks=checks, locked=locked, overrides=overrides, normalisations=normalisations
    )
    log.info(
        "print_plan.validated",
        passed=outcome.passed,
        failed=len(outcome.failed_checks),
        warnings=len(outcome.warning_checks),
        can_replan=outcome.can_replan,
    )
    return outcome
