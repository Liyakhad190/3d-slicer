"""Registry of output formatters + the printer-profile capability declaration.

Printer profiles declare what they can accept, in their own vocabulary:

    machine_restrictions.output_formats = ["gcode", "gx"]

The declaration and the registry answer two different questions and are never
conflated:

* *declared* — the manufacturer/workflow for this machine can consume the file
  at all (a profile that says nothing gets G-code only, so GX can never be
  offered by accident);
* *registered* — this deployment knows how to produce the file.

A format is offered only when both are true; a format that is declared but not
producible is reported with the reason instead of being hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.slicing.output.base import (
    OutputFormat,
    OutputFormatUnsupported,
    OutputFormatter,
    FormatterAvailability,
)

log = get_logger(__name__)

#: What a profile that says nothing about output formats gets. G-code is the
#: universal interchange format every FDM machine here accepts.
DEFAULT_OUTPUT_FORMATS: tuple[str, ...] = ("gcode",)


def declared_output_formats(machine_profile: Any) -> tuple[str, ...]:
    """Read the profile's `output_formats` declaration, defensively.

    Unknown names are kept as strings so they surface in `describe()` rather
    than being silently dropped; conversion to :class:`OutputFormat` happens
    where a format object is actually needed.
    """
    restrictions = getattr(machine_profile, "machine_restrictions", None) or {}
    declared = restrictions.get("output_formats")
    if declared is None:
        return DEFAULT_OUTPUT_FORMATS
    if isinstance(declared, str):
        declared = [declared]
    values = [str(v).strip().lower() for v in declared if str(v).strip()]
    return tuple(values) or DEFAULT_OUTPUT_FORMATS


@dataclass
class OutputFormatterRegistry:
    _formatters: dict[str, OutputFormatter] = field(default_factory=dict)

    def register(self, formatter: OutputFormatter) -> None:
        self._formatters[formatter.key] = formatter

    def get(self, key: str) -> OutputFormatter | None:
        return self._formatters.get(str(key).strip().lower())

    def all(self) -> list[OutputFormatter]:
        return list(self._formatters.values())

    def describe(self) -> list[dict[str, Any]]:
        return [f.describe() for f in self._formatters.values()]

    # -- capability gating ------------------------------------------------

    def supported_for(self, machine_profile: Any) -> list[dict[str, Any]]:
        """Everything this profile may be offered, with its availability."""
        declared = set(declared_output_formats(machine_profile))
        rows: list[dict[str, Any]] = []
        for formatter in self.all():
            state: FormatterAvailability = formatter.availability()
            rows.append(
                {
                    "key": formatter.key,
                    "format": formatter.format.value,
                    "extension": formatter.extension,
                    "declared_by_profile": formatter.key in declared,
                    "advertised": formatter.key in declared and state.available,
                    **state.to_dict(),
                }
            )
        rows.sort(key=lambda r: (not r["declared_by_profile"], r["key"]))
        return rows

    def advertised_formats(self, machine_profile: Any) -> list[str]:
        return [r["key"] for r in self.supported_for(machine_profile) if r["advertised"]]

    def require_supported(self, machine_profile: Any, key: str) -> OutputFormatter:
        """Resolve a requested format, refusing anything not declared."""
        formatter = self.get(key)
        if formatter is None:
            known = sorted(self._formatters)
            raise OutputFormatUnsupported(
                f"Unknown output format {key!r}. Known formats: {', '.join(known)}.",
                details={"requested": key, "known": known},
            )

        declared = declared_output_formats(machine_profile)
        if formatter.key not in declared:
            raise OutputFormatUnsupported(
                f"This printer does not support {formatter.format.value} output. "
                f"It declares: {', '.join(declared)}.",
                details={
                    "requested": formatter.key,
                    "declared_by_profile": list(declared),
                },
            )
        return formatter


_registry: OutputFormatterRegistry | None = None


def get_output_registry() -> OutputFormatterRegistry:
    """The process-wide registry. Adding a format is one `register` call."""
    global _registry
    if _registry is None:
        from app.slicing.output.gcode import GCodeFormatter
        from app.slicing.output.gx import GXFormatter

        _registry = OutputFormatterRegistry()
        _registry.register(GCodeFormatter())
        _registry.register(GXFormatter())
    return _registry


def reset_output_registry() -> None:
    """Test hook: drop the cached registry so a new one is built."""
    global _registry
    _registry = None
