"""Generate the regression test models with known ground truth.

Run: python tests/fixtures/build.py

Every fixture is built from an exact dimension so tests can assert against real
expected values instead of snapshotting whatever the analyzer currently emits.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh

HERE = Path(__file__).parent
SIZE = 20.0


def build() -> dict[str, str]:
    written: dict[str, str] = {}

    # 1. Clean 20 mm cube. Volume must be exactly 8000 mm^3.
    cube = trimesh.creation.box(extents=(SIZE, SIZE, SIZE))
    written["cube_20mm.stl"] = _export(cube, "cube_20mm.stl")

    # 2. Hollow box with 0.28 mm walls (matching the spec's example finding).
    #    Outer 20 mm, inner inset by 0.28 mm per side.
    outer = trimesh.creation.box(extents=(SIZE, SIZE, SIZE))
    inner = trimesh.creation.box(
        extents=(SIZE - 2 * 0.28, SIZE - 2 * 0.28, SIZE + 0.5)
    )
    inner.apply_translation([0, 0, -0.25])  # punch through the top
    hollow = outer.difference(inner)
    written["thin_wall.stl"] = _export(hollow, "thin_wall.stl")

    # 3. Non-manifold / open mesh: a cube missing one face.
    open_mesh = trimesh.Trimesh(
        vertices=cube.vertices.copy(), faces=cube.faces[:-1].copy(), process=False
    )
    written["open_mesh.stl"] = _export(open_mesh, "open_mesh.stl")

    # 4. A box rotated 60 degrees: produces a real 60-degree overhang.
    angled = trimesh.creation.box(extents=(30, 10, 10))
    angled.apply_transform(
        trimesh.transformations.rotation_matrix(np.radians(60.0), [0, 1, 0])
    )
    written["overhang_60deg.stl"] = _export(angled, "overhang_60deg.stl")

    # 5. Bridge test: a long thin span between two supports.
    left = trimesh.creation.box(extents=(10, 10, 20)).apply_translation([-25, 0, 0])
    right = trimesh.creation.box(extents=(10, 10, 20)).apply_translation([25, 0, 0])
    span = trimesh.creation.box(extents=(40, 6, 1.2)).apply_translation([0, 0, 10])
    bridge = trimesh.util.concatenate([left, right, span])
    written["bridge.stl"] = _export(bridge, "bridge.stl")

    # 6. Tiny model: 2 mm cube, exercises small-feature handling.
    tiny = trimesh.creation.box(extents=(2, 2, 2))
    written["tiny_2mm.stl"] = _export(tiny, "tiny_2mm.stl")

    # 7. Multi-part assembly: two separated bodies.
    assembly = trimesh.util.concatenate(
        [
            trimesh.creation.box(extents=(20, 20, 10)),
            trimesh.creation.box(extents=(20, 20, 10)).apply_translation([0, 0, 25]),
        ]
    )
    written["assembly.stl"] = _export(assembly, "assembly.stl")

    # 8. Floating crumb alongside a real part.
    part = trimesh.creation.box(extents=(20, 20, 20))
    crumb = trimesh.creation.box(extents=(0.5, 0.5, 0.5)).apply_translation([12, 0, 10])
    with_crumb = trimesh.util.concatenate([part, crumb])
    written["floating_crumb.stl"] = _export(with_crumb, "floating_crumb.stl")

    # 9. Large model: 300 mm, exceeds a 220 mm bed on one axis.
    large = trimesh.creation.box(extents=(300, 100, 50))
    written["large_300mm.stl"] = _export(large, "large_300mm.stl")

    return written


def _export(mesh: trimesh.Trimesh, name: str) -> str:
    path = HERE / name
    mesh.export(path)
    return str(path.relative_to(HERE.parents[1]))


if __name__ == "__main__":
    for name, rel in build().items():
        print(f"wrote {rel}")
