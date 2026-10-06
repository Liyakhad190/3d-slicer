"""Output formatting: capability gating, formatters, and GX validation.

These are the guarantees the output layer exists to make:

* a printer only ever sees formats its profile declares;
* a declared format that cannot be produced says so instead of degrading;
* `.gx` is an X3G container, and anything that is just the G-code again is
  rejected before it can be stored;
* the pipeline never renames a file into a format it did not produce.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import AppError  # noqa: E402
from app.slicing.output import (  # noqa: E402
    GCodeFormatter,
    GXFormatter,
    OutputFormat,
    OutputFormatRejected,
    OutputFormatUnavailable,
    OutputFormatUnsupported,
    FormatRequest,
    declared_output_formats,
    get_output_registry,
    reset_output_registry,
    validate_gx,
)
from app.slicing.output.gx import RecordingEncoder  # noqa: E402
from app.slicing.output.registry import DEFAULT_OUTPUT_FORMATS  # noqa: E402


GCODE = b"; FLAVOR:Marlin\nG28\nG1 X10 Y10 F3000\nM104 S200\nM104 S0\n"


class _Profile:
    """Stand-in for MachineProfile: the registry only reads restrictions."""

    def __init__(self, output_formats=None):
        restrictions = {}
        if output_formats is not None:
            restrictions["output_formats"] = output_formats
        self.machine_restrictions = restrictions


# --------------------------------------------------------------------------- #
# capability declaration
# --------------------------------------------------------------------------- #


def test_profile_without_declaration_gets_gcode_only():
    assert declared_output_formats(_Profile()) == ("gcode",)
    assert declared_output_formats(_Profile(None)) == DEFAULT_OUTPUT_FORMATS


def test_profile_declaration_is_read_verbatim():
    assert declared_output_formats(_Profile(["gcode", "gx"])) == ("gcode", "gx")


def test_profile_may_declare_a_single_string():
    assert declared_output_formats(_Profile("gcode")) == ("gcode",)


def test_empty_or_blank_declaration_falls_back_to_gcode():
    assert declared_output_formats(_Profile([])) == ("gcode",)
    assert declared_output_formats(_Profile(["  "])) == ("gcode",)


def test_registry_ships_gcode_and_gx():
    registry = get_output_registry()
    assert {"gcode", "gx"} <= {f.key for f in registry.all()}


def test_undeclared_format_is_refused_with_a_409():
    registry = get_output_registry()
    with pytest.raises(OutputFormatUnsupported) as exc:
        registry.require_supported(_Profile(["gcode"]), "gx")
    assert exc.value.http_status == 409
    assert exc.value.details["declared_by_profile"] == ["gcode"]


def test_unknown_format_names_what_is_known():
    registry = get_output_registry()
    with pytest.raises(OutputFormatUnsupported) as exc:
        registry.require_supported(_Profile(["gcode", "gx"]), "dxf")
    assert "gcode" in exc.value.message


def test_declared_but_unproducible_is_reported_not_hidden():
    registry = get_output_registry()
    # No G-code-to-X3G encoder is installed in this environment, so a profile
    # that *does* declare gx must still be able to see it, with the reason.
    rows = {r["key"]: r for r in registry.supported_for(_Profile(["gcode", "gx"]))}
    assert rows["gx"]["declared_by_profile"] is True
    assert rows["gx"]["advertised"] is False
    assert rows["gx"]["reason"]
    assert rows["gcode"]["advertised"] is True


def test_only_declared_formats_are_advertised():
    registry = get_output_registry()
    assert registry.advertised_formats(_Profile(["gcode"])) == ["gcode"]
    assert registry.advertised_formats(_Profile(None)) == ["gcode"]


def test_gx_availability_explains_the_missing_dependency():
    state = GXFormatter().availability()
    assert state.available is False
    assert "GPX" in state.reason or "encoder" in state.reason
    assert state.details["searched"]


# --------------------------------------------------------------------------- #
# formatters
# --------------------------------------------------------------------------- #


def test_gcode_formatter_labels_without_rewriting():
    formatter = GCodeFormatter()
    result = formatter.render(FormatRequest(gcode=GCODE, filename_base="part.stl"))
    assert result.payload is GCODE
    assert result.filename == "part.gcode"
    assert result.format is OutputFormat.gcode


def test_gcode_formatter_refuses_an_empty_toolpath():
    formatter = GCodeFormatter()
    with pytest.raises(OutputFormatRejected):
        formatter.render(FormatRequest(gcode=b"", filename_base="part.stl"))


def test_gcode_formatter_accepts_valid_text_and_flags_binary():
    formatter = GCodeFormatter()
    good = FormatRequest(gcode=GCODE, filename_base="part.gcode")
    assert formatter.validate(good, formatter.render(good)) == []

    binary = FormatRequest(gcode=b"G1\x00X", filename_base="part.gcode")
    rendered = formatter.render(binary)
    assert any("Binary" in p for p in formatter.validate(binary, rendered))


def test_gx_formatter_is_unavailable_without_an_encoder():
    formatter = GXFormatter()
    with pytest.raises(OutputFormatUnavailable) as exc:
        formatter.render(FormatRequest(gcode=GCODE, filename_base="part.stl"))
    assert exc.value.http_status == 503
    assert exc.value.details["format"] == "gx"


# --------------------------------------------------------------------------- #
# GX validation — what makes a real .gx
# --------------------------------------------------------------------------- #


def _container() -> bytes:
    return RecordingEncoder().encode(GCODE, context={})


def test_validate_gx_accepts_a_real_container():
    assert validate_gx(_container(), source_gcode=GCODE) == []


def test_validate_gx_rejects_empty_and_tiny_payloads():
    assert validate_gx(b"")
    assert validate_gx(b"\x99FlashPrint")


def test_validate_gx_rejects_missing_header():
    payload = b"\x99" + b"\x00" * 200
    assert any("header" in p for p in validate_gx(payload, source_gcode=GCODE))


def test_validate_gx_rejects_plain_gcode_text():
    problems = validate_gx(GCODE + b" " * 200, source_gcode=GCODE)
    assert any("G-code text" in p or "byte-identical" in p for p in problems)


def test_validate_gx_rejects_a_renamed_gcode_file():
    """The failure mode the pipeline used to ship: same bytes, new extension."""
    problems = validate_gx(GCODE, source_gcode=GCODE)
    assert any("byte-identical" in p for p in problems)


def test_gx_formatter_renders_a_real_file_when_an_encoder_exists():
    from app.slicing.output import gx as gx_module

    original = gx_module._encoder_registry
    try:
        probe = gx_module.GXEncoderRegistry()
        probe.register(RecordingEncoder())
        gx_module._encoder_registry = probe

        formatter = GXFormatter()
        request = FormatRequest(gcode=GCODE, filename_base="bracket.stl")
        assert formatter.availability().available is True

        result = formatter.render(request)
        assert result.filename == "bracket.gx"
        assert result.format is OutputFormat.gx
        assert result.payload != GCODE
        assert result.payload[5:15] == b"FlashPrint"
        assert formatter.validate(request, result) == []
        assert result.metadata["encoder"] == "recording"
        assert get_output_registry().get("gx") is not None
    finally:
        gx_module._encoder_registry = original


def test_gx_formatter_discards_output_that_fails_validation():
    from app.slicing.output import gx as gx_module

    class LyingEncoder:
        key = "lying"

        def availability(self):
            return gx_module.FormatterAvailability(available=True, reason="test")

        def encode(self, gcode, *, context):
            return gcode  # a renamed file, not a conversion

        def describe(self):
            return {"key": self.key}

    original = gx_module._encoder_registry
    try:
        probe = gx_module.GXEncoderRegistry()
        probe.register(LyingEncoder())
        gx_module._encoder_registry = probe
        with pytest.raises(OutputFormatRejected) as exc:
            GXFormatter().render(FormatRequest(gcode=GCODE, filename_base="part.stl"))
        assert any("byte-identical" in p for p in exc.value.details["problems"])
    finally:
        gx_module._encoder_registry = original


def test_errors_are_typed_for_the_api():
    for cls, code in (
        (OutputFormatUnsupported, "OUTPUT_FORMAT_UNSUPPORTED"),
        (OutputFormatUnavailable, "OUTPUT_FORMAT_UNAVAILABLE"),
        (OutputFormatRejected, "OUTPUT_FORMAT_REJECTED"),
    ):
        assert issubclass(cls, AppError)
        assert cls.code == code


# --------------------------------------------------------------------------- #
# the pipeline must name only what it produced
# --------------------------------------------------------------------------- #


def test_pipeline_never_renames_gcode_into_a_proprietary_format():
    from app.slicing.pipeline import _output_extension

    # FlashForge used to be special-cased to relabel the file `.gx`.
    assert _output_extension("FlashForge", "fdm", None) == ("gcode", "text/x-gcode")
    assert _output_extension("flashforge", "fdm", "marlin") == ("gcode", "text/x-gcode")
    # Firmware hints that describe the bytes still apply.
    assert _output_extension(None, "fdm", "bgcode")[0] == "bgcode"
    assert _output_extension(None, "fdm", None) == ("gcode", "text/x-gcode")


def test_registry_reset_rebuilds_defaults():
    reset_output_registry()
    try:
        registry = get_output_registry()
        assert {f.key for f in registry.all()} == {"gcode", "gx"}
    finally:
        reset_output_registry()
