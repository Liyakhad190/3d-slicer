"""Artifact description for a generated output file.

One record the API, the database and the download UI all agree on. It is built
from data the pipeline already produced (`GeneratedFile` + the job it belongs
to) so nothing has to be recomputed or guessed, and it always carries the
provenance an operator needs to trust the file: who made it, with what, from
which machine profile, and whether it passed validation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OutputArtifact:
    filename: str
    format: str
    bytes: int
    checksum: str  # sha256:<hex>
    printer: str | None = None
    printer_name: str | None = None
    material: str | None = None
    nozzle_diameter_mm: float | None = None
    slicer: str | None = None
    slicer_version: str | None = None
    configuration_fingerprint: str | None = None
    validation: dict[str, Any] = field(default_factory=dict)
    media_type: str = "application/octet-stream"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "format": self.format,
            "bytes": self.bytes,
            "checksum": self.checksum,
            "printer": self.printer,
            "printer_name": self.printer_name,
            "material": self.material,
            "nozzle_diameter_mm": self.nozzle_diameter_mm,
            "slicer": self.slicer,
            "slicer_version": self.slicer_version,
            "configuration_fingerprint": self.configuration_fingerprint,
            "validation": self.validation,
            "media_type": self.media_type,
            "metadata": self.metadata,
        }


def sha256_of(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def describe_artifact(
    *,
    filename: str,
    fmt: str,
    payload: bytes,
    media_type: str,
    printer: str | None = None,
    printer_name: str | None = None,
    material: str | None = None,
    nozzle_diameter_mm: float | None = None,
    slicer: str | None = None,
    slicer_version: str | None = None,
    configuration_fingerprint: str | None = None,
    validation: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> OutputArtifact:
    """Compose the artifact record. `validation` must be explicit — a file that
    has not been checked is described as unvalidated, never as passing."""
    return OutputArtifact(
        filename=filename,
        format=fmt,
        bytes=len(payload),
        checksum=sha256_of(payload),
        printer=printer,
        printer_name=printer_name,
        material=material,
        nozzle_diameter_mm=nozzle_diameter_mm,
        slicer=slicer,
        slicer_version=slicer_version,
        configuration_fingerprint=configuration_fingerprint,
        validation=validation or {"status": "unvalidated", "checks": 0},
        media_type=media_type,
        metadata=metadata or {},
    )


def describe_generated_file(
    generated: Any,
    *,
    payload: bytes,
    printer: str | None = None,
    printer_name: str | None = None,
    material: str | None = None,
    nozzle_diameter_mm: float | None = None,
    slicer: str | None = None,
    slicer_version: str | None = None,
    configuration_fingerprint: str | None = None,
    validation: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> OutputArtifact:
    """Build an artifact record from a stored `GeneratedFile` row.

    The bytes are passed in rather than read from the row: callers already
    have them, and hashing a blob a second time here would be the only place
    in the system that could disagree with the `sha256` already recorded.
    """
    fmt = (generated.kind or "gcode").lstrip(".").lower()
    return describe_artifact(
        filename=generated.filename or f"print.{fmt}",
        fmt=fmt,
        payload=payload,
        media_type=_media_type_for(fmt),
        printer=printer,
        printer_name=printer_name,
        material=material,
        nozzle_diameter_mm=nozzle_diameter_mm,
        slicer=slicer,
        slicer_version=slicer_version,
        configuration_fingerprint=configuration_fingerprint,
        validation=validation,
        metadata=metadata,
    )


def _media_type_for(fmt: str) -> str:
    from app.slicing.output.base import OutputFormat

    try:
        return OutputFormat(fmt).media_type
    except ValueError:
        return "application/octet-stream"
