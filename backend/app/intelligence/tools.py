"""Allowlisted, read-only tools.

The rule this module enforces: a tool may only *read* state that the server
chose to put in front of it, and may only return JSON. There is no tool that
writes a file, runs a command, touches the database, changes a machine profile,
or produces G-code. Adding one is a deliberate change to `ALLOWED_TOOLS`, not an
accident of prompt wording.

Two ways in, and only two:

* `ToolRegistry.invoke()` — server-side, called by the planner with names it has
  already decided on. This is the default path.
* model-directed tool selection, which exists only when
  `LLM_TOOL_CALLING_ENABLED=true`, and is still restricted to this registry.

The model can therefore ask for context it does not have, but it can only ask
for context on this list, and only after the server has supplied a reader for
it. Measured on `qwen2.5:1.5b`, model-directed selection picked the wrong tool
often enough that it stays off by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from app.core.logging import get_logger

log = get_logger(__name__)

#: Hard ceiling. No tool may take more than this, whatever a model asks for.
TOOL_TIMEOUT_SECONDS = 5


class ToolNotAllowedError(PermissionError):
    """Raised when a tool name is not on the allowlist."""


class ToolInputError(ValueError):
    """Raised when a tool is called with arguments it does not accept."""


@dataclass(frozen=True)
class ToolSpec:
    """The declaration of one tool. Also the prompt-facing description."""

    name: str
    description: str
    #: Argument names and their types. Deliberately flat and short: a long
    #: signature costs prefill on every call.
    parameters: dict[str, str] = field(default_factory=dict)
    #: Always "none". Present so the invariant is data, not a comment.
    side_effects: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "side_effects": self.side_effects,
        }


@dataclass
class ToolResult:
    name: str
    data: dict[str, Any]
    #: A short string for the prompt; `data` is for the code and the API response.
    summary: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "data": self.data,
        }


# -- readers --------------------------------------------------------------
#
# Each reader is supplied by the planner and closes over already-loaded state.
# A reader cannot reach the database, the filesystem or a process on its own.

Reader = Callable[[Mapping[str, Any]], dict[str, Any]]

#: name -> spec, and name -> reader. Both are process-global because the tool
#: set is fixed at import time; only the *context* varies per request.
_specs: dict[str, ToolSpec] = {}
_readers: dict[str, Reader] = {}


def register_reader(spec: ToolSpec, reader: Reader) -> None:
    """Register a reader for a spec. Used by the built-ins and by tests."""
    _specs[spec.name] = spec
    _readers[spec.name] = reader


def reset_registry() -> None:
    """Restore the built-in tool set. Test hook."""
    _specs.clear()
    _readers.clear()
    for _spec, _reader in _BUILTIN_TOOLS:
        register_reader(_spec, _reader)


# -- the tools ------------------------------------------------------------

MODEL_SUMMARY = ToolSpec(
    name="model_summary",
    description="Dimensions, bounding box and mesh integrity for the user's model.",
    parameters={"asset_id": "string"},
)
MACHINE_LIMITS = ToolSpec(
    name="machine_limits",
    description="Hard limits of the selected printer: bed size and temperature caps.",
)
MATERIAL_PROFILE = ToolSpec(
    name="material_profile",
    description="Temperatures and speed limits from the selected material profile.",
)
SLICER_CAPABILITIES = ToolSpec(
    name="slicer_capabilities",
    description="Installed slicer engines and the settings each one supports.",
)


def _model_summary(context: Mapping[str, Any]) -> dict[str, Any]:
    summary = context.get("model_summary")
    if not isinstance(summary, dict):
        return {"available": False}
    return {"available": True, **summary}


def _machine_limits(context: Mapping[str, Any]) -> dict[str, Any]:
    limits = context.get("machine_limits")
    if not isinstance(limits, dict):
        return {"available": False}
    return {"available": True, **limits}


def _material_profile(context: Mapping[str, Any]) -> dict[str, Any]:
    profile = context.get("material_profile")
    if not isinstance(profile, dict):
        return {"available": False}
    return {"available": True, **profile}


def _slicer_capabilities(context: Mapping[str, Any]) -> dict[str, Any]:
    capabilities = context.get("slicer_capabilities")
    if not isinstance(capabilities, dict):
        return {"available": False}
    return {"available": True, **capabilities}


#: The built-in tool set, restorable via `reset_registry`.
_BUILTIN_TOOLS: tuple[tuple[ToolSpec, Reader], ...] = (
    (MODEL_SUMMARY, _model_summary),
    (MACHINE_LIMITS, _machine_limits),
    (MATERIAL_PROFILE, _material_profile),
    (SLICER_CAPABILITIES, _slicer_capabilities),
)

for _spec, _reader in _BUILTIN_TOOLS:
    register_reader(_spec, _reader)

#: The allowlist. `invoke` checks membership against this, not against
#: `_readers`, so a reader registered at runtime by a test cannot be reached by
#: anything that has not also been granted access here.
ALLOWED_TOOLS: frozenset[str] = frozenset(_specs)


class ToolRegistry:
    """Executes allowlisted read-only tools against a prepared context."""

    def __init__(self, context: Mapping[str, Any] | None = None) -> None:
        self._context = dict(context or {})

    @property
    def allowed(self) -> frozenset[str]:
        return ALLOWED_TOOLS

    def describe(self) -> list[dict[str, Any]]:
        """The prompt-facing tool list. Only allowed tools appear."""
        return [_specs[name].to_dict() for name in sorted(ALLOWED_TOOLS)]

    def invoke(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> ToolResult:
        if name not in ALLOWED_TOOLS:
            raise ToolNotAllowedError(
                f"Tool {name!r} is not allowed. Allowed: {sorted(ALLOWED_TOOLS)}"
            )
        reader = _readers.get(name)
        if reader is None:
            raise ToolNotAllowedError(f"Tool {name!r} has no registered reader.")

        arguments = dict(arguments or {})
        spec = _specs[name]
        unknown = set(arguments) - set(spec.parameters)
        if unknown:
            raise ToolInputError(
                f"Tool {name!r} does not accept {sorted(unknown)}; it accepts "
                f"{sorted(spec.parameters) or 'no arguments'}."
            )

        import time

        started = time.perf_counter()
        data = reader({**self._context, **arguments})
        if not isinstance(data, dict):
            raise ToolInputError(f"Tool {name!r} returned a non-object result.")
        duration_ms = int((time.perf_counter() - started) * 1000)
        if duration_ms > TOOL_TIMEOUT_SECONDS * 1000:
            log.warning("llm.tool_slow", tool=name, duration_ms=duration_ms)

        return ToolResult(
            name=name,
            data=data,
            summary=_summarise(name, data),
            duration_ms=duration_ms,
        )

    def collect(self, names: tuple[str, ...]) -> list[ToolResult]:
        """Run several tools, skipping any that are unavailable.

        A tool whose data is missing returns `{"available": False}` rather than
        raising, so a partially-populated context still yields a usable plan.
        """
        results: list[ToolResult] = []
        for name in names:
            if name not in ALLOWED_TOOLS:
                log.warning("llm.tool_skipped", tool=name, reason="not_allowed")
                continue
            try:
                results.append(self.invoke(name))
            except (ToolNotAllowedError, ToolInputError) as exc:
                log.warning("llm.tool_failed", tool=name, error=str(exc))
        return results


def _summarise(name: str, data: Mapping[str, Any]) -> str:
    if not data.get("available", True):
        return f"{name}: not available for this request."
    if name == "model_summary":
        dims = data.get("dimensions") or {}
        if isinstance(dims, dict) and dims:
            return "Model dimensions: " + ", ".join(
                f"{axis} {value} mm" for axis, value in dims.items()
            )
    if name == "machine_limits":
        bed = data.get("bed_size_mm")
        if isinstance(bed, dict) and bed:
            return "Build volume: " + ", ".join(
                f"{axis} {value} mm" for axis, value in bed.items()
            )
    if name == "material_profile":
        nozzle = data.get("nozzle_temp_range_c")
        if isinstance(nozzle, (list, tuple)) and len(nozzle) == 2:
            return f"Material nozzle range: {nozzle[0]}-{nozzle[1]} C"
    return name


def render_tool_context(results: list[ToolResult]) -> str:
    """Render tool output for the prompt."""
    lines = [result.summary for result in results if result.summary]
    return "\n".join(lines)