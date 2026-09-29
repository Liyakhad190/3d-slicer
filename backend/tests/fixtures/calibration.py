"""Calibration models for print-fidelity verification.

Each model exists to make one property measurable, and each carries its own
ground truth in `GROUND_TRUTH` so a test asserts against a real number rather
than against whatever the pipeline currently produces.

Regenerate with: python tests/fixtures/calibration.py
"""

from __future__ import annotations

import math
from pathlib import Path

import trimesh

HERE = Path(__file__).parent


def _box(size: float) -> trimesh.Trimesh:
    return trimesh.creation.box(extents=(size, size, size))


def _hollow_box(outer: float, wall: float, height: float) -> trimesh.Trimesh:
    """A box with a genuine wall thickness, open at the top so it meshes cleanly."""
    shell = trimesh.creation.box(extents=(outer, outer, height))
    cavity = trimesh.creation.box(
        extents=(outer - 2 * wall, outer - 2 * wall, height + 2.0)
    )
    cavity.apply_translation([0, 0, -1.0])
    return shell.difference(cavity)


def build_calibration_models() -> dict[str, trimesh.Trimesh]:
    """The models the fidelity suite measures."""
    models: dict[str, trimesh.Trimesh] = {}

    # 1. Plain cube: the baseline for layer count, extent and volume.
    models["cal_cube_20mm"] = _box(20.0)

    # 2. Hollow box, 1.2 mm wall: makes wall count directly observable.
    models["cal_hollow_1p2mm_wall"] = _hollow_box(20.0, 1.2, 20.0)

    # 3. Hollow box, 0.9 mm wall: two walls at a 0.45 mm line width.
    models["cal_hollow_0p9mm_wall"] = _hollow_box(20.0, 0.9, 20.0)

    # 4. Thin-wall block, 0.6 mm: cannot be two lines at a 0.4 mm nozzle.
    models["cal_thin_wall_0p6mm"] = _hollow_box(20.0, 0.6, 20.0)

    # 5. Flat plate: few layers, so the top/bottom shell dominates.
    models["cal_plate_40x2mm"] = trimesh.creation.box(extents=(40.0, 40.0, 2.0))

    # 6. Cylinder: a curved surface, so spiralise and wall count are visible.
    models["cal_cylinder_r10_h20"] = trimesh.creation.cylinder(
        radius=10.0, height=20.0, sections=64
    )

    # 7. Overhang block: a 60-degree shelf, for support decisions.
    overhang = trimesh.creation.box(extents=(30.0, 10.0, 10.0))
    overhang.apply_transform(
        trimesh.transformations.rotation_matrix(math.radians(60.0), [0, 1, 0])
    )
    models["cal_overhang_60deg"] = overhang

    # 8. Tall thin post: build-volume fit and bed stability.
    models["cal_tall_post_5x5x150"] = trimesh.creation.box(extents=(5.0, 5.0, 150.0))

    # 9. Dimensional coupon: three discs of different heights, so each Z extent
    #    can be checked independently.
    disc_a = trimesh.creation.cylinder(radius=8.0, height=2.0, sections=64).apply_translation(
        [0, 0, 1.0]
    )
    disc_b = trimesh.creation.cylinder(radius=8.0, height=5.0, sections=64).apply_translation(
        [0, 0, 4.5]
    )
    disc_c = trimesh.creation.cylinder(radius=8.0, height=3.0, sections=64).apply_translation(
        [0, 0, 8.5]
    )
    models["cal_dimensional_coupon"] = trimesh.util.concatenate([disc_a, disc_b, disc_c])

    return models


#: Ground truth per model, for assertions and for the fidelity report.
#:
#: `size_mm` is the XY extent of the part, excluding any brim or skirt.
#: `solid_volume_mm3` is the volume a 100% solid print would lay down.
GROUND_TRUTH: dict[str, dict[str, float]] = {
    "cal_cube_20mm": {
        "size_mm": 20.0,
        "solid_volume_mm3": 20.0**3,
        "min_feature_mm": 20.0,
    },
    "cal_hollow_1p2mm_wall": {
        "size_mm": 20.0,
        "solid_volume_mm3": 20.0**3 - 17.6**3,
        "min_feature_mm": 1.2,
    },
    "cal_hollow_0p9mm_wall": {
        "size_mm": 20.0,
        "solid_volume_mm3": 20.0**3 - 18.2**3,
        "min_feature_mm": 0.9,
    },
    "cal_thin_wall_0p6mm": {
        "size_mm": 20.0,
        "solid_volume_mm3": 20.0**3 - 18.8**3,
        "min_feature_mm": 0.6,
    },
    "cal_plate_40x2mm": {
        "size_mm": 40.0,
        "solid_volume_mm3": 40.0 * 40.0 * 2.0,
        "min_feature_mm": 2.0,
    },
    "cal_cylinder_r10_h20": {
        "size_mm": 20.0,
        "solid_volume_mm3": math.pi * 10.0**2 * 20.0,
        "min_feature_mm": 20.0,
    },
    "cal_overhang_60deg": {
        # Measured, not assumed: a 30x10x10 box rotated 60 deg about Y has an
        # X extent of 30*cos60 + 10*sin60 = 23.66 mm. Recording the pre-rotation
        # 30 mm here made a correct toolpath look like a fidelity failure.
        "size_mm": 23.6603,
        "solid_volume_mm3": 3000.0,
        "min_feature_mm": 10.0,
    },
    "cal_tall_post_5x5x150": {
        "size_mm": 5.0,
        "solid_volume_mm3": 5.0 * 5.0 * 150.0,
        "min_feature_mm": 5.0,
    },
    "cal_dimensional_coupon": {
        "size_mm": 16.0,
        "solid_volume_mm3": math.pi * 8.0**2 * (2.0 + 5.0 + 3.0),
        "min_feature_mm": 2.0,
    },
}


def write_calibration_models() -> dict[str, str]:
    """Export every calibration model as STL and return the written paths."""
    written: dict[str, str] = {}
    for name, mesh in build_calibration_models().items():
        path = HERE / f"{name}.stl"
        mesh.export(path)
        written[name] = str(path.relative_to(HERE.parents[1]))
    return written


if __name__ == "__main__":
    for name, rel in write_calibration_models().items():
        truth = GROUND_TRUTH.get(name, {})
        print(
            f"wrote {rel}  size={truth.get('size_mm')}mm  "
            f"solid={truth.get('solid_volume_mm3', 0):.1f}mm3"
        )
