"""Output formatting: one toolpath, many deliverable files.

    from app.slicing.output import get_output_registry, generate_output
"""

from app.slicing.output.artifacts import OutputArtifact, describe_artifact, describe_generated_file, sha256_of
from app.slicing.output.base import (
    FormatRequest,
    FormatResult,
    FormatterAvailability,
    OutputFormat,
    OutputFormatRejected,
    OutputFormatUnavailable,
    OutputFormatUnsupported,
    OutputFormatter,
)
from app.slicing.output.gcode import GCodeFormatter
from app.slicing.output.gx import (
    GXEncoder,
    GXEncoderRegistry,
    GXFormatter,
    GpxEncoder,
    get_encoder_registry,
    validate_gx,
)
from app.slicing.output.registry import (
    DEFAULT_OUTPUT_FORMATS,
    OutputFormatterRegistry,
    declared_output_formats,
    get_output_registry,
    reset_output_registry,
)

__all__ = [
    "DEFAULT_OUTPUT_FORMATS",
    "FormatRequest",
    "FormatResult",
    "FormatterAvailability",
    "GCodeFormatter",
    "GXEncoder",
    "GXEncoderRegistry",
    "GXFormatter",
    "GpxEncoder",
    "OutputArtifact",
    "OutputFormat",
    "OutputFormatRejected",
    "OutputFormatUnavailable",
    "OutputFormatUnsupported",
    "OutputFormatter",
    "OutputFormatterRegistry",
    "declared_output_formats",
    "describe_artifact",
    "describe_generated_file",
    "get_encoder_registry",
    "get_output_registry",
    "reset_output_registry",
    "sha256_of",
    "validate_gx",
]
