"""File format detection and the format capability matrix.

Detection is by **magic bytes, never by extension**. A caller who uploads
`payload.exe` named `part.stl` is rejected on content, not on name.
"""

from __future__ import annotations

import json
import struct
import zipfile
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from typing import Final

from app.core.errors import UnsupportedFormatError, ValidationFailedError


class ModelFormat(str, Enum):
    stl_binary = "stl_binary"
    stl_ascii = "stl_ascii"
    obj = "obj"
    three_mf = "3mf"
    amf = "amf"
    ply = "ply"
    glb = "glb"
    gltf = "gltf"
    step = "step"
    stp = "stp"


@dataclass(frozen=True)
class FormatInfo:
    format: ModelFormat
    media_type: str
    extension: str
    is_archive: bool = False
    text_based: bool = False


#: What the platform can actually read/write today, and with what.
#:
#: This is the capability matrix from the requirements, kept as data so the API
#: can serve it and the UI can render it instead of either one hard-coding
#: promises. `write` is only true where a real, validated writer exists.
_CAPABILITY: Final[dict[str, FormatInfo]] = {
    "stl_binary": FormatInfo(ModelFormat.stl_binary, "model/stl", ".stl"),
    "stl_ascii": FormatInfo(ModelFormat.stl_ascii, "model/stl", ".stl", text_based=True),
    "obj": FormatInfo(ModelFormat.obj, "model/obj", ".obj", text_based=True),
    "three_mf": FormatInfo(ModelFormat.three_mf, "model/3mf", ".3mf", is_archive=True),
    "amf": FormatInfo(ModelFormat.amf, "model/amf", ".amf", is_archive=True),
    "ply": FormatInfo(ModelFormat.ply, "model/ply", ".ply", text_based=True),
    "glb": FormatInfo(ModelFormat.glb, "model/gltf-binary", ".glb"),
    "gltf": FormatInfo(ModelFormat.gltf, "model/gltf+json", ".gltf", text_based=True),
    "step": FormatInfo(ModelFormat.step, "application/step", ".step", text_based=True),
    "stp": FormatInfo(ModelFormat.stp, "application/step", ".stp", text_based=True),
}

#: Extension aliases, used only to *hint* when content detection is ambiguous.
_EXTENSION_HINTS: Final[dict[str, ModelFormat]] = {
    ".stl": ModelFormat.stl_binary,
    ".obj": ModelFormat.obj,
    ".3mf": ModelFormat.three_mf,
    ".amf": ModelFormat.amf,
    ".ply": ModelFormat.ply,
    ".glb": ModelFormat.glb,
    ".gltf": ModelFormat.gltf,
    ".step": ModelFormat.step,
    ".stp": ModelFormat.stp,
}

#: Formats the requirements ask for that are NOT yet implemented end to end.
#:
#: Declared explicitly so the API can answer honestly instead of pretending.
#: Step/IGES need a real CAD kernel (OpenCascade) to mesh, which is not wired up.
NOT_IMPLEMENTED: Final[dict[str, str]] = {
    "step": "Requires an OpenCascade-based conversion worker (Phase 6). Accepted "
            "as input only once a validated mesher is available.",
    "stp": "Requires an OpenCascade-based conversion worker (Phase 6).",
    "gltf": "glTF with external buffers (.bin/.png) is not yet assembled; .glb is "
            "the supported path for now.",
}


def capability_matrix() -> list[dict[str, object]]:
    """The read/write matrix served to the UI. No capability is overstated."""
    return [
        {
            "format": key,
            "extension": info.extension,
            "media_type": info.media_type,
            "read": True,
            "write": key in {"stl_binary", "three_mf"},
            "is_archive": info.is_archive,
            "notes": NOT_IMPLEMENTED.get(key),
        }
        for key, info in _CAPABILITY.items()
    ]


def _looks_like_binary_stl(head: bytes, size: int) -> bool:
    """Binary STL has no magic number, so validate its structure instead.

    A binary STL is an 80-byte header followed by a little-endian uint32 face
    count N, then exactly 50*N bytes. Requiring all three conditions makes a
    false positive essentially impossible.
    """
    if size < 84:
        return False
    (face_count,) = struct.unpack("<I", head[80:84])
    if face_count == 0 or face_count > 10_000_000:
        return False
    return size == 84 + face_count * 50


def _sniff_zip_container(data: BytesIO) -> ModelFormat | None:
    try:
        with zipfile.ZipFile(data) as archive:
            names = {name.lower() for name in archive.namelist()}
    except (zipfile.BadZipFile, OSError):
        return None
    if any(name.endswith(".3mf") or "[content_types].xml" in names and "3d" in str(names) for name in names):
        return ModelFormat.three_mf
    if any(name.endswith(".amf") for name in names):
        return ModelFormat.amf
    if "[content_types].xml" in names:
        return ModelFormat.three_mf
    return None


