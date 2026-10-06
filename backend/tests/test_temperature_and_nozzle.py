"""Nozzle and temperature accuracy.

Covers the guarantees the platform makes about the two values a bad answer
can damage most:

* the machine profile — never the UI, never the LLM — decides which nozzle
  diameters a printer accepts, and an unsupported one is refused with the
  supported list attached;
* temperatures resolve deterministically through the safety hierarchy
  (printer ∩ material), a range conflict is a structured rejection rather
  than a silent clamp, and first-layer/bed targets stay inside the safe
  envelope;
* emitted G-code outside the validated envelope fails validation with
  `GCODE_TEMPERATURE_OUT_OF_RANGE`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.slicing.temperature import resolve_safe_temperatures  # noqa: E402
from app.slicing.validator import MachineLimits, validate_gcode  # noqa: E402


# --------------------------------------------------------------------------- #
# resolver unit tests
# --------------------------------------------------------------------------- #


def _machine(nozzle: dict, bed: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        temperature_limits={
            "nozzle": dict(nozzle),
            "bed": dict(bed or {"min": 0, "max": 100}),
        },
        gcode_dialect=None,
    )


def _material(
    nozzle: dict, bed: dict | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        nozzle_temp=dict(nozzle),
        bed_temp=dict(bed or {"min": 50, "typical": 60, "max": 70}),
    )


PLA = _material({"min": 190, "typical": 210, "max": 230})
ENDER = _machine({"min": 0, "max": 260})


def test_safe_range_is_the_intersection_of_printer_and_material():
    res = resolve_safe_temperatures(machine_profile=ENDER, material_profile=PLA)
    assert res.is_conflict is False
    assert res.safe_min_c == 190
    assert res.safe_max_c == 230
    assert res.printer_max_c == 260
    assert res.material_min_c == 190
    # The default target is the manufacturer typical, inside the envelope.
    assert res.safe_min_c <= res.target_nozzle_temp_c <= res.safe_max_c
    assert res.target_nozzle_temp_c == 210


def test_first_layer_stays_inside_the_safe_range():
    res = resolve_safe_temperatures(machine_profile=ENDER, material_profile=PLA)
    assert res.first_layer_nozzle_temp_c >= res.target_nozzle_temp_c
    assert res.first_layer_nozzle_temp_c <= res.safe_max_c

    # A target at the top of the range must not push the first layer past it.
    hot = _material({"min": 190, "typical": 230, "max": 230})
    res = resolve_safe_temperatures(machine_profile=ENDER, material_profile=hot)
    assert res.target_nozzle_temp_c == 230
    assert res.first_layer_nozzle_temp_c <= res.safe_max_c


def test_bed_target_stays_inside_the_safe_bed_range():
    res = resolve_safe_temperatures(machine_profile=ENDER, material_profile=PLA)
    assert res.bed_safe_min_c == 50
    assert res.bed_safe_max_c == 70
    assert res.bed_safe_min_c <= res.target_bed_temp_c <= res.bed_safe_max_c
    assert res.first_layer_bed_temp_c == res.target_bed_temp_c


def test_no_overlap_is_a_conflict_not_a_silent_clamp():
    # A printer whose hotend tops out below what the material needs.
    cold_printer = _machine({"min": 0, "max": 180})
    nylon = _material({"min": 250, "typical": 270, "max": 280})
    res = resolve_safe_temperatures(machine_profile=cold_printer, material_profile=nylon)
    assert res.is_conflict is True
    assert res.conflict_code == "TEMPERATURE_RANGE_CONFLICT"
    assert res.provenance == "conflict"
    assert res.safe_min_c > res.safe_max_c
    # No target is fabricated out of an impossible range.
    assert res.target_nozzle_temp_c == 0
    assert "180" in res.conflict_message
    assert "250" in res.conflict_message


def test_user_override_outside_the_safe_range_is_rejected_not_clamped():
    res = resolve_safe_temperatures(
        machine_profile=ENDER,
        material_profile=PLA,
        user_override_nozzle=300,
    )
    assert res.is_conflict is True
    assert res.conflict_code == "TEMPERATURE_RANGE_CONFLICT"
    assert "300" in res.conflict_message
    assert "[190, 230]" in res.conflict_message


def test_user_override_inside_the_safe_range_is_honoured():
    res = resolve_safe_temperatures(
        machine_profile=ENDER,
        material_profile=PLA,
        user_override_nozzle=200,
        user_override_bed=65,
    )
    assert res.is_conflict is False
    assert res.target_nozzle_temp_c == 200
    assert res.provenance == "user_preference"
    assert res.target_bed_temp_c == 65


def test_ai_recommendation_inside_the_range_is_used():
    res = resolve_safe_temperatures(
        machine_profile=ENDER,
        material_profile=PLA,
        ai_recommended_nozzle=215,
    )
    assert res.target_nozzle_temp_c == 215
    assert res.provenance == "ai_recommendation"
    assert not res.warnings


def test_ai_recommendation_outside_the_range_is_never_used():
    res = resolve_safe_temperatures(
        machine_profile=ENDER,
        material_profile=PLA,
        ai_recommended_nozzle=500,
    )
    # Falls back to the manufacturer recommendation inside the envelope.
    assert res.target_nozzle_temp_c == 210
    assert res.provenance == "material_recommended"
    assert res.safe_min_c <= res.target_nozzle_temp_c <= res.safe_max_c
    assert any("outside safe range" in w for w in res.warnings)


def test_calibrated_value_outside_the_range_is_clamped_with_a_warning():
    res = resolve_safe_temperatures(
        machine_profile=ENDER,
        material_profile=PLA,
        calibrated_nozzle=400,
        calibration_provenance="bench-run-7",
    )
    assert res.safe_min_c <= res.target_nozzle_temp_c <= res.safe_max_c
    assert res.provenance == "calibrated_clamped"
    assert res.warnings, "a clamp must never be silent"


def test_bed_conflict_is_reported_when_the_material_exceeds_the_bed():
    printer = _machine({"min": 0, "max": 260}, bed={"min": 0, "max": 80})
    material = _material(
        {"min": 190, "typical": 210, "max": 230},
        bed={"min": 90, "typical": 100, "max": 110},
    )
    res = resolve_safe_temperatures(machine_profile=printer, material_profile=material)
    assert res.bed_safe_min_c > res.bed_safe_max_c


# --------------------------------------------------------------------------- #
# G-code validation
# --------------------------------------------------------------------------- #


def _limits(**overrides) -> MachineLimits:
    base = dict(
        build_x=220.0,
        build_y=220.0,
        build_z=250.0,
        nozzle_min_c=0.0,
        nozzle_max_c=260.0,
        bed_min_c=0.0,
        bed_max_c=100.0,
        max_print_speed_mm_s=100.0,
        max_travel_speed_mm_s=150.0,
        max_acceleration_mm_s2=500.0,
        max_volumetric_flow_mm3s=8.0,
        nozzle_diameter_mm=0.4,
    )
    base.update(overrides)
    return MachineLimits(**base)


def _gcode(*, nozzle: int = 210, bed: int = 60) -> str:
    # An extruding move keeps NO_EXTRUSION from masking the temperature check.
    return (
        f"M104 S{nozzle}\n"
        f"M140 S{bed}\n"
        "G90\n"
        "G1 X10 Y10 Z0.3 F3000\n"
        "G1 X20 Y10 E1.0 F1500\n"
    )


def _temp_findings(report):
    return [f for f in report.findings if f.code == "GCODE_TEMPERATURE_OUT_OF_RANGE"]


def test_gcode_above_the_machine_limit_is_blocked():
    report = validate_gcode(_gcode(nozzle=300), _limits())
    findings = _temp_findings(report)
    assert findings, "an out-of-envelope nozzle target must produce a finding"
    assert findings[0].severity == "critical"
    assert findings[0].measured_value == 300
    assert findings[0].limit_value == 260
    assert report.blocks_output is True
    assert report.passed is False


def test_gcode_above_the_validated_safe_maximum_is_blocked():
    report = validate_gcode(
        _gcode(nozzle=250),
        _limits(safe_nozzle_min_c=190.0, safe_nozzle_max_c=230.0),
    )
    findings = _temp_findings(report)
    assert findings, "exceeding the validated safe maximum must block"
    assert findings[0].limit_value == 230.0
    assert report.blocks_output is True


def test_gcode_below_the_validated_safe_minimum_is_blocked():
    report = validate_gcode(
        _gcode(nozzle=150),
        _limits(safe_nozzle_min_c=190.0, safe_nozzle_max_c=230.0),
    )
    findings = _temp_findings(report)
    assert findings, "printing below the material's minimum must block"
    assert findings[0].measured_value == 150
    assert report.blocks_output is True


def test_gcode_inside_the_validated_range_passes_the_temperature_checks():
    report = validate_gcode(
        _gcode(nozzle=210, bed=60),
        _limits(safe_nozzle_min_c=190.0, safe_nozzle_max_c=230.0),
    )
    assert not _temp_findings(report)
    assert not [f for f in report.findings if f.code == "BED_TEMP_TOO_HIGH"]
    assert report.passed is True
    assert report.blocks_output is False


def test_bed_above_the_machine_limit_blocks_output():
    report = validate_gcode(_gcode(bed=120), _limits())
    bed_findings = [f for f in report.findings if f.code == "BED_TEMP_TOO_HIGH"]
    assert bed_findings, "an over-limit bed target must produce a finding"
    assert bed_findings[0].severity == "critical"
    assert report.blocks_output is True


# --------------------------------------------------------------------------- #
# profile-level nozzle authority (HTTP)
# --------------------------------------------------------------------------- #


def test_profile_level_nozzle_not_in_supported_list_is_rejected(
    client, fresh_headers, seeded
):
    # 0.5 mm passes the schema's sanity bound (0 < x <= 10) but is not offered
    # by the Bambu X1 Carbon profile: only the profile can refuse this, so the
    # rejection must come back with the supported list attached.
    profile = next(
        p for p in seeded["profiles"].values() if 0.5 not in p["nozzle_diameters"]
    )
    response = client.post(
        "/api/v1/printers",
        headers=fresh_headers,
        json={
            "name": "Unsupported",
            "machine_profile_id": profile["id"],
            "nozzle_diameter_mm": 0.5,
            "nozzle_material": "brass",
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_FAILED"
    assert "0.5" in body["message"]
    assert body["details"]["requested_mm"] == "0.5"
    assert body["details"]["supported_mm"] == sorted(
        str(n) for n in profile["nozzle_diameters"]
    )


def test_every_seeded_profile_defaults_to_a_supported_nozzle(seeded):
    for profile in seeded["profiles"].values():
        default = profile["default_nozzle_diameter_mm"]
        assert default in profile["nozzle_diameters"]
        assert profile["nozzle_diameters"], "a profile must declare nozzles"
        assert len(profile["nozzle_diameters"]) == len(
            set(profile["nozzle_diameters"])
        ), "duplicate nozzle diameters in a profile"


# --------------------------------------------------------------------------- #
# /temperature-range endpoint (HTTP)
# --------------------------------------------------------------------------- #


def _register_printer(client, headers, profile, nozzle_mm=0.4):
    response = client.post(
        "/api/v1/printers",
        headers=headers,
        json={
            "name": "Range Probe",
            "machine_profile_id": profile["id"],
            "nozzle_diameter_mm": nozzle_mm,
            "nozzle_material": "brass",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_temperature_range_endpoint_reports_validated_targets(
    client, fresh_headers, seeded
):
    profile = next(
        p
        for p in seeded["profiles"].values()
        if 0.4 in p["nozzle_diameters"]
    )
    printer = _register_printer(client, fresh_headers, profile)
    pla = next(
        p for p in seeded["material_profiles"].values() if p["brand_sku"] == "pla-generic"
    )
    response = client.get(
        "/api/v1/temperature-range",
        headers=fresh_headers,
        params={"printer_id": printer["id"], "material_profile_id": pla["id"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "valid"
    assert body["safe_min_c"] >= body["printer_min_c"]
    assert body["safe_max_c"] <= body["printer_max_c"]
    assert body["safe_min_c"] >= body["material_min_c"]
    assert body["safe_max_c"] <= body["material_max_c"]
    assert body["safe_min_c"] <= body["recommended_c"] <= body["safe_max_c"]
    assert (
        body["recommended_c"]
        <= body["first_layer_recommended_c"]
        <= body["safe_max_c"]
    )
    assert (
        body["bed_safe_min_c"]
        <= body["bed_recommended_c"]
        <= body["bed_safe_max_c"]
    )
    assert body["provenance"]
    assert body["reason"]
    assert body["warnings"] == []


def test_temperature_range_endpoint_is_scoped_to_the_owner(
    client, fresh_headers, auth_headers, seeded
):
    profile = next(p for p in seeded["profiles"].values())
    printer = _register_printer(client, fresh_headers, profile)
    pla = next(p for p in seeded["material_profiles"].values())
    # A different account must not read another user's resolved range.
    response = client.get(
        "/api/v1/temperature-range",
        headers=auth_headers,
        params={"printer_id": printer["id"], "material_profile_id": pla["id"]},
    )
    assert response.status_code in (403, 404)
