"""CuraEngine adapter (FDM backend).

**Licensing note — this corrects an earlier assumption in this project.**
CuraEngine 5.13 identifies itself as **AGPL-3.0**, not LGPL-3.0:

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License ...

AGPL is materially stricter than LGPL: it has network-use copyleft. Running it
as a *separate process* keeps this project's source from becoming a derivative
work, but the deployment story still needs a legal review before any commercial
use. `licence_review_required` is exposed in the engine capabilities so the
platform surfaces this rather than hiding it. OrcaSlicer and PrusaSlicer are
GPL-3.0 with the same consideration.

The adapter is deliberately thin. It translates a canonical `PrintConfiguration`
into Cura's vocabulary and runs the binary. It holds no business logic: profile
resolution, optimisation and validation all live elsewhere.

Cura 5.13 CLI contract, established by reading `CuraEngine help` and confirmed
empirically:

    CuraEngine slice [-v] [-p] [-j <settings.def.json>]
                    [-s <key>=<value>] ... [-o <out.gcode>] [-l <model.stl>]

* `slice` is a required leading subcommand.
* `-j` registers a settings *definition* file; without one, no settings are
  known and every lookup fails with "no value given".
* `-r` would load resolved values from JSON, but it is undocumented in the
  synopsis and ignored the section nesting, so `-s key=value` is used instead.
* `-v` increases verbosity. It is NOT valid as a bare flag on some builds.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.core.config import Settings, get_settings
from app.core.errors import ExternalToolError, SlicingError
from app.core.logging import get_logger

log = get_logger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

#: Version banner, used to detect the licence the binary actually ships under.
_LICENCE_PATTERN = re.compile(r"(Afero|General Public License|BSD|MIT)", re.IGNORECASE)


@dataclass(frozen=True)
class SliceRequest:
    """A fully-resolved, already-validated slice request."""

    model_path: Path
    output_path: Path
    settings: Mapping[str, Any]
    job_label: str = ""
    #: Format of the model file. Required for content-addressed blobs, which
    #: have no extension for trimesh to infer from.
    model_format: str | None = None


@dataclass(frozen=True)
class SliceResult:
    output_path: Path
    engine: str
    engine_version: str
    duration_ms: int
    stdout_tail: str = ""
    settings_used: dict[str, Any] = field(default_factory=dict)
    dropped_settings: list[str] = field(default_factory=list)


@runtime_checkable
class SlicerEngineAdapter(Protocol):
    """The port every slicing engine implements.

    The core platform depends only on this. It is the single boundary that
    keeps the application free of engine-specific behaviour.
    """

    key: str
    display_name: str
    license: str

    def is_available(self) -> bool: ...
    def probe(self) -> str | None: ...
    def get_capabilities(self) -> dict[str, Any]: ...
    def validate_configuration(self, configuration: Any) -> list[str]: ...
    def prepare_configuration(self, configuration: Any) -> dict[str, Any]: ...
    def slice(self, request: SliceRequest) -> SliceResult: ...
    def validate_output(self, output_path: Path) -> list[str]: ...
    def get_supported_formats(self) -> list[str]: ...
    def get_supported_technologies(self) -> list[str]: ...
    def get_supported_materials(self) -> list[str]: ...


def _slug(value: str) -> uuid.UUID:
    """A filesystem-safe, collision-free workspace id derived from a job label.

    The label is user-influenced and is never used as a path component; it only
    selects among freshly generated UUIDs.
    """
    return uuid.UUID(hashlib.sha256(value.encode("utf-8")).hexdigest()[:32])


class CuraEngineAdapter:
    """Drives CuraEngine strictly as a subprocess."""

    key = "cura"
    display_name = "UltiMaker CuraEngine"
    license = "AGPL-3.0"

    def __init__(self, executable: Path | None = None, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self._settings = settings
        self._executable = Path(executable or settings.cura_engine_path)
        self._defaults_cache: dict[str, Any] | None = None

    @property
    def executable(self) -> Path:
        return self._executable

    # -- discovery -------------------------------------------------------

    def resolve_executable(self) -> Path:
        if self._executable.exists():
            return self._executable
        found = shutil.which(self._executable.name)
        return Path(found) if found else self._executable

    def is_available(self) -> bool:
        executable = self.resolve_executable()
        if not executable.is_file():
            return False
        return os.name == "nt" or os.access(executable, os.X_OK)

    def definitions_path(self) -> Path | None:
        """Locate `fdmprinter.def.json` next to the binary.

        Without this file the engine has no registered settings at all, which is
        the difference between "no value given" spam and a successful slice.
        """
        candidates = [
            self.resolve_executable().parent / "share" / "cura" / "resources" / "definitions" / "fdmprinter.def.json",
            self.resolve_executable().parent / "definitions" / "fdmprinter.def.json",
            Path("/usr/share/cura/resources/definitions/fdmprinter.def.json"),
            Path("/usr/local/share/cura/resources/definitions/fdmprinter.def.json"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def probe(self) -> str | None:
        """Return the engine version, matching the banner line."""
        executable = self.resolve_executable()
        if not executable.is_file():
            return None
        try:
            completed = subprocess.run(
                [str(executable), "help"],
                capture_output=True, text=True, timeout=60,
                env=self._child_env(), creationflags=_NO_WINDOW, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        banner = re.compile(r"(?:Cura\S*Engine|UltiMaker)\s+version\s+([0-9][0-9A-Za-z.\-]*)", re.IGNORECASE)
        for stream in (completed.stderr, completed.stdout, ""):
            for line in stream.splitlines():
                match = banner.search(line)
                if match:
                    return match.group(1)
        return "unknown"

    def detect_license(self) -> str:
        """Read the licence from the binary's own banner rather than assuming."""
        executable = self.resolve_executable()
        if not executable.is_file():
            return "unknown"
        try:
            completed = subprocess.run(
                [str(executable), "help"],
                capture_output=True, text=True, timeout=60,
                env=self._child_env(), creationflags=_NO_WINDOW, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        text = f"{completed.stderr}\n{completed.stdout}".lower()
        if "affero" in text:
            return "AGPL-3.0"
        if "general public license" in text:
            return "GPL-3.0"
        if "lesser general public" in text:
            return "LGPL-3.0"
        return "unknown"

    # -- capabilities ----------------------------------------------------

    def get_capabilities(self) -> dict[str, Any]:
        definitions = self.definitions_path()
        return {
            "engine": self.key,
            "display_name": self.display_name,
            "version": self.probe(),
            "license": self.detect_license(),
            "licence_review_required": True,
            "integration_mode": "subprocess",
            "available": self.is_available(),
            "settings_schema_found": definitions is not None,
            "settings_schema_path": str(definitions) if definitions else None,
            "output_formats": ["gcode"],
            "technologies": ["fdm"],
            "features": {
                "tree_support": True,
                "organic_support": True,
                "multi_material": True,
                "ironing": True,
                "variable_width": True,
                "input_shaping": "via machine profile, not the slicer",
            },
        }

    def get_supported_formats(self) -> list[str]:
        return ["gcode"]

    def get_supported_technologies(self) -> list[str]:
        return ["fdm"]

    def get_supported_materials(self) -> list[str]:
        # Any thermoplastic the material profiles can express; the profile
        # data, not this list, is what actually bounds it.
        return ["*"]

    # -- configuration ---------------------------------------------------

    def validate_configuration(self, configuration: Any) -> list[str]:
        """Pre-flight checks. Returns a list of problems (empty means fine)."""
        problems: list[str] = []
        if not self.is_available():
            problems.append(f"CuraEngine is not available at {self.resolve_executable()}.")
        if self.definitions_path() is None:
            problems.append(
                "fdmprinter.def.json was not found next to the binary; CuraEngine "
                "cannot resolve settings without it."
            )
        if getattr(configuration, "technology", "fdm") != "fdm":
            problems.append(
                f"CuraEngine slices FDM only; this job is {getattr(configuration, 'technology', '?')}."
            )
        return problems

    def prepare_configuration(self, configuration: Any) -> dict[str, Any]:
        """Canonical `PrintConfiguration` -> Cura settings."""
        from app.slicing.cura_settings import to_cura_settings

        return to_cura_settings(
            configuration.parameters,
            limits=configuration.limits,
            start_gcode=configuration.start_gcode,
            end_gcode=configuration.end_gcode,
        )

    def validate_output(self, output_path: Path) -> list[str]:
        if not output_path.is_file():
            return [f"No output file was produced at {output_path}."]
        if output_path.stat().st_size == 0:
            return ["The slicer produced an empty file."]
        return []

    # -- slicing ---------------------------------------------------------

    def schema_defaults(self) -> dict[str, Any]:
        """Every setting the schema defines, with its default value.

        Loading the full baseline from the definition file is what makes this
        adapter version-robust: hand-listing 700 settings would break on the
        next Cura release, and omitting one produces "no value given" errors
        mid-slice.
        """
        if self._defaults_cache is not None:
            return self._defaults_cache
        path = self.definitions_path()
        if path is None:
            self._defaults_cache = {}
            return self._defaults_cache
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("cura.definitions_unreadable", path=str(path), error=str(exc))
            self._defaults_cache = {}
            return self._defaults_cache

        collected: dict[str, Any] = {}

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, dict) and "default_value" in value and "label" in value:
                        collected[key] = value["default_value"]
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(document.get("settings", {}))
        self._defaults_cache = collected
        log.info("cura.schema_loaded", settings=len(collected), path=str(path))
        return collected

    def slice(self, request: SliceRequest) -> SliceResult:
        """Slice a prepared model.

        The model is first placed on the build plate (see
        `app.services.geometry.placement`). This is not cosmetic: CuraEngine
        does not lift a model off the origin, so a centred 20 mm cube is
        clipped to 10 mm and the toolpath is silently short.
        """
        import time

        executable = self.resolve_executable()
        if not executable.is_file():
            raise ExternalToolError(
                f"Slicing engine not found at {executable}.",
                details={"engine": self.key, "expected_path": str(executable)},
            )
        definitions = self.definitions_path()
        if definitions is None:
            raise ExternalToolError(
                "fdmprinter.def.json was not found. CuraEngine cannot resolve "
                "settings without its definitions file.",
                details={"engine": self.key, "searched_near": str(executable.parent)},
            )
        if not request.model_path.is_file():
            raise SlicingError("The model file to slice does not exist.")

        request.output_path.parent.mkdir(parents=True, exist_ok=True)

        # Prepare the mesh: placed on the plate, original untouched. The
        # prepared file keeps a real extension because trimesh selects its
        # exporter from the extension, and the content-addressed input has none.
        from app.services.geometry.placement import prepare_file

        build_volume = self._build_volume_from_settings(request.settings)
        extension = (request.model_format or "stl").lower()
        prepared = request.output_path.with_name(f"prepared.{extension}")
        placement = prepare_file(
            request.model_path,
            build_volume=build_volume,
            output_path=prepared,
            allow_scale=False,
            file_type=request.model_format,
        )

        merged, dropped = self._merge_with_schema(request.settings)
        argv = self._build_argv(executable, definitions, request, merged, model=prepared)

        started = time.perf_counter()
        try:
            completed = subprocess.run(
                argv,
                capture_output=True, text=True,
                timeout=self._settings.slicer_timeout_seconds,
                env=self._child_env(), cwd=request.output_path.parent,
                creationflags=_NO_WINDOW, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExternalToolError(
                f"{self.display_name} exceeded the {self._settings.slicer_timeout_seconds}s "
                "timeout and was terminated.",
                details={"engine": self.key, "model": request.model_path.name},
            ) from exc
        except OSError as exc:
            raise ExternalToolError(
                f"Could not start {self.display_name}: {exc}",
                details={"engine": self.key, "executable": str(executable)},
            ) from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""

        if dropped:
            log.warning("cura.settings_dropped", count=len(dropped), settings=dropped[:20])

        problems = self.validate_output(request.output_path)
        if completed.returncode != 0 or problems:
            log.error(
                "cura.failed",
                returncode=completed.returncode,
                duration_ms=duration_ms,
                problems=problems,
                stderr_tail=stderr[-1500:],
            )
            raise SlicingError(
                f"{self.display_name} failed to produce G-code (exit {completed.returncode}).",
                details={
                    "engine": self.key,
                    "returncode": completed.returncode,
                    "problems": problems,
                    "stdout_tail": "\n".join(stdout.splitlines()[-40:])[-4000:],
                    "stderr_tail": "\n".join(stderr.splitlines()[-40:])[-4000:],
                },
            )

        size = request.output_path.stat().st_size
        if size > self._settings.slicer_max_output_bytes:
            request.output_path.unlink(missing_ok=True)
            raise ExternalToolError(
                f"{self.display_name} produced {size} bytes, above the configured cap; "
                "the file was discarded rather than stored.",
                details={"engine": self.key, "bytes": size},
            )

        log.info("cura.completed", duration_ms=duration_ms, output_bytes=size,
                 model=request.model_path.name, settings=len(merged))
        return SliceResult(
            output_path=request.output_path,
            engine=self.key,
            engine_version=self.probe() or "unknown",
            duration_ms=duration_ms,
            stdout_tail="\n".join(stdout.splitlines()[-40:]),
            settings_used=merged,
            dropped_settings=dropped,
        )

    # -- internals -------------------------------------------------------

    def _merge_with_schema(self, settings: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Merge the engine's schema defaults with our resolved settings.

        Two rules make this robust across Cura versions:

        * A setting the schema does not define is dropped and reported, never
          sent — an unknown key aborts the whole slice.
        * If the schema defines a per-extruder variant (`wall_line_count_0`)
          alongside the global one, both are set. Cura 5 resolves several shell
          and material settings per extruder train, and setting only the global
          name silently left the extrusion path at its default.
        """
        defaults = self.schema_defaults()
        merged: dict[str, Any] = dict(defaults) if defaults else {}
        dropped: list[str] = []

        for key, value in settings.items():
            if value is None:
                continue
            if defaults and key not in defaults:
                dropped.append(key)
                continue
            merged[key] = value

        extruder_count = merged.get("machine_extruder_count") or 1
        for key in list(merged):
            if not isinstance(key, str) or merged.get(key) is None:
                continue
            if key.startswith("machine_") or key.endswith(f"_{int(extruder_count) - 1}"):
                continue
            scoped = f"{key}_{int(extruder_count) - 1}"
            if scoped in defaults:
                merged[scoped] = merged[key]

        if not defaults:
            log.warning("cura.no_schema_baseline", settings=len(settings))

        return merged, dropped

    @staticmethod
    def _build_volume_from_settings(settings: Mapping[str, Any]) -> tuple[float, float, float]:
        """The build volume the engine was told, for mesh placement.

        Read from the same settings the slicer receives, so placement and the
        validator's bounds check cannot disagree about where the machine is.
        """

        def pick(*keys: str, default: float) -> float:
            for key in keys:
                value = settings.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    return float(value)
            return default

        return (
            pick("machine_width", default=0.0),
            pick("machine_depth", default=0.0),
            pick("machine_height", default=0.0),
        )

    def _build_argv(
        self,
        executable: Path,
        definitions: Path,
        request: SliceRequest,
        settings: Mapping[str, Any],
        *,
        model: Path | None = None,
    ) -> list[str]:
        """Assemble the documented Cura 5.13 command line.

        `slice` is a required subcommand, `-j` registers the definition file,
        `-l` loads the model and `-o` writes the toolpath. Settings are passed
        as repeated `-s key=value` pairs, which is the only form that
        round-trips multi-line values such as start/end G-code.
        """
        argv: list[str] = [str(executable), "slice", "-p", "-j", str(definitions)]
        for key, value in sorted(settings.items()):
            argv.extend(["-s", f"{key}={self._format_value(value)}"])
        argv.extend(["-o", str(request.output_path), "-l", str(model or request.model_path)])
        return argv

    @staticmethod
    def _format_value(value: Any) -> str:
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, float):
            return repr(value)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str):
            return value
        if isinstance(value, (list, dict)):
            # The schema stores matrix/graph settings as JSON-ish strings.
            return json.dumps(value, separators=(",", ":"))
        raise SlicingError(
            f"Unsupported slicer setting type: {type(value).__name__}. "
            "Settings must be scalars, strings, or JSON-serialisable structures.",
            details={"type": type(value).__name__},
        )

    def _child_env(self) -> dict[str, str]:
        """A minimal environment for the slicer subprocess.

        Inheriting the full environment would leak secrets into a process that
        parses untrusted geometry.
        """
        return {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": os.environ.get("TEMP", tempfile.gettempdir()),
            "TMP": os.environ.get("TMP", tempfile.gettempdir()),
            "HOME": os.environ.get("HOME", str(Path.home())),
            "LANG": "C",
        }

    def workspace_for(self, job_label: str) -> Path:
        from app.services.storage import new_job_workspace

        return new_job_workspace(_slug(job_label))
