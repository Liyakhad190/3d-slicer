"""Unit tests for deterministic machine profile validation and provenance."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.catalog.validator import validate_machine_profile
from app.models.catalog import MotionSystem, Technology, ValidationStatus
from app.slicing.configuration import from_profiles
from app.core.errors import InsufficientInformationError


def _valid_profile_dict() -> dict:
    return {
        "build_volume": {"x": 220, "y": 220, "z": 250},
        "nozzle_diameters": [0.2, 0.4, 0.6, 0.8],
        "heated_bed": True,
        "temperature_limits": {
            "nozzle": {"min": 0, "max": 260},
            "bed": {"min": 0, "max": 100},
        },
        "speed_limits": {
            "max_print_speed_mm_s": 100,
            "max_travel_speed_mm_s": 150,
            "max_acceleration_mm_s2": 500,
        },
        "machine_restrictions": {
            "output_formats": ["gcode"],
        },
    }


def _valid_machine() -> SimpleNamespace:
    return SimpleNamespace(
        vendor="Creality",
        model="Ender 3",
        technology=Technology.fdm,
        motion_system=MotionSystem.cartesian,
    )


def test_valid_profile_passes_validation():
    verdict = validate_machine_profile(_valid_profile_dict(), _valid_machine())
    assert verdict.is_valid is True
    assert verdict.status == ValidationStatus.validated
    assert verdict.reason is None


def test_missing_vendor_or_model_fails():
    bad_machine = SimpleNamespace(
        vendor="",
        model="Ender 3",
        technology=Technology.fdm,
        motion_system=MotionSystem.cartesian,
    )
    verdict = validate_machine_profile(_valid_profile_dict(), bad_machine)
    assert verdict.is_valid is False
    assert "Missing manufacturer" in verdict.reason


def test_invalid_build_volume_dimensions_fail():
    prof = _valid_profile_dict()
    prof["build_volume"] = {"x": -10, "y": 200, "z": 200}
    verdict = validate_machine_profile(prof, _valid_machine())
    assert verdict.is_valid is False
    assert "must be greater than zero" in verdict.reason


def test_empty_nozzle_diameters_fail():
    prof = _valid_profile_dict()
    prof["nozzle_diameters"] = []
    verdict = validate_machine_profile(prof, _valid_machine())
    assert verdict.is_valid is False
    assert "Nozzle diameters" in verdict.reason


def test_inverted_nozzle_temperature_envelope_fails():
    prof = _valid_profile_dict()
    prof["temperature_limits"]["nozzle"] = {"min": 280, "max": 220}
    verdict = validate_machine_profile(prof, _valid_machine())
    assert verdict.is_valid is False
    assert "Inconsistent nozzle temperature" in verdict.reason


def test_missing_speed_limits_fails():
    prof = _valid_profile_dict()
    prof["speed_limits"] = {"max_print_speed_mm_s": 0}
    verdict = validate_machine_profile(prof, _valid_machine())
    assert verdict.is_valid is False
    assert "Speed limit" in verdict.reason or "missing" in verdict.reason.lower()


def test_missing_output_formats_fails():
    prof = _valid_profile_dict()
    prof["machine_restrictions"]["output_formats"] = []
    verdict = validate_machine_profile(prof, _valid_machine())
    assert verdict.is_valid is False
    assert "output format" in verdict.reason


def test_unvalidated_profile_blocks_slicing_configuration():
    mock_machine = _valid_machine()
    mock_profile = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        machine=mock_machine,
        validation_status=ValidationStatus.unvalidated,
        validation_reason="Custom profile not bench-validated",
        build_volume={"x": 220, "y": 220, "z": 250},
        nozzle_diameters=[0.4],
        temperature_limits={"nozzle": {"min": 0, "max": 260}, "bed": {"min": 0, "max": 100}},
        speed_limits={"max_print_speed_mm_s": 100, "max_travel_speed_mm_s": 150, "max_acceleration_mm_s2": 500},
        gcode_dialect=None,
        start_gcode="",
        end_gcode="",
        max_volumetric_flow_mm3s=10.0,
    )
    mock_material = SimpleNamespace(
        material=SimpleNamespace(key="pla"),
        material_diameter=1.75,
        nozzle_temp={"min": 190, "max": 230},
        bed_temp={"min": 50, "max": 70},
    )

    with pytest.raises(InsufficientInformationError) as exc_info:
        from_profiles(
            machine_profile=mock_profile,
            material_profile=mock_material,
            nozzle_diameter_mm=0.4,
            nozzle_material="brass",
            parameters={},
            objective="balanced",
        )
    assert "Custom profile not bench-validated" in str(exc_info.value)
