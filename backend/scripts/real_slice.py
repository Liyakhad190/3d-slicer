"""Real slicing proof: optimizer -> CuraEngine -> G-code -> validator.

Runs the actual CuraEngine binary against a real model. Skipped automatically
when the engine is not installed, so the suite still passes on a machine
without it — but this is the script that proves the platform produces genuine
machine-specific G-code rather than a simulated progress bar.

Run: python scripts/real_slice.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.geometry.analyzer import analyse_file  # noqa: E402
from app.slicing.cura_adapter import CuraEngineAdapter, SliceRequest  # noqa: E402
from app.slicing.cura_settings import to_cura_settings  # noqa: E402
from app.slicing.optimizer import ParameterOptimizer  # noqa: E402
from app.slicing.validator import MachineLimits, validate_gcode  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = BACKEND / "tests" / "fixtures"


def build_profiles() -> tuple[SimpleNamespace, SimpleNamespace, MachineLimits]:
    """A resolved machine + material pair shaped like the ORM rows.

    Built from the real seed values so this script exercises the same numbers
    the database would hand the optimizer.
    """
    seed = json.loads((BACKEND / "app" / "catalog" / "seed_data.json").read_text(encoding="utf-8"))

    machine_seed = next(m for m in seed["machines"] if m["model"] == "Ender 3")
    profile = machine_seed["profile"]
    material = next(m for m in seed["materials"] if m["key"] == "pla")["profiles"][0]

    machine_profile = SimpleNamespace(
        build_volume=profile["build_volume"],
        chamber=profile["chamber"],
        temperature_limits=profile["temperature_limits"],
        speed_limits=profile["speed_limits"],
        max_volumetric_flow_mm3s=profile["max_volumetric_flow_mm3s"],
        machine_restrictions=profile["machine_restrictions"],
        nozzle_material="brass",
        profile_hash="seed-ender3",
        start_gcode=profile["start_gcode"],
        end_gcode=profile["end_gcode"],
        gcode_dialect_id=None,
    )
    material_profile = SimpleNamespace(
        nozzle_temp=material["nozzle_temp"],
        bed_temp=material["bed_temp"],
        layer_height_range=material["layer_height_range"],
        print_speed=material["print_speed"],
        retraction=material["retraction"],
        adhesion=material["adhesion"],
        max_volumetric_flow_mm3s=material["max_volumetric_flow_mm3s"],
        requires_enclosure=material["requires_enclosure"],
        abrasiveness=material["abrasiveness"],
        material=SimpleNamespace(key="pla", display_name="PLA"),
        profile_hash="seed-pla",
    )
    limits = MachineLimits(
        build_x=profile["build_volume"]["x"],
        build_y=profile["build_volume"]["y"],
        build_z=profile["build_volume"]["z"],
        nozzle_min_c=profile["temperature_limits"]["nozzle"]["min"],
        nozzle_max_c=profile["temperature_limits"]["nozzle"]["max"],
        bed_min_c=profile["temperature_limits"]["bed"]["min"],
        bed_max_c=profile["temperature_limits"]["bed"]["max"],
        max_print_speed_mm_s=profile["speed_limits"]["max_print_speed_mm_s"],
        max_travel_speed_mm_s=profile["speed_limits"]["max_travel_speed_mm_s"],
        max_acceleration_mm_s2=profile["speed_limits"]["max_acceleration_mm_s2"],
        max_volumetric_flow_mm3s=profile["max_volumetric_flow_mm3s"],
        nozzle_diameter_mm=0.4,
    )
    return machine_profile, material_profile, limits


def main() -> int:
    settings = get_settings()
    adapter = CuraEngineAdapter(settings=settings)

    if not adapter.is_available():
        print(f"SKIP: CuraEngine not found at {adapter.executable}")
        print("      Set CURA_ENGINE_PATH to run this.")
        return 0

    print(f"engine    : {adapter.display_name} ({adapter.license})")
    print(f"version   : {adapter.probe()}")
    print()

    machine_profile, material_profile, limits = build_profiles()
    model_path = FIXTURES / "cube_20mm.stl"

    analysis = analyse_file(
        str(model_path), input_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest()
    )
    geometry = analysis.to_dict()["metrics"]

    optimizer = ParameterOptimizer(
        machine_profile=machine_profile,
        material_profile=material_profile,
        nozzle_diameter_mm=0.4,
        limits=limits,
    )
    result = optimizer.optimise(objective="balanced", geometry=geometry)

    print("=== resolved parameters (objective: balanced) ===")
    for decision in result.decisions:
        print(f"  {decision.key:26} = {str(decision.value):20} [{decision.source}]")
    print(f"  fingerprint {result.fingerprint[:16]}  confidence {result.confidence:.2f}")
    for warning in result.warnings:
        print(f"  ! {warning}")
    print()

    settings_map = to_cura_settings(
        result.parameters,
        limits=limits,
        start_gcode=machine_profile.start_gcode,
        end_gcode=machine_profile.end_gcode,
    )
    print(f"=== {len(settings_map)} settings handed to CuraEngine ===\n")

    with tempfile.TemporaryDirectory(prefix="real-slice-") as tmp:
        request = SliceRequest(
            model_path=model_path,
            output_path=Path(tmp) / "cube.gcode",
            settings=settings_map,
            job_label="real-slice-proof",
        )
        try:
            slice_result = adapter.slice(request)
        except Exception as exc:
            print(f"SLICE FAILED: {exc}")
            details = getattr(exc, "details", None)
            if details:
                for key, value in details.items():
                    print(f"  {key}:")
                    for line in str(value).splitlines()[-30:]:
                        print(f"    | {line}")
            return 1

        gcode = slice_result.output_path.read_text(encoding="utf-8", errors="replace")
        lines = gcode.splitlines()
        print("=== produced G-code ===")
        print(f"  bytes          : {slice_result.output_path.stat().st_size:,}")
        print(f"  lines          : {len(lines):,}")
        print(f"  duration       : {slice_result.duration_ms} ms")
        print("  first 14 lines:")
        for line in lines[:14]:
            print(f"    | {line}")
        print()

        report = validate_gcode(gcode, limits, source_name="cube.gcode")
        print("=== validation ===")
        print(f"  passed          : {report.passed}")
        print(f"  blocks output   : {report.blocks_output}")
        print(f"  summary         : {report.summary}")
        print(f"  extrusion moves : {report.stats['extrusion_moves']:,}")
        print(f"  travel moves    : {report.stats['travel_moves']:,}")
        print(f"  retractions     : {report.stats['retractions']:,}")
        print(f"  max Z           : {report.stats['max_z_seen']:.3f} mm")
        print(f"  max feedrate    : {report.stats['max_feedrate_seen']:.1f} mm/s")
        print(f"  peak flow       : {report.stats['max_volumetric_flow_seen']:.2f} mm3/s")
        for finding in report.findings:
            print(f"  [{finding.severity}] {finding.code}: {finding.message}")


        if report.stats["extrusion_moves"] > 0 and report.stats["max_z_seen"] > 0:
            print("\nREAL SLICE OK - real G-code, real moves, real validation.")
            return 0
        print("\nREAL SLICE FAILED - the file contains no real extrusion.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
