"""Output format abstraction: one sliced toolpath, many deliverable files.

The pipeline produces exactly one thing — a validated machine toolpath (G-code)
from a universal :class:`~app.slicing.configuration.PrintConfiguration`. What
the operator finally downloads is a *presentation* of that toolpath, and the
presentation differs per manufacturer:

    PrintConfiguration -> SlicerAdapter -> toolpath (G-code)
                                              |
                                       OutputFormatter
                                        /          \
                                   .gcode          .gx
                                      |              |
                                 validate         validate
                                        \\          /
                                          artifact

A formatter is therefore not allowed to slice, re-optimise, or otherwise
re-decide anything about the job. It receives the already-validated toolpath
bytes and turns them into one file format. That boundary is what keeps GX (and
any future format) from forking the pipeline.

Two rules are enforced structurally rather than by convention:

* A formatter that cannot run says so through :meth:`OutputFormatter.availability`
  and raises :class:`OutputFormatUnavailable`. It never emits best-effort bytes.
* A printer only ever sees formats its profile declares, so a machine that has
  no GX workflow cannot be handed a GX download link.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fastapi import status

from app.core.errors import AppError


class OutputFormat(str, Enum):
    """A deliverable file format. Deliberately open for new members."""

    gcode = "gcode"
    gx = "gx"
    bgcode = "bgcode"
    three_mf = "3mf"
    g = "g"

    @property
    def extension(self) -> str:
        return {
            OutputFormat.gcode: "gcode",
            OutputFormat.gx: "gx",
            OutputFormat.bgcode: "bgcode",
            OutputFormat.three_mf: "3mf",
            OutputFormat.g: "g",
        }[self]

    @property
    def media_type(self) -> str:
        return {
            OutputFormat.gcode: "text/x-gcode",
            OutputFormat.gx: "application/octet-stream",
            OutputFormat.bgcode: "application/octet-stream",
            OutputFormat.three_mf: "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
            OutputFormat.g: "text/x-gcode",
        }[self]


class OutputFormatUnsupported(AppError):
    """The selected printer does not declare this output format."""

    code = "OUTPUT_FORMAT_UNSUPPORTED"
    http_status = status.HTTP_409_CONFLICT
    message = "This printer does not support the requested output format."


class OutputFormatUnavailable(AppError):
    """The printer declares the format, but a required encoder is missing."""

    code = "OUTPUT_FORMAT_UNAVAILABLE"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "The requested output format cannot be produced right now."


class OutputFormatRejected(AppError):
    """The formatter produced bytes that failed validation. Nothing is stored."""

    code = "OUTPUT_FORMAT_REJECTED"
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "The generated file did not pass validation and was discarded."


@dataclass(frozen=True)
class FormatterAvailability:
    """Whether a formatter can run *here*, and why not when it cannot."""

    available: bool
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "details": self.details,
        }


@dataclass(frozen=True)
class FormatRequest:
    """Everything a formatter is allowed to see.

    `gcode` is the shared, validated toolpath. Formatters may not ask for the
    model, the printer profile, or a re-slice: they format, they do not decide.
    """

    gcode: bytes
    filename_base: str
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def gcode_text(self) -> str:
        return self.gcode.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class FormatResult:
    """The bytes of one deliverable file, plus the metadata that describes it."""

    payload: bytes
    format: OutputFormat
    filename: str
    media_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


class OutputFormatter(ABC):
    """Turns one validated toolpath into one deliverable file."""

    #: Stable identifier used by the API and stored on `GeneratedFile.kind`.
    key: str
    format: OutputFormat

    @property
    def extension(self) -> str:
        return self.format.extension

    @property
    def media_type(self) -> str:
        return self.format.media_type

    @abstractmethod
    def availability(self) -> FormatterAvailability:
        """Can this formatter produce a real file in this environment?"""

    @abstractmethod
    def render(self, request: FormatRequest) -> FormatResult:
        """Produce the file. Raises rather than guessing when unavailable."""

    @abstractmethod
    def validate(self, request: FormatRequest, result: FormatResult) -> list[str]:
        """Return blocking problems with a rendered file. Empty means good."""

    def describe(self) -> dict[str, Any]:
        state = self.availability()
        return {
            "key": self.key,
            "format": self.format.value,
            "extension": self.extension,
            "media_type": self.media_type,
            **state.to_dict(),
        }

    # -- shared helpers ---------------------------------------------------

    def filename_for(self, request: FormatRequest) -> str:
        base = (request.filename_base or "print").rsplit(".", 1)[0]
        return f"{base}.{self.extension}"

    def _require_available(self) -> FormatterAvailability:
        state = self.availability()
        if not state.available:
            raise OutputFormatUnavailable(
                f"{self.format.value} output is not available: {state.reason}",
                details={"format": self.format.value, **state.details},
            )
        return state
