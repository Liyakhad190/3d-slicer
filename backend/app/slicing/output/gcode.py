"""Plain G-code output — the default, always-available deliverable.

This is deliberately the thin formatter: the pipeline already sliced, already
ran :func:`app.slicing.validator.validate_gcode` against the machine limits, and
already stored the bytes. Re-validating here costs little and catches a file
that was truncated or corrupted between slicing and download.
"""

from __future__ import annotations

from app.slicing.output.base import (
    FormatRequest,
    FormatResult,
    FormatterAvailability,
    OutputFormat,
    OutputFormatter,
)


class GCodeFormatter(OutputFormatter):
    key = "gcode"
    format = OutputFormat.gcode

    def availability(self) -> FormatterAvailability:
        return FormatterAvailability(available=True, reason="G-code is always available.")

    def render(self, request: FormatRequest) -> FormatResult:
        self._require_available()
        if not request.gcode:
            from app.slicing.output.base import OutputFormatRejected

            raise OutputFormatRejected(
                "There is no toolpath to write: the sliced file is empty.",
                details={"format": self.format.value},
            )
        return FormatResult(
            payload=request.gcode,
            format=self.format,
            filename=self.filename_for(request),
            media_type=self.media_type,
            # Nothing added to the toolpath — this formatter only labels it.
            metadata={"source": "sliced-gcode"},
        )

    def validate(self, request: FormatRequest, result: FormatResult) -> list[str]:
        problems: list[str] = []
        if not result.payload:
            problems.append("The file is empty.")
            return problems

        try:
            text = result.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            problems.append(f"G-code must be UTF-8 text: {exc}")
            return problems

        if "\x00" in text:
            problems.append("Binary data found in a file that must be plain G-code.")

        # Re-run the real validator when the caller supplied machine limits;
        # the pipeline does this on the freshly sliced file, and the download
        # path should never hand over something that would fail it again.
        limits = request.context.get("limits")
        if limits is not None:
            from app.slicing.validator import validate_gcode

            report = validate_gcode(text, limits, source_name=result.filename)
            if not report.passed:
                problems.append(report.summary or "G-code validation failed.")

        return problems
