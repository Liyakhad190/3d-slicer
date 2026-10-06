"""FlashForge `.gx` output.

What `.gx` actually is
----------------------
A `.gx` file is **not** G-code with a different extension. A genuine file
produced by FlashForge's own tooling (`ffslicer` 2.4.4, shipped with
FlashPrint 5) begins with a short container header — `00 00 00 00 FlashPrint\\0`
after a leading marker byte — followed by an **X3G binary command stream** (the
Sailfish/RepRapG machine protocol), with metadata entries such as
`pre_ext:0.20` and `layer:0.28` embedded along the way. It is machine code for
a FlashForge controller, not a text file.

Because the file is a different protocol, it cannot be produced by renaming or
re-encoding G-code text: it needs an **encoder** that walks the toolpath and
emits X3G commands. That encoder is the adapter boundary this module defines.

Status of this installation
---------------------------
No G-code-to-X3G encoder ships with this repository, and none is installed on
the host: `gpx` (the usual open-source converter) is absent, and FlashPrint's
`ffslicer.exe` — which *does* contain a `Gcode2X3g` implementation — exposes
only a slicing CLI (`--model`, `--config`, `--x3g`, `--gcode`) and refuses a
G-code file as input (`read stl failed!`). It therefore cannot convert an
already-sliced toolpath, and using it to re-slice would duplicate the slicing
logic this architecture deliberately keeps in one place.

Consequently:

* :class:`GXFormatter` is a complete, registered, selectable formatter.
* It produces a real file only when a :class:`GXEncoder` is registered and
  available. Until then it reports exactly what is missing and raises
  :class:`OutputFormatUnavailable` — it never emits placeholder bytes.
* :func:`validate_gx` checks the structure of whatever an encoder returns and
  rejects anything that is just the input G-code again.
"""

from __future__ import annotations

import glob
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.core.logging import get_logger
from app.slicing.output.base import (
    FormatRequest,
    FormatResult,
    FormatterAvailability,
    OutputFormat,
    OutputFormatRejected,
    OutputFormatter,
)

log = get_logger(__name__)

#: Observed container marker: `<marker> 00 00 00 00 "FlashPrint\\0"`.
GX_MAGIC = b"FlashPrint"
GX_MAGIC_OFFSET = 5

#: Below this a file cannot contain both a header and a usable stream.
GX_MIN_BYTES = 64


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def validate_gx(payload: bytes, *, source_gcode: bytes | None = None) -> list[str]:
    """Structural validation for a generated `.gx`. Empty list means valid.

    The checks are the ones we can stand behind from real FlashForge output:
    it exists, it carries the container header, it is binary machine protocol
    rather than text, and — the one that matters most — it is *not* the G-code
    that was handed in, which is exactly what a renamed file would look like.
    """
    problems: list[str] = []

    if not payload:
        return ["The GX file is empty."]

    if len(payload) < GX_MIN_BYTES:
        problems.append(
            f"The GX file is only {len(payload)} bytes; a real file carries a "
            f"container header plus an X3G command stream."
        )

    if payload[GX_MAGIC_OFFSET : GX_MAGIC_OFFSET + len(GX_MAGIC)] != GX_MAGIC:
        # Accept the magic anywhere in the leading block so a future header
        # revision that moves it does not fail closed on a genuine file.
        if GX_MAGIC not in payload[:128]:
            problems.append(
                "Missing the FlashForge container header: this does not look "
                "like a .gx file."
            )

    # Plain UTF-8 G-code masquerading as GX — the rename failure mode.
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    else:
        if "G1 " in text or "G0 " in text or "M104" in text:
            problems.append(
                "The file is plain G-code text, not an X3G command stream."
            )

    if source_gcode is not None and payload == source_gcode:
        problems.append(
            "The produced file is byte-identical to the source G-code: "
            "renaming a .gcode file to .gx is not a valid conversion."
        )

    return problems


# --------------------------------------------------------------------------- #
# encoders — the actual G-code -> X3G adapter boundary
# --------------------------------------------------------------------------- #


class GXEncoder(Protocol):
    """Converts a validated G-code toolpath into FlashForge `.gx` bytes."""

    key: str

    def availability(self) -> FormatterAvailability: ...

    def encode(self, gcode: bytes, *, context: dict[str, Any]) -> bytes: ...

    def describe(self) -> dict[str, Any]: ...


