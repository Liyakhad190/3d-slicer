"""Generate the machine-readable print-fidelity report.

Runs the calibration suite against the real engine and writes
`data/fidelity-report.json`. Every claim is REQUESTED / GENERATED / MEASURED,
and the report states that no physical validation was performed.

Run: python scripts/fidelity_report.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests" / "fixtures"))

from app.core.config import get_settings  # noqa: E402
from app.services.geometry.analyzer import analyse_file  # noqa: E402
from app.slicing.cura_adapter import CuraEngineAdapter, SliceRequest  # noqa: E402
from app.slicing.cura_settings import (  # noqa: E402
    DEFAULT_FILAMENT_DIAMETER_MM,
    to_cura_settings,
)
from app.slicing.fidelity import (  # noqa: E402
    INERT_SETTINGS,
    OPERATIVE_SETTINGS,
    FidelityReport,
    measure_calibration_model,
)
from app.slicing.validator import MachineLimits  # noqa: E402
from calibration import GROUND_TRUTH  # noqa: E402

LINE_WIDTH_MM = 0.45
LAYER_HEIGHT_MM = 0.3

PARAMETERS = {
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

LIMITS = MachineLimits(
    build_x=220.0, build_y=220.0, build_z=250.0,
    nozzle_min_c=0.0, nozzle_max_c=260.0, bed_min_c=0.0, bed_max_c=100.0,
    max_print_speed_mm_s=100.0, max_travel_speed_mm_s=150.0,
    max_acceleration_mm_s2=500.0, max_volumetric_flow_mm3s=8.0,
    nozzle_diameter_mm=0.4,
)


def main() -> int:
    adapter = CuraEngineAdapter(settings=get_settings())
    if not adapter.is_available():
        print(f"SKIP: {adapter.resolve_executable()} is not available")
        return 0

    report = FidelityReport(
        engine=adapter.key,
        engine_version=adapter.probe() or "unknown",
        engine_license=adapter.detect_license(),
        operative_settings=OPERATIVE_SETTINGS,
        inert_settings=INERT_SETTINGS,
    )

    settings = to_cura_settings(
        dict(PARAMETERS), limits=LIMITS, filament_diameter_mm=DEFAULT_FILAMENT_DIAMETER_MM
    )
    settings["skirt_line_count"] = 0
    settings["brim_line_count"] = 0
    settings["magic_spiralize"] = False

    workdir = Path(tempfile.mkdtemp(prefix="fidelity-"))
    failures: list[str] = []

    for model, truth in GROUND_TRUTH.items():
        model_path = BACKEND / "tests" / "fixtures" / f"{model}.stl"
        if not model_path.is_file():
            print(f"  {model:28} SKIP (fixture missing)")
            continue

        analysis = analyse_file(
            str(model_path),
            input_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
        )
        height_mm = float(analysis.metrics.dimensions_mm["z"])
        expected_layers = int(height_mm / LAYER_HEIGHT_MM) + 1

        out = workdir / f"{model}.gcode"
        adapter.slice(
            SliceRequest(
                model_path=model_path, output_path=out, settings=dict(settings),
                job_label=model, model_format="stl",
            )
        )
        gcode = out.read_text(encoding="utf-8", errors="replace")

        result = measure_calibration_model(
            gcode, LIMITS, model=model,
            description=f"calibration coupon, {truth['size_mm']} mm",
            requested={"expected_layer_count": expected_layers,
                       "layer_height_mm": LAYER_HEIGHT_MM},
            generated=settings,
            line_width_mm=LINE_WIDTH_MM, layer_height_mm=LAYER_HEIGHT_MM,
            expected={"size_mm": truth["size_mm"],
                      "solid_volume_mm3": truth["solid_volume_mm3"]},
        )
        report.results.append(result)

        layers = result.claims.get("layer_count")
        mark = "OK " if (layers and layers.verified) else "FAIL"
        if not (layers and layers.verified):
            failures.append(f"{model}: layer count")
        print(f"  {model:28} {mark} layers={layers.measured if layers else '?'} "
              f"(expected ~{expected_layers})  vol={result.measurement['deposited_volume_mm3']:.1f} mm3")

    out_path = REPO / "data" / "fidelity-report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report.to_json(), encoding="utf-8")

    print()
    print(f"engine        : {report.engine} {report.engine_version} [{report.engine_license}]")
    print(f"models tested : {len(report.results)}")
    print(f"verified      : {report.verified}")
    print(f"written       : {out_path}")
    print(f"physical validation performed: NO (toolpath measurements only)")

    if failures:
        print(f"\nFAILED claims: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
