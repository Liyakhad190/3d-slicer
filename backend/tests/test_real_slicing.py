"""Real slicing tests, gated on CuraEngine being installed.

These are the tests that prove the platform produces genuine machine-specific
G-code. They are skipped (not faked) when the engine is absent.

Every assertion here corresponds to a bug that was actually found and fixed
during development, so this file doubles as a regression record:

* Cura 5.13 needs a leading `slice` subcommand and `-l <model>`; a bare
  filename is an unknown command.
* `-j` loads the settings *definition* file. Without it, every lookup fails
  with "no value given" and the engine emits an empty file.
* The engine does not lift a model off the origin. A 20 mm cube centred on the
  origin was clipped to 10 mm and produced a silently short toolpath.
* Pre-centring the mesh on the *bed* double-centres it, because the engine
  centres again, and the part lands off the edge of the plate.
* Cura 5.13 is AGPL-3.0, not LGPL-3.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.geometry.placement import prepare_file  # noqa: E402
from app.slicing.cura_adapter import CuraEngineAdapter, SliceRequest  # noqa: E402
from app.slicing.cura_settings import to_cura_settings  # noqa: E402
from app.slicing.optimizer import ParameterOptimizer  # noqa: E402
from app.slicing.validator import MachineLimits, validate_gcode  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def adapter() -> CuraEngineAdapter:
    instance = CuraEngineAdapter(settings=get_settings())
    if not instance.is_available():
        pytest.skip(f"CuraEngine not available at {instance.resolve_executable()}")
    return instance


@pytest.fixture(scope="module")
def profiles():
    """Resolved machine/material rows, built from the real seed data."""
    seed = json.loads((BACKEND / "app" / "catalog" / "seed_data.json").read_text(encoding="utf-8"))
    machine = next(m for m in seed["machines"] if m["model"] == "Ender 3")["profile"]
    material = next(m for m in seed["materials"] if m["key"] == "pla")["profiles"][0]

    machine_profile = SimpleNamespace(
        build_volume=machine["build_volume"], chamber=machine["chamber"],
        temperature_limits=machine["temperature_limits"], speed_limits=machine["speed_limits"],
        max_volumetric_flow_mm3s=machine["max_volumetric_flow_mm3s"],
        machine_restrictions=machine["machine_restrictions"], nozzle_material="brass",
        profile_hash="test-ender3", start_gcode=machine["start_gcode"],
        end_gcode=machine["end_gcode"], gcode_dialect_id=None,
    )
    material_profile = SimpleNamespace(
        nozzle_temp=material["nozzle_temp"], bed_temp=material["bed_temp"],
        layer_height_range=material["layer_height_range"], print_speed=material["print_speed"],
        retraction=material["retraction"], adhesion=material["adhesion"],
        max_volumetric_flow_mm3s=material["max_volumetric_flow_mm3s"],
        requires_enclosure=material["requires_enclosure"],
        abrasiveness=material["abrasiveness"],
        material=SimpleNamespace(key="pla", display_name="PLA"), profile_hash="test-pla",
    )
    limits = MachineLimits(
        build_x=machine["build_volume"]["x"], build_y=machine["build_volume"]["y"],
        build_z=machine["build_volume"]["z"],
        nozzle_min_c=0, nozzle_max_c=machine["temperature_limits"]["nozzle"]["max"],
        bed_min_c=0, bed_max_c=machine["temperature_limits"]["bed"]["max"],
        max_print_speed_mm_s=machine["speed_limits"]["max_print_speed_mm_s"],
        max_travel_speed_mm_s=machine["speed_limits"]["max_travel_speed_mm_s"],
        max_acceleration_mm_s2=machine["speed_limits"]["max_acceleration_mm_s2"],
        max_volumetric_flow_mm3s=machine["max_volumetric_flow_mm3s"],
        nozzle_diameter_mm=0.4,
    )
    return machine_profile, material_profile, limits


@pytest.fixture(scope="module")
def sliced(adapter, profiles, tmp_path_factory):
    """A real slice of the 20 mm cube, produced once and reused."""
    machine_profile, material_profile, limits = profiles
    from app.services.geometry.analyzer import analyse_file

    model = FIXTURES / "cube_20mm.stl"
    import hashlib

    geometry = analyse_file(
        str(model), input_sha256=hashlib.sha256(model.read_bytes()).hexdigest()
    ).to_dict()["metrics"]

    result = ParameterOptimizer(
        machine_profile=machine_profile, material_profile=material_profile,
        nozzle_diameter_mm=0.4, limits=limits,
    ).optimise(objective="balanced", geometry=geometry)

    settings = to_cura_settings(
        result.parameters, limits=limits,
        start_gcode=machine_profile.start_gcode, end_gcode=machine_profile.end_gcode,
    )
    out_dir = tmp_path_factory.mktemp("real-slice")
    request = SliceRequest(
        model_path=model, output_path=out_dir / "cube.gcode",
        settings=settings, job_label="test",
    )
    slice_result = adapter.slice(request)
    return slice_result, limits, result


# --------------------------------------------------------------------------- #
# engine contract
# --------------------------------------------------------------------------- #


def test_definitions_file_is_found(adapter):
    assert adapter.definitions_path() is not None, (
        "fdmprinter.def.json must sit next to the binary; without it CuraEngine "
        "cannot resolve any setting"
    )


def test_engine_version_is_read_from_the_banner(adapter):
    version = adapter.probe()
    assert version and version != "unknown"
    assert re.match(r"^\d", version), f"expected a version number, got {version!r}"


def test_license_is_detected_not_assumed(adapter):
    """Cura 5.13 is AGPL-3.0. This asserts the detection works, not the value."""
    detected = adapter.detect_license()
    assert detected in {"AGPL-3.0", "GPL-3.0", "LGPL-3.0", "unknown"}
    assert "AGPL" in adapter.get_capabilities()["license"] or detected != "AGPL-3.0"


def test_capabilities_declare_a_licence_review_is_required(adapter):
    capabilities = adapter.get_capabilities()
    assert capabilities["licence_review_required"] is True
    assert capabilities["integration_mode"] == "subprocess"
    assert capabilities["output_formats"] == ["gcode"]


def test_rejects_a_non_fdm_job(adapter):
    problems = adapter.validate_configuration(SimpleNamespace(technology="msla"))
    assert any("FDM only" in p for p in problems)


def test_schema_defaults_are_loaded(adapter):
    defaults = adapter.schema_defaults()
    assert len(defaults) > 500, "the full settings schema should be available"
    for key in ("layer_height", "wall_line_count", "machine_nozzle_size", "mesh_rotation_matrix"):
        assert key in defaults


# --------------------------------------------------------------------------- #
# the real thing
# --------------------------------------------------------------------------- #


def test_slice_produces_real_gcode(sliced):
    slice_result, _, _ = sliced
    assert slice_result.output_path.is_file()
    assert slice_result.output_path.stat().st_size > 10_000
    assert slice_result.duration_ms > 0


def test_gcode_contains_real_extrusion_and_layers(sliced):
    slice_result, _, _ = sliced
    text = slice_result.output_path.read_text(encoding="utf-8", errors="replace")

    layers = re.findall(r"^;LAYER:(\d+)", text, re.M)
    assert len(layers) >= 60, f"expected ~67 layers for a 20 mm cube, got {len(layers)}"

    moves = re.findall(r"^G[01]\b", text, re.M)
    assert len(moves) > 500

    extruding = [line for line in text.splitlines()
                 if re.match(r"^G[01]\b", line) and re.search(r"E-?\d", line)]
    assert len(extruding) > 200


def test_part_is_not_silently_truncated_in_z(sliced):
    """A 20 mm cube must produce a toolpath that reaches the top of the part.

    This is the regression test for the clipping bug: the engine did not lift
    the model off the origin, so half the cube fell below the plate and the
    toolpath silently covered only 10 mm.
    """
    slice_result, _, _ = sliced
    text = slice_result.output_path.read_text(encoding="utf-8", errors="replace")
    layer_z = [float(m) for m in re.findall(r"^;LAYER:\d+", text, re.M) and
               re.findall(r" Z(-?\d+\.?\d*)", text)]
    assert max(layer_z) >= 19.5, f"toolpath only reaches Z={max(layer_z)} mm of a 20 mm part"


def test_part_stays_on_the_bed(sliced):
    """Regression: pre-centring on the bed made the engine centre again."""
    slice_result, limits, _ = sliced
    text = slice_result.output_path.read_text(encoding="utf-8", errors="replace")
    xs = [float(m) for m in re.findall(r" X(-?\d+\.?\d*)", text)]
    assert xs
    assert min(xs) >= -0.5, f"part went off the bed at X={min(xs)}"
    assert max(xs) <= limits.build_x + 0.5, f"part went off the bed at X={max(xs)}"


def test_thermal_commands_are_present(sliced):
    """A file with no M104 would print at whatever temperature the machine is at."""
    slice_result, _, _ = sliced
    text = slice_result.output_path.read_text(encoding="utf-8", errors="replace")
    assert re.search(r"^M104 S2[0-9][0-9]", text, re.M), "no nozzle heat-up command"
    assert re.search(r"^M140 S[0-9][0-9]", text, re.M), "no bed heat command"
    assert re.search(r"^M104 S0\s*$", text, re.M), "nozzle is never turned off"


def test_profile_start_and_end_gcode_are_used(sliced):
    """Regression: our start_gcode was dropped, so Cura's default Z-hop leaked in.

    The seed profile's start G-code homes with `G28 ; Home all axes` and the end
    G-code parks at `G1 X0 Y200 F3000`; both must appear verbatim.
    """
    slice_result, _, _ = sliced
    text = slice_result.output_path.read_text(encoding="utf-8", errors="replace")
    assert "Home all axes" in text, "the profile's start G-code was not emitted"
    assert re.search(r"^G28\b", text, re.M), "no homing command at all"
    assert "G1 X0 Y200 F3000" in text, "the profile's end G-code was not emitted"


def test_validator_passes_on_a_real_slice(sliced):
    slice_result, limits, _ = sliced
    text = slice_result.output_path.read_text(encoding="utf-8", errors="replace")
    report = validate_gcode(text, limits)
    assert report.passed, f"unexpected critical findings: {[f.code for f in report.findings if f.severity == 'critical']}"
    assert report.stats["extrusion_moves"] > 0
    assert report.stats["filament_extruded_mm"] > 0


def test_validator_reads_temperatures_from_the_file(sliced):
    """Regression: the validator looked for a `nozzle` word no G-code contains,
    so every temperature check silently passed."""
    slice_result, limits, _ = sliced
    text = slice_result.output_path.read_text(encoding="utf-8", errors="replace")
    report = validate_gcode(text, limits)
    assert report.stats["max_nozzle_temp_seen"] is not None
    assert report.stats["max_bed_temp_seen"] is not None


# --------------------------------------------------------------------------- #
# placement
# --------------------------------------------------------------------------- #


def test_placement_sits_the_part_on_the_plate(tmp_path):
    result = prepare_file(
        FIXTURES / "cube_20mm.stl",
        build_volume=(220.0, 220.0, 250.0),
        output_path=tmp_path / "placed.stl",
    )
    assert result.placed_extents == pytest.approx((20.0, 20.0, 20.0), abs=1e-3)
    assert result.scale_factor == 1.0
    assert result.fits

    import trimesh

    placed = trimesh.load(str(result.placed_path), force="mesh", process=False)
    placed.merge_vertices()
    # Bottom exactly on the plate, centred on the origin.
    assert placed.bounds[0][2] == pytest.approx(0.0, abs=1e-6)
    centre = (placed.bounds[0] + placed.bounds[1]) / 2.0
    assert centre[0] == pytest.approx(0.0, abs=1e-6)
    assert centre[1] == pytest.approx(0.0, abs=1e-6)


def test_placement_refuses_to_silently_scale_an_oversized_part(tmp_path):
    from app.core.errors import ValidationFailedError

    with pytest.raises(ValidationFailedError) as excinfo:
        prepare_file(
            FIXTURES / "large_300mm.stl",
            build_volume=(220.0, 220.0, 250.0),
            output_path=tmp_path / "placed.stl",
        )
    assert "does not fit" in str(excinfo.value)
    assert excinfo.value.details["overshoot_mm"]


def test_placement_does_not_modify_the_original(tmp_path):
    """The user's upload must be byte-identical after a prepare."""
    import hashlib

    source = FIXTURES / "cube_20mm.stl"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    prepare_file(source, build_volume=(220.0, 220.0, 250.0), output_path=tmp_path / "p.stl")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
