"""Model preparation: place, orient and normalise a mesh for a build volume.

Slicers are not consistent about where a mesh ends up. CuraEngine, for example,
does not lift a model onto the build plate: a mesh authored centred on the
origin spans Z -10..+10 and everything below the plate is silently clipped
away, so a 20 mm cube produced a 10 mm print with no warning anywhere.

The platform therefore does the placement itself and hands the slicer a mesh
whose position is already correct. This is the `prepareConfiguration` step of
the adapter contract, implemented once, deterministically, for every engine.

Two invariants:

* The user's original blob is never modified. A prepared copy is written to the
  job workspace and is a separate artifact.
* Placement is a pure function of (mesh, build volume). The same inputs always
  produce byte-identical output, so a re-run is reproducible.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from app.core.errors import ValidationFailedError
from app.core.logging import get_logger

log = get_logger(__name__)

PLACEMENT_VERSION = "mesh-placement-1.0.0"


@dataclass(frozen=True)
class PlacementResult:
    """What was done to the mesh, and whether it fits."""

    placed_path: Path
    original_extents: tuple[float, float, float]
    placed_extents: tuple[float, float, float]
    offset: tuple[float, float, float]
    scale_factor: float
    fits: bool
    overshoot: dict[str, float]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "placement_version": PLACEMENT_VERSION,
            "original_extents_mm": [round(v, 4) for v in self.original_extents],
            "placed_extents_mm": [round(v, 4) for v in self.placed_extents],
            "offset_mm": [round(v, 4) for v in self.offset],
            "scale_factor": self.scale_factor,
            "fits_build_volume": self.fits,
            "overshoot_mm": {k: round(v, 4) for k, v in self.overshoot.items()},
            "notes": self.notes,
        }


def prepare_mesh(
    mesh: trimesh.Trimesh,
    *,
    build_volume: tuple[float, float, float],
    output_path: Path,
    allow_scale: bool = False,
    file_type: str = "stl",
) -> PlacementResult:
    """Place a mesh on the build plate and write a prepared copy.

    Placement rules:

    * The bottom of the part sits exactly on the plate (min Z = 0).
    * The part is centred on the build **origin** in X and Y, not on the bed.

    The second point is the subtle one. Slicers apply their own centring: Cura
    Engine shifts the model so it sits in the middle of the plate, so a mesh
    already placed at bed centre is pushed a second time and can end up off the
    edge of the bed. Emitting the canonical pose — centred on the origin,
    sitting on the plate — and letting each engine map the origin onto its own
    coordinate frame is the only convention that works across engines.

    Two invariants:

    * The user's original blob is never modified. A prepared copy is written to
      the job workspace and is a separate artifact.
    * Placement is a pure function of (mesh, build volume). The same inputs
      always produce byte-identical output, so a re-run is reproducible.
    """
    notes: list[str] = []
    build_x, build_y, build_z = build_volume

    placed = mesh.copy()
    extents = tuple(float(v) for v in placed.extents)

    # Centre on the build origin in X/Y and sit on the plate in Z. Slicers
    # apply their own centring, so pre-centring on the bed would double it.
    centre = (placed.bounds[0] + placed.bounds[1]) / 2.0
    translation = np.array(
        [
            -float(centre[0]),
            -float(centre[1]),
            -float(placed.bounds[0][2]),
        ]
    )
    placed.apply_translation(translation)

    scale_factor = 1.0
    overshoot: dict[str, float] = {}
    new_extents = tuple(float(v) for v in placed.extents)

    for axis, (value, limit) in enumerate(
        zip(new_extents, (build_x, build_y, build_z), strict=False)
    ):
        if limit and value > limit:
            overshoot["xyz"[axis]] = value - limit

    if overshoot:
        largest = max(overshoot.values())
        if allow_scale:
            scale_factor = scale_to_fit / (1.0 + largest / min(
                limit for limit in (build_x, build_y, build_z) if limit
            ))
            placed.apply_scale(scale_factor)
            # Re-seat after scaling so the bottom still touches the plate.
            placed.apply_translation([0, 0, -float(placed.bounds[0][2])])
            new_extents = tuple(float(v) for v in placed.extents)
            notes.append(
                f"Scaled by {scale_factor:.4f} to fit the build volume. The part is now "
                f"{new_extents[0]:.1f} x {new_extents[1]:.1f} x {new_extents[2]:.1f} mm."
            )
        else:
            raise ValidationFailedError(
                "The model does not fit in this machine's build volume at this "
                f"orientation. It needs {largest:.1f} mm more on the "
                f"{max(overshoot, key=overshoot.get)} axis.",
                details={
                    "build_volume_mm": {"x": build_x, "y": build_y, "z": build_z},
                    "model_extents_mm": {
                        "x": round(new_extents[0], 3),
                        "y": round(new_extents[1], 3),
                        "z": round(new_extents[2], 3),
                    },
                    "overshoot_mm": {k: round(v, 3) for k, v in overshoot.items()},
                    "remedy": "Re-orient the model, scale it in CAD, or choose a larger machine.",
                },
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Export with an explicit type: content-addressed inputs arrive without an
    # extension, and trimesh's exporter selection is driven by the extension.
    placed.export(output_path, file_type=file_type)

    notes.append(
        "Prepared pose: centred on the build origin in X and Y, bottom face on "
        "the plate at Z=0. The slicer maps the origin onto its own coordinate "
        "frame. The original file is unchanged."
    )

    result = PlacementResult(
        placed_path=output_path,
        original_extents=extents,
        placed_extents=new_extents,
        offset=tuple(float(v) for v in translation),
        scale_factor=scale_factor,
        fits=not overshoot or scale_factor != 1.0,
        overshoot=overshoot,
        notes=notes,
    )
    log.info(
        "mesh.placed",
        original=[round(v, 3) for v in extents],
        placed=[round(v, 3) for v in new_extents],
        offset=[round(v, 3) for v in translation],
        scale=scale_factor,
        fits=result.fits,
    )
    return result


def prepare_file(
    source: Path,
    *,
    build_volume: tuple[float, float, float],
    output_path: Path,
    allow_scale: bool = False,
    file_type: str | None = None,
) -> PlacementResult:
    """Load, place and write a prepared copy of a model file.

    `file_type` must be supplied for stored blobs. Blobs are content-addressed
    and therefore have no extension, so trimesh cannot infer the format from
    the path and raises "File type: ... not supported" without it.
    """
    if file_type is None:
        file_type = source.suffix.lstrip(".").lower() or None
    mesh = trimesh.load(str(source), force="mesh", process=False, file_type=file_type)
    if isinstance(mesh, trimesh.Scene):
        meshes = [g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not meshes:
            raise ValidationFailedError("The model contains no triangle geometry.")
        mesh = trimesh.util.concatenate(meshes)
    mesh.merge_vertices()
    mesh.remove_unreferenced_vertices()
    return prepare_mesh(
        mesh,
        build_volume=build_volume,
        output_path=output_path,
        allow_scale=allow_scale,
        file_type=file_type or "stl",
    )


def placement_fingerprint(result: PlacementResult) -> str:
    """Stable hash of a placement, for the job's input fingerprint."""
    payload = result.to_dict()
    payload.pop("notes", None)
    import json

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
