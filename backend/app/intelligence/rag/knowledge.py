"""The platform's own printing knowledge, as retrievable documents.

This is the corpus the assistant reasons over. It is written by hand from the
platform's actual behaviour — the same rules the deterministic code enforces —
so retrieval reinforces the system instead of contradicting it.

Nothing here is invented advice. If a document says a thing, the optimizer or
validator in this repo does it.
"""

from __future__ import annotations

from app.intelligence.rag.base import KnowledgeDocument

#: Immutable corpus. Tuples, because a module-level list invites accidental
#: mutation from a test and the corpus must stay stable across requests.
KNOWLEDGE_DOCUMENTS: tuple[KnowledgeDocument, ...] = (
    KnowledgeDocument(
        id="priorities-strength",
        title="When the user wants strength",
        content=(
            "High strength means more perimeters, more top and bottom layers, and a "
            "higher infill density. This raises both material use and print time. "
            "Strength on an FDM part comes mostly from wall count and layer bonding, "
            "not from infill: raising perimeters buys more strength per gram than "
            "raising infill density."
        ),
        tags=("intent", "strength", "infill", "walls"),
    ),
    KnowledgeDocument(
        id="priorities-speed",
        title="When the user wants speed",
        content=(
            "Speed is bought with a higher layer height, fewer perimeters, a lower "
            "infill density and more aggressive acceleration and travel settings. "
            "Each of these reduces surface quality or structural strength. Speed is "
            "the priority most often traded against surface quality."
        ),
        tags=("intent", "speed", "quality", "layer-height"),
    ),
    KnowledgeDocument(
        id="priorities-material",
        title="When the user wants to save material",
        content=(
            "Material efficiency means lower infill density, fewer solid layers, and "
            "a sparse infill pattern such as gyroid or lightning where the geometry "
            "allows it. This reduces part strength substantially, so do not silently "
            "apply it to a part the user described as load-bearing."
        ),
        tags=("intent", "material", "infill", "cost"),
    ),
    KnowledgeDocument(
        id="priorities-surface",
        title="When the user wants a good surface",
        content=(
            "Surface quality comes from a lower layer height, more top layers, and a "
            "slower outer wall speed. It conflicts directly with speed. A good "
            "surface finish is the priority most often worth spending print time on."
        ),
        tags=("intent", "quality", "layer-height", "walls"),
    ),
    KnowledgeDocument(
        id="supports-overhangs",
        title="Supports and overhangs",
        content=(
            "An overhang steeper than roughly 50 degrees from vertical generally needs "
            "support. If the user provides an orientation, respect it and only warn. "
            "If the user has not supplied one and has asked to avoid supports, prefer "
            "an orientation that reduces overhang area, but note that this platform "
            "does not yet run orientation optimisation automatically, so the choice is "
            "surfaced to the user rather than silently applied."
        ),
        tags=("intent", "supports", "orientation", "overhang"),
    ),
    KnowledgeDocument(
        id="materials-profile",
        title="Material profiles are the source of truth",
        content=(
            "Nozzle temperatures, bed temperatures and maximum volumetric speed come "
            "from the stored material profile, never from a model estimate. The "
            "assistant may express a preference such as a slower cool-down for a "
            "bridge-heavy part, but it must not assert a specific temperature as "
            "fact unless the profile contains it."
        ),
        tags=("materials", "temperature", "profile", "safety"),
    ),
    KnowledgeDocument(
        id="machine-limits",
        title="Machine limits are never negotiated",
        content=(
            "Bed size, maximum nozzle temperature, maximum bed temperature and maximum "
            "print speed come from the printer profile and are hard limits. No user "
            "request and no model suggestion can exceed them. If a request cannot be "
            "met within the limits, say so plainly and offer the nearest option."
        ),
        tags=("machine", "limits", "safety", "bed"),
    ),
    KnowledgeDocument(
        id="time-and-material-limits",
        title="Hard time and material budgets",
        content=(
            "A stated maximum print time or maximum material weight is a ceiling, not "
            "a target. When a ceiling is given, prefer settings that fit under it "
            "even at some cost to quality, and report the estimated time and weight "
            "so the user can see the result before slicing."
        ),
        tags=("constraints", "time", "material", "budget"),
    ),
    KnowledgeDocument(
        id="safety-and-filament",
        title="What the assistant must not do",
        content=(
            "The assistant cannot generate G-code, cannot select a slicer engine, and "
            "cannot change a machine limit. It produces intent and preferences only, "
            "which deterministic code turns into a validated configuration. If the "
            "user asks for something outside that boundary, explain the boundary "
            "rather than attempting it."
        ),
        tags=("safety", "limits", "gcode"),
    ),
    KnowledgeDocument(
        id="ambiguous-requests",
        title="Ambiguous requests",
        content=(
            "When a request could mean two materially different things, for example "
            "'make it strong' on a cosmetic part, ask one specific question rather "
            "than guessing. A single well-aimed question is better than a wrong print "
            "that takes hours."
        ),
        tags=("clarification", "conversation"),
    ),
)