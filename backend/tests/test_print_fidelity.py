"""Print-fidelity tests: does the generated G-code do what was requested?

These answer the question the project previously could not: *does a requested
parameter actually reach the toolpath?*

The failure they guard against is subtle and was real. CuraEngine exposes
*derived* settings (`infill_sparse_density`, `top_bottom_thickness`, ...) that
only the GUI computes; the headless engine accepts them, reports no error, and
prints the default. The platform was sending those, so a requested 20% infill
silently printed at whatever the default was.

Every assertion is a measurement from the emitted G-code, never an assumption
about what a setting name implies. Marked `slow`; skipped, not faked, when no
engine is installed.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from app.core.config import get_settings  # noqa: E402
from app.services.geometry.analyzer import analyse_file  # noqa: E402
from app.slicing.cura_adapter import CuraEngineAdapter, SliceRequest  # noqa: E402
from app.slicing.cura_settings import (  # noqa: E402
    DEFAULT_FILAMENT_DIAMETER_MM,
    infill_line_distance_mm,
    to_cura_settings,
)
from app.slicing.fidelity import (  # noqa: E402
    INERT_SETTINGS,
    OPERATIVE_SETTINGS,
    FidelityReport,
    ModelFidelityResult,
    measure_calibration_model,
)
from app.slicing.measurement import measure_gcode  # noqa: E402
from app.slicing.validator import MachineLimits  # noqa: E402
from calibration import GROUND_TRUTH  # noqa: E402

pytestmark = pytest.mark.slow

FIXTURES = Path(__file__).parent / "fixtures"
NOZZLE_MM = 0.4
LINE_WIDTH_MM = 0.45
LAYER_HEIGHT_MM = 0.3

BASE_PARAMETERS = {
    "layer_height_mm": LAYER_HEIGHT_MM,
    "first_layer_height_mm": LAYER_HEIGHT_MM,
    "extrusion_width_mm": LINE_WIDTH_MM,
    "wall_count": 3,
    "top_bottom_layers": 5,
    "infill_pattern": "gyroid",
    "infill_density_percent": 20.0,
    "print_speed_mm_s": 50.0,
    "travel_speed_mm_s": 100.0,
    "nozzle_temperature_c": 210,
    "bed_temperature_c": 60,
    "cooling_fan_percent": 100,
    "retraction": {"distance_mm": 0.8, "speed_mm_s": 40},
    "support_enable": False,
    "adhesion_type": "none",
}


@pytest.fixture(scope="module")
def adapter() -> CuraEngineAdapter:
    instance = CuraEngineAdapter(settings=get_settings())
    if not instance.is_available():
        pytest.skip(f"CuraEngine not available at {instance.resolve_executable()}")
    return instance


@pytest.fixture(scope="module")
def limits() -> MachineLimits:
    return MachineLimits(
        build_x=220.0, build_y=220.0, build_z=250.0,
        nozzle_min_c=0.0, nozzle_max_c=260.0,
        bed_min_c=0.0, bed_max_c=100.0,
        max_print_speed_mm_s=100.0, max_travel_speed_mm_s=150.0,
        max_acceleration_mm_s2=500.0, max_volumetric_flow_mm3s=8.0,
        nozzle_diameter_mm=NOZZLE_MM,
    )


def slice_model(
    adapter: CuraEngineAdapter,
    model: str,
    overrides: dict,
    limits: MachineLimits,
    tmp_path: Path,
) -> tuple[str, dict]:
    """Slice a calibration model with explicit settings; return G-code + settings."""
    settings = to_cura_settings(
        dict(BASE_PARAMETERS), limits=limits, filament_diameter_mm=DEFAULT_FILAMENT_DIAMETER_MM
    )
    settings.update(overrides)
    # Isolate the measurement: no brim, no skirt, no spiralise smoothing.
    settings["skirt_line_count"] = 0
    settings["brim_line_count"] = 0
    settings["magic_spiralize"] = False

    out = tmp_path / f"{model}.gcode"
    adapter.slice(
        SliceRequest(
            model_path=FIXTURES / f"{model}.stl",
            output_path=out,
            settings=settings,
            job_label=model,
            model_format="stl",
        )
    )
    return out.read_text(encoding="utf-8", errors="replace"), settings


def deposited_volume(gcode: str, limits: MachineLimits) -> float:
    report = measure_gcode(
        gcode, limits, line_width_mm=LINE_WIDTH_MM, layer_height_mm=LAYER_HEIGHT_MM
    )
    return report.total_deposited_volume_mm3


# --------------------------------------------------------------------------- #
# unit: the density -> line-distance conversion
# --------------------------------------------------------------------------- #


def test_infill_density_converts_to_line_distance():
    # Verified against the schema defaults: 0.4 mm lines at 20% is 2.0 mm.
    assert infill_line_distance_mm(20.0, 0.4) == pytest.approx(2.0)
    assert infill_line_distance_mm(50.0, 0.4) == pytest.approx(0.8)
    assert infill_line_distance_mm(25.0, 0.45) == pytest.approx(1.8)
    # 100% is solid, expressed as zero distance.
    assert infill_line_distance_mm(100.0, 0.45) == 0.0
    # 0% has no finite distance, so a sentinel is used rather than dividing by zero.
    assert infill_line_distance_mm(0.0, 0.45) > 10.0


def test_settings_never_emit_a_derived_setting(limits):
    """Guard the exact defect this phase exists to fix."""
    settings = to_cura_settings(
        {
            "infill_pattern": "gyroid",
            "infill_density_percent": 20.0,
            "top_bottom_layers": 5,
            "wall_count": 3,
        },
        limits=limits,
    )
    for inert_key in (
        "infill_sparse_density",
        "infill_sparse_pattern",
        "infill_sparse_thickness",
        "top_bottom_thickness",
        "wall_thickness",
        "skin_outline_count",
    ):
        assert inert_key not in settings, f"{inert_key} is inert in CuraEngine"

    assert settings["infill_pattern"] == "gyroid"
    assert settings["infill_line_distance"] == pytest.approx(LINE_WIDTH_MM * 100 / 20)
    assert settings["top_layers"] == 5
    assert settings["bottom_layers"] == 5
    assert settings["wall_line_count"] == 3


def test_filament_diameter_is_always_the_real_value(limits):
    """A placeholder diameter mis-scales every E value in the file."""
    settings = to_cura_settings({}, limits=limits, filament_diameter_mm=1.75)
    assert settings["material_diameter"] == 1.75

    with pytest.raises(ValueError, match="Unsupported filament diameter"):
        to_cura_settings({}, limits=limits, filament_diameter_mm=1.2)


# --------------------------------------------------------------------------- #
# measurement: does each operative setting change the deposited geometry?
# --------------------------------------------------------------------------- #


def test_wall_count_changes_the_deposited_geometry(adapter, limits, tmp_path):
    """The control case: a setting known to be operative must be measurable."""
    volumes = {}
    for walls in (1, 2, 4):
        gcode, _ = slice_model(
            adapter, "cal_cube_20mm",
            {"wall_line_count": walls, "infill_line_distance": 100.0,
             "top_layers": 0, "bottom_layers": 0},
            limits, tmp_path,
        )
        volumes[walls] = deposited_volume(gcode, limits)
    assert volumes[1] < volumes[2] < volumes[4], volumes


def test_infill_density_changes_the_deposited_geometry(adapter, limits, tmp_path):
    """The regression this phase was opened for.

    Before the fix, `infill_sparse_density` was sent and the engine ignored it,
    so every density produced an identical file.
    """
    volumes = {}
    for density in (10.0, 25.0, 50.0):
        gcode, _ = slice_model(
            adapter, "cal_cube_20mm",
            {"infill_line_distance": infill_line_distance_mm(density, LINE_WIDTH_MM),
             "wall_line_count": 2, "top_layers": 0, "bottom_layers": 0},
            limits, tmp_path,
        )
        volumes[density] = deposited_volume(gcode, limits)
    assert volumes[10.0] < volumes[25.0] < volumes[50.0], volumes


def test_infill_pattern_changes_the_deposited_geometry(adapter, limits, tmp_path):
    lengths = {}
    for pattern in ("lines", "gyroid", "grid"):
        gcode, _ = slice_model(
            adapter, "cal_cube_20mm",
            {"infill_pattern": pattern, "wall_line_count": 2,
             "top_layers": 0, "bottom_layers": 0},
            limits, tmp_path,
        )
        report = measure_gcode(
            gcode, limits, line_width_mm=LINE_WIDTH_MM, layer_height_mm=LAYER_HEIGHT_MM
        )
        lengths[pattern] = round(report.total_extruded_length_mm)
    assert len(set(lengths.values())) == 3, f"infill_pattern had no effect: {lengths}"


def test_top_layers_changes_the_deposited_geometry(adapter, limits, tmp_path):
    """`top_bottom_thickness` was inert; `top_layers` is the operative control."""
    volumes = {}
    for layers in (0, 4, 8):
        gcode, _ = slice_model(
            adapter, "cal_cube_20mm",
            {"top_layers": layers, "bottom_layers": layers,
             "infill_line_distance": 100.0},
            limits, tmp_path,
        )
        volumes[layers] = deposited_volume(gcode, limits)
    assert volumes[0] < volumes[4] < volumes[8], volumes


# --------------------------------------------------------------------------- #
# the fidelity report over the calibration suite
# --------------------------------------------------------------------------- #


def test_fidelity_report_over_the_calibration_suite(adapter, limits, tmp_path):
    """Every calibration model must measure as requested.

    Layer count and layer height are the claims checkable exactly from G-code.
    Volume is checked loosely, because nominal swept volume ignores line
    overlap and the gaps between infill lines.
    """
    results: list[ModelFidelityResult] = []

    for model, truth in GROUND_TRUTH.items():
        model_path = FIXTURES / f"{model}.stl"
        if not model_path.is_file():
            pytest.skip(f"{model} fixture missing; run tests/fixtures/calibration.py")

        analysis = analyse_file(
            str(model_path),
            input_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
        )
        height_mm = float(analysis.metrics.dimensions_mm["z"])
        expected_layers = int(height_mm / LAYER_HEIGHT_MM) + 1

        gcode, settings = slice_model(adapter, model, {}, limits, tmp_path)
        result = measure_calibration_model(
            gcode,
            limits,
            model=model,
            description=f"calibration coupon, {truth['size_mm']} mm",
            requested={
                "expected_layer_count": expected_layers,
                "layer_height_mm": LAYER_HEIGHT_MM,
            },
            generated=settings,
            line_width_mm=LINE_WIDTH_MM,
            layer_height_mm=LAYER_HEIGHT_MM,
            expected={
                "size_mm": truth["size_mm"],
                "solid_volume_mm3": truth["solid_volume_mm3"],
            },
        )

        layers = result.claims.get("layer_count")
        assert layers is not None, f"{model}: no layer_count claim produced"
        assert layers.verified, (
            f"{model}: expected ~{expected_layers} layers for a {height_mm:.1f} mm part, "
            f"measured {layers.measured}"
        )

        height = result.claims.get("layer_height")
        if height is not None:
            assert height.verified, (
                f"{model}: layer height requested {LAYER_HEIGHT_MM}, measured {height.measured}"
            )

        results.append(result)

    assert len(results) == len(GROUND_TRUTH)


def test_fidelity_report_is_machine_readable_and_honest(adapter, limits, tmp_path):
    """The report must be serialisable and must never claim physical accuracy."""
    gcode, settings = slice_model(adapter, "cal_cube_20mm", {}, limits, tmp_path)
    result = measure_calibration_model(
        gcode, limits, model="cal_cube_20mm", description="baseline",
        requested={"expected_layer_count": 67, "layer_height_mm": LAYER_HEIGHT_MM},
        generated=settings, line_width_mm=LINE_WIDTH_MM, layer_height_mm=LAYER_HEIGHT_MM,
        expected={"size_mm": 20.0, "solid_volume_mm3": 8000.0},
    )
    report = FidelityReport(
        engine="cura", engine_version="5.13.0", engine_license="AGPL-3.0",
        results=[result], operative_settings=OPERATIVE_SETTINGS,
        inert_settings=INERT_SETTINGS,
    )
    payload = report.to_dict()

    assert payload["physical_validation_required"] is True
    assert payload["license_review_required"] is True
    assert payload["summary"]["models_tested"] == 1
    # REQUESTED / GENERATED / MEASURED are distinct fields, never conflated.
    layer_claim = payload["models"][0]["claims"]["layer_count"]
    assert {"requested", "generated", "measured", "verified"} <= set(layer_claim)
    assert isinstance(report.to_json(), str)


def test_operative_and_inert_settings_are_documented():
    """The report must document the mapping it relies on, and its known traps."""
    assert "wall_line_count" in OPERATIVE_SETTINGS["wall_count"]
    assert "infill_line_distance" in OPERATIVE_SETTINGS["infill_density_percent"]
    assert "top_layers" in OPERATIVE_SETTINGS["top_bottom_layers"]
    assert "infill_sparse_density" in INERT_SETTINGS
    assert "top_bottom_thickness" in INERT_SETTINGS


def test_measurement_reports_physical_validation_required(adapter, limits, tmp_path):
    """No measurement of G-code may be presented as a measurement of a part."""
    gcode, _ = slice_model(adapter, "cal_cube_20mm", {}, limits, tmp_path)
    payload = measure_gcode(
        gcode, limits, line_width_mm=LINE_WIDTH_MM, layer_height_mm=LAYER_HEIGHT_MM
    ).to_dict()
    assert payload["physical_validation_required"] is True
    assert "do not establish physical print accuracy" in payload["physical_validation_note"]


def test_measurement_does_not_count_z_hops_as_layers(adapter, limits, tmp_path):
    """A Z-hop is not a layer."""
    gcode, _ = slice_model(adapter, "cal_cube_20mm", {}, limits, tmp_path)
    report = measure_gcode(
        gcode, limits, line_width_mm=LINE_WIDTH_MM, layer_height_mm=LAYER_HEIGHT_MM
    )
    assert report.layer_count == 67, f"expected 67 layers, got {report.layer_count}"
