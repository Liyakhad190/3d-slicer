"""Engine capability probing: what the selected slicer can actually do.

The AI plan is written against *canonical* print-preparation capabilities, not
against Cura setting names. Whether a capability exists is answered here, by
reading the engine's own settings schema at runtime.

This is the mechanism that keeps the platform slicer-agnostic. `PRINT_PLAN`
never asks "is `retraction_hop_enabled` set"; it asks "does this engine
support z-hop". If the engine answers with a different key name, or with no
key at all, the plan either maps to the right key or the category is marked
`unsupported` and the UI says so instead of pretending the setting exists.

Nothing here guesses. A capability is supported only when every key it needs
is present in the engine's schema defaults; a missing definition file yields
"unknown", never "supported".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.catalog.capabilities import PrinterCapabilities
from app.core.logging import get_logger

log = get_logger(__name__)

CAPABILITIES_VERSION = "engine-capabilities-1.0.0"

#: Canonical capability -> the settings it needs in the engine schema.
#:
#: Every entry is a list of alternatives; a capability is supported when at
#: least one alternative has *all* of its keys present. Alternatives exist
#: because engines name the same concept differently (Cura uses
#: `retraction_hop`, others may use `speed_zhop`), and because some engines
#: fold several concepts into one key.
FEATURE_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    "first_layer_temperature": (("material_print_temperature_layer_0",),),
    "first_layer_bed_temperature": (("material_bed_temperature_layer_0",),),
    "first_layer_speed": (("speed_layer_0",),),
    "first_layer_line_width": (("layer_height_0",),),
    "bed_temperature": (("material_bed_temperature",),),
    "nozzle_temperature": (("material_print_temperature",),),
    "wall_speed": (("speed_wall_0",), ("speed_wall_1",)),
    "infill_speed": (("speed_infill",),),
    "travel_speed": (("speed_travel",),),
    "retraction": (("retraction_amount", "retraction_speed"),),
    "z_hop": (
        ("retraction_hop_enabled", "retraction_hop"),
        ("retraction_hop_enabled", "speed_z_hop"),
        ("speed_zhop",),
    ),
    "cooling_fan": (("cool_fan_speed_max",), ("cool_fan_speed_0",)),
    "min_layer_time": (("cool_min_layer_time",),),
    "acceleration": (("acceleration_print",),),
    "top_bottom_layers": (("top_layers", "bottom_layers"),),
    "infill": (("infill_line_distance", "infill_pattern"),),
    "wall_line_count": (("wall_line_count",),),
    "line_width": (("line_width",),),
    "support": (("support_enable", "support_type"),),
    "support_interface": (
        ("support_interface_enable", "support_interface_height", "support_interface_density"),
        ("support_interface_enable", "support_interface_height"),
    ),
    "support_raft": (("raft_airgap",),),
    "brim": (("brim_width",),),
    "adhesion_type": (("adhesion_type",),),
    "brim_width": (("brim_width",),),
}


@dataclass(frozen=True)
class FeatureSupport:
    feature: str
    supported: bool
    status: str  # supported | unsupported | unknown
    reason: str
    settings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "supported": self.supported,
            "status": self.status,
            "reason": self.reason,
            "settings": list(self.settings),
        }


@dataclass(frozen=True)
class EngineCapabilities:
    """The selected engine's honest answer about what it can do."""

    engine_key: str
    display_name: str
    version: str | None
    available: bool
    features: dict[str, FeatureSupport] = field(default_factory=dict)
    probe_version: str = CAPABILITIES_VERSION

    @property
    def supported_features(self) -> frozenset[str]:
        return frozenset(k for k, v in self.features.items() if v.supported)

    @property
    def unknown_features(self) -> frozenset[str]:
        return frozenset(k for k, v in self.features.items() if v.status == "unknown")

    def supports(self, feature: str) -> bool:
        entry = self.features.get(feature)
        return bool(entry and entry.supported)

    def status(self, feature: str) -> str:
        entry = self.features.get(feature)
        if entry is None:
            return "unsupported"
        return entry.status

    def reason(self, feature: str) -> str:
        entry = self.features.get(feature)
        if entry is None:
            return "This capability is not declared for the selected engine."
        return entry.reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_version": self.probe_version,
            "engine": self.engine_key,
            "display_name": self.display_name,
            "version": self.version,
            "available": self.available,
            "features": {k: v.to_dict() for k, v in sorted(self.features.items())},
        }


def _schema_for(adapter: Any) -> dict[str, Any] | None:
    """Read the engine's settings schema, or `None` if it cannot be read."""
    probe = getattr(adapter, "schema_defaults", None)
    if probe is None:
        return None
    try:
        schema = probe()
    except Exception as exc:  # pragma: no cover - defensive around a bad install
        log.warning("capabilities.schema_failed", engine=getattr(adapter, "key", "?"), error=str(exc))
        return None
    if not isinstance(schema, dict):  # pragma: no cover - defensive
        return None
    return schema


def probe_capabilities(adapter: Any) -> EngineCapabilities:
    """Resolve every canonical capability against the engine's own schema."""
    schema = _schema_for(adapter)
    version: str | None
    try:
        version = adapter.probe() if getattr(adapter, "is_available", lambda: False)() else None
    except Exception:  # pragma: no cover - defensive
        version = None

    features: dict[str, FeatureSupport] = {}
    for feature, alternatives in FEATURE_KEYS.items():
        if schema is None:
            features[feature] = FeatureSupport(
                feature=feature,
                supported=False,
                status="unknown",
                reason=(
                    "The engine's settings schema could not be read, so this "
                    "capability could not be confirmed."
                ),
            )
            continue

        matched: tuple[str, ...] | None = None
        for alternative in alternatives:
            if all(key in schema for key in alternative):
                matched = alternative
                break

        if matched is not None:
            features[feature] = FeatureSupport(
                feature=feature,
                supported=True,
                status="supported",
                reason=f"Declared by the engine as {', '.join(matched)}.",
                settings=matched,
            )
        else:
            wanted = alternatives[0]
            features[feature] = FeatureSupport(
                feature=feature,
                supported=False,
                status="unsupported",
                reason=(
                    "The selected engine does not expose "
                    f"{', '.join(wanted)}; this plan cannot use it."
                ),
            )

    capabilities = EngineCapabilities(
        engine_key=str(getattr(adapter, "key", "unknown")),
        display_name=str(getattr(adapter, "display_name", "Unknown engine")),
        version=version,
        available=bool(getattr(adapter, "is_available", lambda: False)()),
        features=features,
    )
    log.debug(
        "capabilities.probed",
        engine=capabilities.engine_key,
        supported=sorted(capabilities.supported_features),
    )
    return capabilities
