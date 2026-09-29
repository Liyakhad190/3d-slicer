"""Technology and slicer registries.

Selection is data-driven end to end. There is no `if printer == "Ender"` anywhere
in this codebase, and there must never be: a machine is a profile row, a profile
names a technology and a dialect, and these registries resolve the rest.

`TechnologyRegistry` answers "what does this class of machine need?".
`SlicerRegistry` answers "which engine can provide it, and is it healthy?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.errors import InsufficientInformationError
from app.core.logging import get_logger
from app.slicing.configuration import PrintConfiguration, Technology

log = get_logger(__name__)


@dataclass(frozen=True)
class TechnologySpec:
    """What one additive-manufacturing technology requires."""

    key: Technology
    display_name: str
    #: Formats a *model* may be supplied in.
    input_formats: tuple[str, ...]
    #: Formats a *print file* may be emitted in.
    output_formats: tuple[str, ...]
    #: Whether a solid mesh is required (false for volume/powder data).
    requires_solid_mesh: bool
    #: Slicing pipeline kind. Different technologies genuinely slice differently.
    pipeline: str
    #: Whether a real slicing engine is wired up for this technology yet.
    supported: bool
    validation_requirements: tuple[str, ...] = ()
    notes: str = ""


#: Every technology the platform is designed around.
#:
#: Only FDM has a validated engine wired up. The others are declared with
#: `supported=False` so the platform can say "resin slicing is not implemented"
#: honestly instead of routing a resin job through a plastic slicer.
TECHNOLOGIES: dict[str, TechnologySpec] = {
    "fdm": TechnologySpec(
        key="fdm",
        display_name="Fused Deposition Modelling (FDM/FFF)",
        input_formats=("stl", "obj", "3mf", "ply", "amf", "glb", "gltf"),
        output_formats=("gcode", "3mf"),
        requires_solid_mesh=True,
        pipeline="planar_slicing_then_extrusion",
        supported=True,
        validation_requirements=(
            "build_volume", "thermal_envelope", "speed_limits", "coordinate_frame",
        ),
        notes="Fully implemented. CuraEngine backend, validated G-code.",
    ),
    "msla": TechnologySpec(
        key="msla",
        display_name="Masked Stereolithography (MSLA) - resin",
        input_formats=("stl", "obj", "3mf"),
        output_formats=("photon", "chiptube", "slice"),
        requires_solid_mesh=True,
        pipeline="layer_to_image",
        supported=False,
        validation_requirements=("build_volume", "resin_profile", "exposure_calibration"),
        notes=(
            "NOT IMPLEMENTED. Requires an image-slice engine and a resin profile "
            "(exposure time, peel speed, bottom exposure). Routing a resin job "
            "through a plastic slicer would be wrong, so the platform refuses."
        ),
    ),
    "dlp": TechnologySpec(
        key="dlp",
        display_name="Digital Light Processing (DLP) - resin",
        input_formats=("stl", "obj", "3mf"),
        output_formats=("chiptube", "slice"),
        requires_solid_mesh=True,
        pipeline="layer_to_image",
        supported=False,
        validation_requirements=("build_volume", "resin_profile", "exposure_calibration"),
        notes="NOT IMPLEMENTED. See msla.",
    ),
    "sls": TechnologySpec(
        key="sls",
        display_name="Selective Laser Sintering - powder bed",
        input_formats=("stl", "obj", "3mf", "step"),
        output_formats=("sco", "photon"),
        requires_solid_mesh=False,
        pipeline="hatching_and_contouring",
        supported=False,
        validation_requirements=("build_volume", "powder_profile", "thermal_profile"),
        notes=(
            "NOT IMPLEMENTED. Powder-bed needs hatch/contour generation, a powder "
            "profile and thermal management the platform has no model of."
        ),
    ),
    "slm": TechnologySpec(
        key="slm",
        display_name="Selective Laser Melting - metal",
        input_formats=("stl", "obj", "3mf", "step"),
        output_formats=("mfp"),
        requires_solid_mesh=False,
        pipeline="hatching_and_contouring",
        supported=False,
        validation_requirements=("build_volume", "powder_profile", "thermal_profile"),
        notes="NOT IMPLEMENTED. See sls.",
    ),
}


class TechnologyRegistry:
    """Lookup over `TECHNOLOGIES`."""

    def get(self, key: str) -> TechnologySpec:
        spec = TECHNOLOGIES.get(str(key).lower())
        if spec is None:
            raise InsufficientInformationError(
                f"Unknown printing technology {key!r}.",
                details={"known": sorted(TECHNOLOGIES)},
            )
        return spec

    def supported(self) -> list[TechnologySpec]:
        return [spec for spec in TECHNOLOGIES.values() if spec.supported]

    def all(self) -> list[TechnologySpec]:
        return list(TECHNOLOGIES.values())

    def require_supported(self, key: str) -> TechnologySpec:
        spec = self.get(key)
        if not spec.supported:
            raise InsufficientInformationError(
                f"{spec.display_name} slicing is not implemented. {spec.notes}",
                details={"technology": spec.key, "supported": False},
            )
        return spec

    def accepts_input(self, key: str, model_format: str) -> bool:
        return str(model_format).lower() in self.get(key).input_formats


@runtime_checkable
class SlicerAdapter(Protocol):
    """The subset of the adapter port the registry needs."""

    key: str
    display_name: str
    license: str

    def is_available(self) -> bool: ...
    def probe(self) -> str | None: ...
    def get_capabilities(self) -> dict[str, Any]: ...
    def get_supported_formats(self) -> list[str]: ...
    def get_supported_technologies(self) -> list[str]: ...
    def validate_configuration(self, configuration: PrintConfiguration) -> list[str]: ...
    def prepare_configuration(self, configuration: PrintConfiguration) -> dict[str, Any]: ...


@dataclass
class SlicerCandidate:
    adapter: SlicerAdapter
    score: int
    problems: list[str] = field(default_factory=list)
    version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.adapter.key,
            "display_name": self.adapter.display_name,
            "version": self.version,
            "score": self.score,
            "problems": self.problems,
        }


class SlicerRegistry:
    """Chooses a slicing backend from capability, not from vendor names."""

    def __init__(self, adapters: list[SlicerAdapter] | None = None) -> None:
        self._adapters: dict[str, SlicerAdapter] = {}
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: SlicerAdapter) -> None:
        self._adapters[adapter.key] = adapter
        log.info("slicer.registered", engine=adapter.key, name=adapter.display_name)

    def get(self, key: str) -> SlicerAdapter:
        adapter = self._adapters.get(key)
        if adapter is None:
            raise InsufficientInformationError(
                f"No slicing engine is registered as {key!r}.",
                details={"registered": sorted(self._adapters)},
            )
        return adapter

    def all(self) -> list[SlicerAdapter]:
        return list(self._adapters.values())

    def available(self) -> list[SlicerAdapter]:
        return [a for a in self._adapters.values() if a.is_available()]

    def describe(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for adapter in self._adapters.values():
            try:
                capabilities = adapter.get_capabilities()
            except Exception as exc:  # pragma: no cover - defensive
                capabilities = {"error": str(exc)}
            out.append(
                {
                    "engine": adapter.key,
                    "display_name": adapter.display_name,
                    "license": adapter.license,
                    "available": adapter.is_available(),
                    "version": adapter.probe(),
                    "capabilities": capabilities,
                }
            )
        return out

    def candidates(self, configuration: PrintConfiguration) -> list[SlicerCandidate]:
        """Score every registered engine against a configuration.

        Higher is better. The score is a *ranking*, and a candidate with a
        non-empty `problems` list is never selectable.
        """
        results: list[SlicerCandidate] = []
        wanted_output = self._required_output_formats(configuration)

        for adapter in self._adapters.values():
            score = 0
            problems: list[str] = []

            if not adapter.is_available():
                problems.append("The engine binary is not available.")
            else:
                score += 50

            technologies = {t.lower() for t in adapter.get_supported_technologies()}
            if configuration.technology.lower() in technologies:
                score += 40
            else:
                problems.append(
                    f"{adapter.display_name} does not slice "
                    f"{configuration.technology.upper()}."
                )

            formats = {f.lower() for f in adapter.get_supported_formats()}
            if wanted_output <= formats:
                score += 30
            else:
                problems.append(
                    f"{adapter.display_name} cannot emit {sorted(wanted_output - formats)}."
                )

            try:
                config_problems = adapter.validate_configuration(configuration)
            except Exception as exc:  # pragma: no cover
                config_problems = [f"validation raised: {exc}"]
            if config_problems:
                problems.extend(config_problems)
            else:
                score += 20

            results.append(
                SlicerCandidate(
                    adapter=adapter,
                    score=score,
                    problems=problems,
                    version=adapter.probe() if adapter.is_available() else None,
                )
            )

        return sorted(results, key=lambda c: c.score, reverse=True)

    def select(self, configuration: PrintConfiguration) -> SlicerAdapter:
        """Return the best usable adapter, or refuse with a reason.

        Refusing is the correct outcome when nothing qualifies. The platform's
        job is to stop an unsafe job, not to produce a file.
        """
        ranked = self.candidates(configuration)
        usable = [c for c in ranked if not c.problems]
        if not usable:
            detail = {
                "configuration": configuration.to_dict(),
                "candidates": [c.to_dict() for c in ranked],
            }
            best = ranked[0] if ranked else None
            reason = (
                f"No registered slicing engine can slice this job. The closest "
                f"candidate is {best.adapter.display_name}: {'; '.join(best.problems)}"
                if best
                else "No slicing engines are registered."
            )
            raise InsufficientInformationError(reason, details=detail)

        chosen = usable[0]
        log.info(
            "slicer.selected",
            engine=chosen.adapter.key,
            score=chosen.score,
            alternatives=[c.adapter.key for c in usable[1:]],
        )
        return chosen.adapter

    @staticmethod
    def _required_output_formats(configuration: PrintConfiguration) -> set[str]:
        spec = TECHNOLOGIES.get(configuration.technology.lower())
        if spec is None:
            return {"gcode"}
        # FDM always needs machine G-code; 3MF is offered alongside it.
        return {"gcode"} if spec.supported else set(spec.output_formats)


_registry: SlicerRegistry | None = None


def get_registry() -> SlicerRegistry:
    """The process-wide registry, with the available backends registered.

    Adding an engine means adding one line here. Nothing else changes.
    """
    global _registry
    if _registry is None:
        from app.slicing.cura_adapter import CuraEngineAdapter

        _registry = SlicerRegistry([CuraEngineAdapter()])
        log.info("slicer.registry_ready", engines=[a.key for a in _registry.all()])
    return _registry


def get_technology_registry() -> TechnologyRegistry:
    return TechnologyRegistry()
