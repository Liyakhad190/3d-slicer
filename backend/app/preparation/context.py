"""Assembles the deterministic context the model is allowed to see.

The prompt is built here, server-side, from measured facts only. Nothing in it
is written by the model, and nothing in it is a recommendation: the context
states what the mesh measures, what the machine will allow, what the material
needs, what the engine exposes, and what the previous attempt got wrong.

That last section is what makes the replan loop honest. On attempt two the
model is told, in numbers, which checks it failed and why. It is not told the
answer; it is told the constraint it violated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.intelligence.rag.retriever import format_context, retrieve
from app.intelligence.schemas import IntentPriorities
from app.preparation.analysis import PreAnalysis
from app.preparation.validators import ValidationOutcome

#: The print plan is structurally large. This keeps the prompt inside what a
#: 1.5B model can still condition on rather than drowning it in JSON.
MAX_CONTEXT_CHARS = 9000

SYSTEM_PROMPT = (
    "You are a print preparation engineer. You receive measured facts about a "
    "3D model, a machine, a material and a slicing engine, and you return a "
    "print preparation plan as JSON.\n"
    "Rules:\n"
    "1. Every checklist category must be present. If a category does not apply, "
    "set status to 'not_required' and say why in reason. If the engine cannot "
    "provide it, set status to 'unsupported'.\n"
    "2. Every reason must cite a number, a limit or a profile name. Evidence "
    "lines must be specific: 'overhang 412 mm2 at 45 deg', not 'good geometry'.\n"
    "3. Orientation must be an id from the measured candidate list. You cannot "
    "invent a rotation.\n"
    "4. Temperatures and speeds must fall inside the bands given. A value "
    "outside them will be rejected.\n"
    "5. Support must agree with the measured overhang requirement.\n"
    "6. You never output G-code, coordinates, or machine code. You propose "
    "print preparation decisions; a deterministic validator checks them."
)


@dataclass
class PreparationContext:
    """The facts, pre-rendered, plus any retrieval the request earns."""

    analysis: PreAnalysis
    intent: IntentPriorities = field(default_factory=IntentPriorities)
    objective: str = "balanced"
    material: dict[str, Any] = field(default_factory=dict)
    user_request: str = ""
    #: Findings from the previous attempt, rendered as constraint text.
    feedback: list[str] = field(default_factory=list)
    attempt: int = 1
    knowledge: list[Any] = field(default_factory=list)

    # -- sections --------------------------------------------------------

    def _machine_section(self) -> dict[str, Any]:
        limits = self.analysis.limits
        return {
            "name": getattr(self.analysis, "machine_name", "machine"),
            "build_volume_mm": {
                "x": limits.build_x,
                "y": limits.build_y,
                "z": limits.build_z,
            },
            "origin_convention": limits.origin_convention,
            "nozzle_diameter_mm": limits.nozzle_diameter_mm,
            "max_print_speed_mm_s": limits.max_print_speed_mm_s,
            "max_travel_speed_mm_s": limits.max_travel_speed_mm_s,
            "max_acceleration_mm_s2": limits.max_acceleration_mm_s2,
            "max_volumetric_flow_mm3s": limits.max_volumetric_flow_mm3s,
            "nozzle_temperature_band_c": [limits.safe_nozzle_min_c, limits.safe_nozzle_max_c],
            "bed_temperature_band_c": [limits.bed_min_c, limits.bed_max_c],
        }

    def _orientation_section(self) -> dict[str, Any]:
        return {
            "recommended": self.analysis.recommended_candidate_id,
            "candidates": [
                {
                    "id": c.id,
                    "rotation_deg": list(c.rotation_deg),
                    "strategy": c.strategy,
                    "extents_mm": [round(v, 2) for v in c.extents_mm],
                    "contact_area_mm2": round(c.contact_area_mm2, 1),
                    "overhang_percent": round(c.overhang_percent, 2),
                    "height_mm": round(c.height_mm, 2),
                    "stability": round(c.stability, 3),
                    "fits": c.fits,
                    "score": c.score,
                }
                for c in self.analysis.candidates
            ],
        }

    def sections(self) -> dict[str, Any]:
        analysis = self.analysis
        return {
            "attempt": self.attempt,
            "objective": self.objective,
            "intent": self.intent.to_dict(),
            "user_request": self.user_request,
            "geometry": analysis.geometry_summary,
            "machine": self._machine_section(),
            "material": self.material,
            "engine": {
                "key": analysis.capabilities.engine_key,
                "display_name": analysis.capabilities.display_name,
                "version": analysis.capabilities.version,
                "supported_features": sorted(analysis.capabilities.supported_features),
                "unsupported_features": sorted(
                    k for k, v in analysis.capabilities.features.items() if v.status == "unsupported"
                ),
            },
            "orientation": self._orientation_section(),
            "placement": analysis.placement.to_dict(),
            "support_requirement": analysis.support.to_dict(),
            "adhesion_assessment": analysis.adhesion.to_dict(),
            "bridges": analysis.bridges,
            "blocking_findings": analysis.blocking_findings,
            "findings": [f.to_dict() for f in analysis.report.findings[:8]],
            "constraints_from_previous_attempt": self.feedback,
        }

    def render(self) -> str:
        payload = json.dumps(self.sections(), indent=2, default=str)
        if len(payload) > MAX_CONTEXT_CHARS:
            # Drop the least load-bearing sections rather than truncating JSON
            # mid-structure, which would guarantee a parse failure.
            trimmed = dict(self.sections())
            trimmed.pop("findings", None)
            trimmed.pop("bridges", None)
            trimmed["orientation"]["candidates"] = trimmed["orientation"]["candidates"][:6]
            payload = json.dumps(trimmed, indent=2, default=str)
            if len(payload) > MAX_CONTEXT_CHARS:
                payload = payload[:MAX_CONTEXT_CHARS]
        return payload

    def build_prompt(self) -> str:
        parts = [
            "Measured facts about this print job:\n" + self.render(),
        ]
        if self.knowledge:
            parts.append("Platform knowledge:\n" + format_context(self.knowledge))
        parts.append("Respond with the print plan JSON only.")
        return "\n\n".join(parts)


def feedback_from(outcome: ValidationOutcome) -> list[str]:
    """Render a failed validation as constraint lines for the next attempt."""
    lines: list[str] = []
    for check in outcome.checks:
        if check.status not in ("FAILED", "WARNING"):
            continue
        line = f"[{check.status}] {check.category}: {check.reason}"
        if check.code:
            line = f"{line} (code {check.code})"
        lines.append(line[:400])
        if len(lines) >= 12:
            break
    return lines


def collect_knowledge(user_request: str, *, settings: Any = None) -> list[Any]:
    """Retrieve platform knowledge for the request, ignoring retrieval failures."""
    if not user_request.strip():
        return []
    try:
        if settings is None:
            return retrieve(user_request)
        return retrieve(user_request, settings=settings)
    except Exception:  # pragma: no cover - retrieval is optional enrichment
        return []
