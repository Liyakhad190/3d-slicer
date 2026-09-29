"""Content-sniffing and file-handling security tests.

The rule these enforce: a file is accepted or rejected on its **bytes**, never
on its name, and no caller-supplied string ever reaches a filesystem path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import UnsupportedFormatError, ValidationFailedError  # noqa: E402
from app.core.validation import sanitize_filename, validate_password  # noqa: E402
from app.services.formats import (  # noqa: E402
    ModelFormat,
    assert_safe_archive,
    capability_matrix,
    detect_format,
)

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# format detection
# --------------------------------------------------------------------------- #


def test_detects_binary_stl_by_structure():
    data = (FIXTURES / "cube_20mm.stl").read_bytes()
    assert detect_format(data, filename="part.stl").format is ModelFormat.stl_binary


def test_detects_binary_stl_even_without_the_stl_extension():
    data = (FIXTURES / "cube_20mm.stl").read_bytes()
    assert detect_format(data, filename="part.bin").format is ModelFormat.stl_binary


def test_rejects_a_pe_binary_named_stl():
    """The core anti-spoofing check."""
    payload = b"MZ\x90\x00" + b"\x00" * 5000
    with pytest.raises(UnsupportedFormatError):
        detect_format(payload, filename="totally_a_model.stl")


def test_rejects_a_png_named_obj():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000
    with pytest.raises(UnsupportedFormatError):
        detect_format(png, filename="sneaky.obj")


def test_rejects_random_json_as_gltf():
    with pytest.raises(UnsupportedFormatError):
        detect_format(b'{"hello": "world"}', filename="fake.gltf")


def test_rejects_empty_file():
    with pytest.raises(ValidationFailedError):
        detect_format(b"", filename="empty.stl")


def test_rejects_a_zip_that_is_not_a_model():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "not a model")
    with pytest.raises(UnsupportedFormatError):
        detect_format(buffer.getvalue(), filename="archive.3mf")


def test_detects_ascii_stl():
    text = b"solid test\n  facet normal 0 0 1\n    outer loop\n      vertex 0 0 0\n"
    assert detect_format(text, filename="a.stl").format is ModelFormat.stl_ascii


def test_detects_obj():
    obj = b"# comment\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
    assert detect_format(obj, filename="a.obj").format is ModelFormat.obj


def test_detects_ply():
    ply = b"ply\nformat ascii 1.0\nelement vertex 3\nend_header\n"
    assert detect_format(ply, filename="a.ply").format is ModelFormat.ply


def test_detects_glb_magic():
    glb = b"glTF" + b"\x02\x00\x00\x00" + b"\x00" * 200
    assert detect_format(glb, filename="a.glb").format is ModelFormat.glb


def test_detects_step_header():
    step = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
    assert detect_format(step, filename="a.step").format is ModelFormat.step


def test_capability_matrix_does_not_overstate_writers():
    matrix = capability_matrix()
    by_format = {row["format"]: row for row in matrix}

    # Only formats with a real, validated writer claim `write`.
    assert by_format["stl_binary"]["write"] is True
    assert by_format["three_mf"]["write"] is True
    assert by_format["stl_ascii"]["write"] is False
    assert by_format["obj"]["write"] is False

    # STEP is accepted as input but explicitly not implemented as a converter.
    assert by_format["step"]["notes"]
    assert "NOT" not in str(by_format["step"]["read"]).upper()


# --------------------------------------------------------------------------- #
# zip bomb defence
# --------------------------------------------------------------------------- #


def test_rejects_zip_with_traversing_member_path():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../../../etc/passwd", "pwned")
    with pytest.raises(ValidationFailedError):
        assert_safe_archive(buffer.getvalue(), max_ratio=120, max_entries=1000)


def test_rejects_implausible_compression_ratio():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", "0" * 4_000_000)
    with pytest.raises(ValidationFailedError):
        assert_safe_archive(buffer.getvalue(), max_ratio=10, max_entries=1000)


def test_accepts_a_modest_archive():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", "0" * 2000)
    assert_safe_archive(buffer.getvalue(), max_ratio=200, max_entries=1000)


# --------------------------------------------------------------------------- #
# filename hygiene
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
        ("/etc/shadow", "shadow"),
        ("C:\\Users\\Bob\\part.stl", "part.stl"),
        ("normal_name.stl", "normal_name.stl"),
        ("", "model"),
        ("....", "model"),
        ("a b c.stl", "a-b-c.stl"),
        ("CON.stl", "_CON.stl"),
        ("nul", "_nul"),
    ],
)
def test_sanitize_filename(raw: str, expected: str):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_strips_control_characters():
    result = sanitize_filename("part\x00\x1b[31m.stl")
    assert "\x00" not in result
    assert "\x1b" not in result


def test_sanitize_filename_bounds_length():
    result = sanitize_filename("a" * 1000 + ".stl")
    assert len(result) <= 255
    assert result.endswith(".stl")


def test_sanitize_filename_handles_unicode():
    assert sanitize_filename("проба.stl") == "stl"
    assert sanitize_filename("模型.stl") == "stl"


# --------------------------------------------------------------------------- #
# password policy
# --------------------------------------------------------------------------- #


def test_rejects_short_password():
    with pytest.raises(ValidationFailedError):
        validate_password("short")


def test_rejects_common_password_even_when_long_enough():
    with pytest.raises(ValidationFailedError):
        validate_password("password123")


def test_rejects_surrounding_whitespace():
    with pytest.raises(ValidationFailedError):
        validate_password("  correcthorsebattery1")


def test_accepts_a_long_passphrase():
    validate_password("correct-horse-battery-staple")