@dataclass
class GXEncoderRegistry:
    """Every encoder we know how to drive. One is enough to unlock GX."""

    _encoders: dict[str, GXEncoder] = field(default_factory=dict)

    def register(self, encoder: GXEncoder) -> None:
        self._encoders[encoder.key] = encoder

    def get(self, key: str) -> GXEncoder | None:
        return self._encoders.get(key)

    def all(self) -> list[GXEncoder]:
        return list(self._encoders.values())

    def first_available(self) -> tuple[GXEncoder | None, FormatterAvailability]:
        reasons: list[dict[str, Any]] = []
        for encoder in self._encoders.values():
            state = encoder.availability()
            if state.available:
                return encoder, state
            reasons.append({"encoder": encoder.key, **state.to_dict()})
        if not reasons:
            return None, FormatterAvailability(
                available=False,
                reason=(
                    "No G-code-to-X3G encoder is registered. Install GPX and "
                    "set GPX_EXECUTABLE, or register a GXEncoder."
                ),
                details={"encoders": []},
            )
        return None, FormatterAvailability(
            available=False,
            reason=(
                "No G-code-to-X3G encoder is available: "
                + "; ".join(f"{r['encoder']}: {r['reason']}" for r in reasons)
            ),
            details={"encoders": reasons},
        )

    def describe(self) -> list[dict[str, Any]]:
        return [e.describe() for e in self._encoders.values()]


def find_flashprint_engine() -> str | None:
    """Locate FlashPrint's `ffslicer.exe`, if the host has FlashPrint installed.

    Present for diagnostics only: `ffslicer` slices meshes and cannot accept an
    already-sliced G-code file, so it is *not* a valid encoder for this
    pipeline. Reporting it separately is how an operator learns why the obvious
    tool cannot be used here.
    """
    configured = os.environ.get("FLASHFORGE_ENGINE") or os.environ.get("FFSLICER_PATH")
    if configured and Path(configured).is_file():
        return configured

    roots = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")]
    for root in filter(None, roots):
        pattern = str(Path(root) / "FlashForge" / "*" / "engine" / "ffslicer.exe")
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


class GpxEncoder:
    """Drives [GPX](https://github.com/kliment/Printrun)'s `gpx` converter.

    GPX is the established open-source G-code to X3G compiler. It is an
    external runtime dependency: when it is not installed this encoder reports
    itself unavailable with the exact lookup that failed, and GX stays locked.
    """

    key = "gpx"

    def __init__(self, executable: str | None = None) -> None:
        self._configured = executable or os.environ.get("GPX_EXECUTABLE")

    def _resolve(self) -> str | None:
        if self._configured:
            return self._configured if Path(self._configured).is_file() else None
        return shutil.which("gpx")

    def availability(self) -> FormatterAvailability:
        resolved = self._resolve()
        if resolved:
            return FormatterAvailability(
                available=True,
                reason=f"GPX found at {resolved}",
                details={"executable": resolved},
            )
        searched = self._configured or "PATH (command `gpx`)"
        return FormatterAvailability(
            available=False,
            reason=(
                "GPX, the G-code to X3G compiler, is not installed. Set "
                "GPX_EXECUTABLE to its path or put `gpx` on PATH."
            ),
            details={
                "searched": searched,
                "install": "https://github.com/kliment/Printrun (gpx)",
                "flashprint_engine": find_flashprint_engine(),
                "flashprint_engine_usable_as_encoder": False,
                "flashprint_engine_note": (
                    "FlashPrint's ffslicer.exe contains an X3G encoder but only "
                    "accepts mesh input, so it cannot convert an already-sliced "
                    "toolpath."
                ),
            },
        )

    def encode(self, gcode: bytes, *, context: dict[str, Any]) -> bytes:
        resolved = self._resolve()
        if not resolved:
            from app.slicing.output.base import OutputFormatUnavailable

            raise OutputFormatUnavailable(
                "GPX is not available; cannot encode G-code to X3G.",
                details={"format": OutputFormat.gx.value},
            )

        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory(prefix="gx-") as tmp:
            src = Path(tmp) / "input.gcode"
            dst = Path(tmp) / "output.gx"
            src.write_bytes(gcode)
            # `gpx [options] infile outfile`; machine dialect comes from the
            # caller when the profile knows one, otherwise GPX's default.
            cmd = [resolved, *context.get("gpx_args", []), str(src), str(dst)]
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if proc.returncode != 0 or not dst.is_file():
                from app.slicing.output.base import OutputFormatRejected

                raise OutputFormatRejected(
                    "GPX failed to convert the toolpath to X3G.",
                    details={
                        "returncode": proc.returncode,
                        "stderr": proc.stderr.decode("utf-8", errors="replace")[:2000],
                    },
                )
            return dst.read_bytes()

    def describe(self) -> dict[str, Any]:
        return {"key": self.key, **self.availability().to_dict()}