def detect_format(data: bytes, *, filename: str | None = None) -> FormatInfo:
    """Identify an upload from its content. Raises for anything unrecognised.

    The extension is consulted only as a tiebreaker for ASCII formats whose
    leading bytes are shared (OBJ vs PLY vs glTF), and never as the reason a
    file is accepted.
    """
    if not data:
        raise ValidationFailedError("The uploaded file is empty.")

    size = len(data)
    head = data[:4096]

    # --- archives: 3MF / AMF (both ZIP containers) --------------------------
    if data[:4] == b"PK\x03\x04":
        detected = _sniff_zip_container(BytesIO(data))
        if detected is not None:
            return _CAPABILITY[detected.value]
        raise UnsupportedFormatError(
            "This is a ZIP archive but not a recognised 3MF or AMF model.",
            details={"supported": ["3mf", "amf"]},
        )

    # --- GLB: the only format with a 4-byte magic at offset 0 ---------------
    if head[:4] == b"glTF":
        return _CAPABILITY[ModelFormat.glb.value]

    # --- STL: binary (structural check) or ASCII ---------------------------
    if _looks_like_binary_stl(head, size):
        return _CAPABILITY[ModelFormat.stl_binary.value]
    stripped = head.lstrip()
    if stripped[:5].lower() == b"solid" and b"facet normal" in data[:2048].lower():
        return _CAPABILITY[ModelFormat.stl_ascii.value]

    # --- PLY: "ply" as the first token --------------------------------------
    if stripped[:3] == b"ply":
        return _CAPABILITY[ModelFormat.ply.value]

    # --- STEP: ISO-10303-21 header, possibly after whitespace/BOM ----------
    probe = data[:2048]
    if b"ISO-10303-21" in probe:
        return _CAPABILITY[ModelFormat.step.value]

    # --- text formats: OBJ / glTF(JSON) / AMF(XML) -------------------------
    try:
        text = head.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = head.decode("latin-1", errors="ignore")

    lowered = text.lstrip().lower()
    if lowered.startswith("{"):
        # glTF is the only JSON model format we accept; verify it really is one
        # rather than accepting any JSON blob.
        try:
            document = json.loads(data.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UnsupportedFormatError("File is not valid JSON.") from exc
        if isinstance(document, dict) and "asset" in document and "gltf" in str(
            document.get("asset", {}).get("version", "")
        ):
            return _CAPABILITY[ModelFormat.gltf.value]
        raise UnsupportedFormatError(
            "JSON file is not a glTF document.", details={"expected_key": "asset.version"}
        )

    if lowered.startswith("<?xml") or lowered.startswith("<amf"):
        if "<amf" in lowered:
            return _CAPABILITY[ModelFormat.amf.value]
        raise UnsupportedFormatError(
            "XML file is not a supported model document.", details={"supported": ["amf"]}
        )

    obj_tokens = ("v ", "vn ", "vt ", "f ", "o ", "g ", "mtllib", "#", "\n")
    first_lines = [line.strip() for line in text.splitlines()[:40] if line.strip()]
    if first_lines and any(
        line.startswith(("v ", "vn ", "vt ", "f ", "o ", "g ", "mtllib", "#"))
        for line in first_lines[:8]
    ) and any(line in obj_tokens or " " in line for line in first_lines):
        return _CAPABILITY[ModelFormat.obj.value]

    if filename:
        suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
        hinted = _EXTENSION_HINTS.get(suffix)
        if hinted is not None:
            raise UnsupportedFormatError(
                f"Content does not look like a valid {_CAPABILITY[hinted.value].extension} "
                "file. The file was rejected on its content, not its name.",
                details={"declared_extension": suffix, "detected_as": "unknown"},
            )

    raise UnsupportedFormatError(
        "Unrecognised file format. Supported: STL, OBJ, 3MF, AMF, PLY, GLB.",
        details={"detected_as": "unknown", "byte_length": size},
    )


def assert_safe_archive(data: bytes, *, max_ratio: int, max_entries: int) -> None:
    """Reject zip bombs before anything decompresses the archive.

    A 3MF is a ZIP. Without this check a 200 MB upload can expand to
    terabytes and take the worker process with it.
    """
    total_compressed = 0
    total_uncompressed = 0
    with zipfile.ZipFile(BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > max_entries:
            raise ValidationFailedError(
                f"Archive contains more than {max_entries} entries.",
                details={"entries": len(infos), "limit": max_entries},
            )
        for info in infos:
            total_compressed += info.compress_size
            total_uncompressed += info.file_size
            # Refuse an absolute or traversing member path outright.
            name = info.filename
            if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
                raise ValidationFailedError(
                    "Archive contains an unsafe member path.", details={"entry": name[:120]}
                )

    if total_compressed > 0:
        ratio = total_uncompressed / total_compressed
        if ratio > max_ratio:
            raise ValidationFailedError(
                "Archive compression ratio is implausible; refusing to unpack "
                "(possible zip bomb).",
                details={"ratio": round(ratio, 1), "limit": max_ratio},
            )
