"""Tests for Phase 3: automatic preparation capabilities.

Covers:
- Geometry mesh validation service (validation.py)
- Printer capabilities projection (capabilities.py)
- Print intent mapping (intent.py)
- Geometry orientation service (services/geometry/orientation.py)
- Orientation API endpoint (POST /preparation/{job_id}/orient)
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import trimesh

from app.catalog.capabilities import PrinterCapabilities
from app.core.errors import ValidationFailedError
from app.models.catalog import ValidationStatus
from app.services.geometry.orientation import (
    generate_candidates,
    verify_candidate,
)
from app.services.geometry.validation import (
    MeshValidationResult,
    validate_mesh,
    validate_mesh_report,
)
from app.slicing.intent import PrintIntent, Objective

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #


def _create(ws, client, **extra):
    """POST /preparation to start a new preparation job."""
    body = {
        "model_version_id": ws.version_id,
        "printer_id": ws.printer_id,
        "material_profile_id": ws.material_profile_id,
        "user_request": "print this",
        **extra,
    }
    return client.post("/api/v1/preparation", headers=ws.headers, json=body)


# --------------------------------------------------------------------------- #
# Mesh Validation Service Tests
# --------------------------------------------------------------------------- #


def test_cube_passes_mesh_validation():
    result = validate_mesh(FIXTURES / "cube_20mm.stl", build_volume=(220.0, 220.0, 250.0))
    assert isinstance(result, MeshValidationResult)
    assert result.valid is True
    assert result.can_slice is True
    assert result.build_volume_fits is True
    assert result.repair_recommended is False
    assert len(result.critical_issues) == 0


def test_open_mesh_fails_watertightness_validation():
    result = validate_mesh(FIXTURES / "open_mesh.stl")
    assert result.valid is False
    assert result.can_slice is False
    assert result.repair_recommended is True
    codes = [issue.code for issue in result.critical_issues]
    assert "NOT_WATERTIGHT" in codes


def test_mesh_exceeds_build_volume_reported():
    result = validate_mesh(
        FIXTURES / "large_300mm.stl",
        build_volume=(200.0, 200.0, 200.0),
    )
    assert result.build_volume_fits is False
    codes = [issue.code for issue in result.critical_issues]
    assert "EXCEEDS_BUILD_VOLUME" in codes


def test_thin_wall_issue_reported():
    result = validate_mesh(FIXTURES / "thin_wall.stl", nozzle_diameter_mm=0.4)
    codes = [issue.code for issue in result.issues]
    assert "THIN_WALL" in codes


# --------------------------------------------------------------------------- #
# PrinterCapabilities Tests
# --------------------------------------------------------------------------- #


def test_printer_capabilities_projection_from_seed():
    seed = json.loads((BACKEND / "app" / "catalog" / "seed_data.json").read_text(encoding="utf-8"))
    machine_raw = next(m for m in seed["machines"] if m["model"] == "Ender 3")
    profile_data = machine_raw["profile"]

    mock_machine = SimpleNamespace(
        vendor=machine_raw["vendor"],
        model=machine_raw["model"],
        technology=machine_raw["technology"],
        motion_system=machine_raw["motion_system"],
    )
    mock_profile = SimpleNamespace(
        machine=mock_machine,
        build_volume=profile_data["build_volume"],
        bed_type=profile_data.get("bed_type", "pei_sheet"),
        heated_bed=profile_data.get("heated_bed", True),
        chamber=profile_data.get("chamber", {}),
        nozzle_diameters=profile_data.get("nozzle_diameters", [0.4]),
        extruders=profile_data.get("extruders", 1),
        temperature_limits=profile_data["temperature_limits"],
        speed_limits=profile_data["speed_limits"],
        max_volumetric_flow_mm3s=profile_data.get("max_volumetric_flow_mm3s", 15.0),
        validation_status=ValidationStatus.validated,
        machine_restrictions=profile_data.get("machine_restrictions", {}),
    )

    caps = PrinterCapabilities.from_profile(mock_profile)
    assert caps.vendor == "Creality"
    assert caps.model == "Ender 3"
    assert caps.technology == "fdm"
    assert caps.build_volume == (220.0, 220.0, 250.0)
    assert caps.heated_bed is True
    assert caps.is_validated is True

    # Nozzle checks
    assert caps.supports_nozzle(0.4) is True
    assert caps.supports_nozzle(1.2) is False

    # Temperature checks
    assert caps.supports_temperature(nozzle_c=200.0, bed_c=60.0) is True
    assert caps.supports_temperature(nozzle_c=400.0, bed_c=60.0) is False

    # Build volume checks
    assert caps.fits_build_volume((100.0, 100.0, 100.0)) is True
    assert caps.fits_build_volume((300.0, 100.0, 100.0)) is False

    # Serialisation
    as_dict = caps.to_dict()
    assert as_dict["vendor"] == "Creality"
    assert as_dict["build_volume_mm"]["x"] == 220.0


# --------------------------------------------------------------------------- #
# Print Intent Tests
# --------------------------------------------------------------------------- #


def test_print_intent_objective_presets():
    quality_intent = PrintIntent.from_objective("quality")
    assert quality_intent.surface_quality > quality_intent.speed
    assert quality_intent.dominant_objective() == "quality"

    speed_intent = PrintIntent.from_objective("speed")
    assert speed_intent.speed > speed_intent.surface_quality
    assert speed_intent.dominant_objective() == "speed"

    strength_intent = PrintIntent.from_objective("strength")
    assert strength_intent.strength >= 0.9
    assert strength_intent.dominant_objective() == "strength"

    balanced_intent = PrintIntent.from_objective("balanced")
    assert balanced_intent.dominant_objective() == "balanced"


# --------------------------------------------------------------------------- #
# Orientation Service Tests
# --------------------------------------------------------------------------- #


def test_geometry_orientation_service():
    mesh = trimesh.load(str(FIXTURES / "cube_20mm.stl"), force="mesh")
    candidates = generate_candidates(mesh, build_volume=(220.0, 220.0, 250.0), limit=5)
    assert len(candidates) > 0
    first = candidates[0]
    assert first.fits is True
    assert first.contact_area_mm2 > 0

    verified = verify_candidate(first.id, candidates)
    assert verified.id == first.id

    with pytest.raises(ValidationFailedError):
        verify_candidate("rot_non_existent", candidates)


# --------------------------------------------------------------------------- #
# Orientation API Endpoint Tests
# --------------------------------------------------------------------------- #


def test_orientation_endpoint_flow(client, workspace, provider):
    # 1. Create a preparation job
    response = _create(workspace, client)
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]

    # 2. Call POST /preparation/{job_id}/orient without candidate_id -> list candidates
    orient_resp = client.post(
        f"/api/v1/preparation/{job_id}/orient",
        json={},
        headers=workspace.headers,
    )
    assert orient_resp.status_code == 200, orient_resp.text
    orient_data = orient_resp.json()
    assert "candidates" in orient_data
    assert len(orient_data["candidates"]) > 0
    candidate_id = orient_data["candidates"][0]["id"]

    # 3. Call POST /preparation/{job_id}/orient with candidate_id -> apply orientation
    apply_resp = client.post(
        f"/api/v1/preparation/{job_id}/orient",
        json={"candidate_id": candidate_id},
        headers=workspace.headers,
    )
    assert apply_resp.status_code == 200, apply_resp.text
    apply_data = apply_resp.json()
    assert apply_data["job_id"] == job_id
    assert apply_data["selected_candidate"]["id"] == candidate_id
    assert "job" in apply_data
    assert apply_data["job"]["plan"]["orientation"]["candidate_id"] == candidate_id