@dataclass
class RecordingEncoder:
    """A registered encoder used by tests to exercise the real GX path.

    It emits a structurally genuine container (header + X3G-shaped body that is
    provably not the input G-code), so the formatter, validator and artifact
    pipeline are all exercised for real. It is never registered in production.
    """

    payload_tail: bytes = b"\xd5\x00\x00\x00\x00\x00\x00\x00"
    key: str = "recording"

    def availability(self) -> FormatterAvailability:
        return FormatterAvailability(available=True, reason="test encoder")

    def encode(self, gcode: bytes, *, context: dict[str, Any]) -> bytes:
        header = b"\x99\x00\x00\x00\x00" + GX_MAGIC + b"\x00"
        body = b"\x95\x00\x00\x00\x00layer:0.20\x00"
        body += b"\x95\x00\x00\x00\x00pre_ext:0.20\x00"
        body += self.payload_tail * 8
        body += str(len(gcode)).encode("ascii") + b"\x00"
        return header + body

    def describe(self) -> dict[str, Any]:
        return {"key": self.key, **self.availability().to_dict()}


_encoder_registry = GXEncoderRegistry()


def get_encoder_registry() -> GXEncoderRegistry:
    """The process-wide encoder registry, with GPX registered by default."""
    global _encoder_registry
    if not _encoder_registry.all():
        _encoder_registry.register(GpxEncoder())
    return _encoder_registry


# --------------------------------------------------------------------------- #
# the formatter
# --------------------------------------------------------------------------- #


class GXFormatter(OutputFormatter):
    """Produces a real FlashForge `.gx` from the sliced toolpath."""

    key = "gx"
    format = OutputFormat.gx

    def __init__(self, encoders: GXEncoderRegistry | None = None) -> None:
        self._encoders = encoders

    @property
    def encoders(self) -> GXEncoderRegistry:
        return self._encoders if self._encoders is not None else get_encoder_registry()

    def availability(self) -> FormatterAvailability:
        encoder, state = self.encoders.first_available()
        if encoder is None:
            # Surface the failing lookup itself (which binary, which path was
            # searched) rather than making callers dig through a list.
            details = dict(state.details)
            entries = details.get("encoders") or []
            if entries:
                entry = entries[0]
                details.update(
                    {
                        k: v
                        for k, v in entry.items()
                        if k not in {"encoder", "available", "reason", "details"}
                    }
                )
                details.update(entry.get("details") or {})
            return FormatterAvailability(
                available=False, reason=state.reason, details=details
            )
        details = dict(state.details)
        details["encoder"] = encoder.key
        return FormatterAvailability(
            available=True, reason=state.reason, details=details
        )

    def render(self, request: FormatRequest) -> FormatResult:
        self._require_available()
        encoder, _ = self.encoders.first_available()
        assert encoder is not None  # availability() just passed

        payload = encoder.encode(request.gcode, context=dict(request.context))
        problems = validate_gx(payload, source_gcode=request.gcode)
        if problems:
            raise OutputFormatRejected(
                "The generated .gx file failed validation and was discarded.",
                details={"format": self.format.value, "problems": problems},
            )

        return FormatResult(
            payload=payload,
            format=self.format,
            filename=self.filename_for(request),
            media_type=self.media_type,
            metadata={
                "encoder": encoder.key,
                "source_bytes": len(request.gcode),
                "gx_bytes": len(payload),
            },
        )

    def validate(self, request: FormatRequest, result: FormatResult) -> list[str]:
        return validate_gx(result.payload, source_gcode=request.gcode)
