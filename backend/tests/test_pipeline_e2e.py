"""End-to-end: the whole product flow through the real API and the real engine.

This is the test the requirements' "first working milestone" describes:

    upload STL -> analyze -> select printer/material/nozzle -> resolve profiles
    -> PrintConfiguration -> SlicerEngineAdapter -> REAL SLICING -> REAL G-code
    -> validation -> toolpath -> download

Every stage is the production code path. Nothing is mocked.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "correct-horse-battery-staple"

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def engine_available() -> None:
    from app.slicing.registry import get_registry

    available = get_registry().available()
    if not available:
        pytest.skip("no slicing engine is available")


@pytest.fixture(scope="module")
def account(client):
    email = f"e2e-{abs(hash(str(FIXTURES))) % 10**8}@example.com"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "E2E"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _validated_profile(client, account, machine: str) -> dict:
    """A validated machine profile, promoted the way an operator would.

    Seeded profiles ship `unvalidated` on purpose, so the platform refuses to
    slice for them. This mirrors the real promotion path rather than bypassing
    the check.
    """
    from app.db.session import session_scope
    from app.models.catalog import Machine, MachineProfile, ValidationStatus
    from app.db.base import utcnow

    with session_scope() as db:
        machine_row = db.query(Machine).filter(Machine.model == machine).one()
        profile = (
            db.query(MachineProfile)
            .filter(MachineProfile.machine_id == machine_row.id)
            .order_by(MachineProfile.profile_version.desc())
            .first()
        )
        profile.validation_status = ValidationStatus.validated
        profile.validated_at = utcnow()
        profile.validated_by = "e2e-test"
        db.flush()
        return {
            "id": str(profile.id),
            "machine_id": str(machine_row.id),
            "nozzle_diameters": profile.nozzle_diameters,
        }


def test_technologies_report_what_is_and_is_not_implemented(client, account):
    response = client.get("/api/v1/technologies", headers=account)
    assert response.status_code == 200
    by_key = {row["key"]: row for row in response.json()}

    assert by_key["fdm"]["supported"] is True
    # Resin and powder bed must be declared, and must not pretend to work.
    for key in ("msla", "dlp", "sls", "slm"):
        assert by_key[key]["supported"] is False
        assert by_key[key]["notes"]


def test_slicers_endpoint_reports_real_health(client, account):
    response = client.get("/api/v1/slicers", headers=account)
    assert response.status_code == 200
    engines = {row["engine"]: row for row in response.json()}
    assert "cura" in engines
    assert engines["cura"]["available"] is True
    assert engines["cura"]["version"]
    # The licence is read from the binary, and the review flag is surfaced.
    assert engines["cura"]["license"]
    assert engines["cura"]["capabilities"]["licence_review_required"] is True


def test_unvalidated_machine_profile_is_refused(client, account, engine_available):
    """The no-hallucination rule, enforced: no profile, no output."""
    from app.db.session import session_scope
    from app.models.catalog import Machine, MachineProfile, Nozzle

    with session_scope() as db:
        machine = db.query(Machine).filter(Machine.model == "MK4S").one()
        profile = (
            db.query(MachineProfile)
            .filter(MachineProfile.machine_id == machine.id)
            .order_by(MachineProfile.profile_version.desc())
            .first()
        )
        profile_id = str(profile.id)
        nozzle_id = str(
            db.query(Nozzle).filter(Nozzle.diameter_mm == 0.4, Nozzle.material == "brass").first().id
        )
        material_id = str(
            db.query(__import__("app.models.catalog", fromlist=["MaterialProfile"]).MaterialProfile)
            .filter_by(brand_sku="pla-generic")
            .first()
            .id
        )

    printer = client.post(
        "/api/v1/printers",
        headers=account,
        json={"name": "Refused", "machine_profile_id": profile_id, "nozzle_diameter_mm": 0.4},
    )
    assert printer.status_code == 201

    upload = client.post(
        "/api/v1/models",
        headers=account,
        files={"file": ("cube.stl", (FIXTURES / "cube_20mm.stl").read_bytes(), "application/octet-stream")},
    )
    version_id = upload.json()["version"]["id"]

    response = client.post(
        "/api/v1/slice",
        headers=account,
        json={
            "model_version_id": version_id,
            "printer_id": printer.json()["id"],
            "material_profile_id": material_id,
            "objective": "balanced",
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "INSUFFICIENT_INFORMATION"
    assert "not been validated" in body["message"]


def test_full_pipeline_produces_real_gcode(client, account, engine_available):
    """The milestone: a real, validated, downloadable toolpath."""
    profile = _validated_profile(client, account, "Ender 3")

    from app.db.session import session_scope
    from app.models.catalog import MaterialProfile

    with session_scope() as db:
        material_id = str(
            db.query(MaterialProfile).filter_by(brand_sku="pla-generic").first().id
        )

    printer = client.post(
        "/api/v1/printers",
        headers=account,
        json={
            "name": "Workshop",
            "machine_profile_id": profile["id"],
            "nozzle_diameter_mm": 0.4,
            "nozzle_material": "brass",
        },
    )
    assert printer.status_code == 201, printer.text
    printer_id = printer.json()["id"]

    upload = client.post(
        "/api/v1/models",
        headers=account,
        files={"file": ("cube.stl", (FIXTURES / "cube_20mm.stl").read_bytes(), "application/octet-stream")},
    )
    assert upload.status_code == 201
    version_id = upload.json()["version"]["id"]
    transformed = client.post(
        f"/api/v1/models/versions/{version_id}/transform",
        headers=account,
        json={"scale_percent": {"x": 125, "y": 125, "z": 125}, "preserve_aspect_ratio": True},
    )
    assert transformed.status_code == 200, transformed.text
    assert transformed.json()["dimensions_mm"] == {"x": 25.0, "y": 25.0, "z": 25.0}

    created = client.post(
        "/api/v1/slice",
        headers=account,
        json={
            "model_version_id": version_id,
            "printer_id": printer_id,
            "material_profile_id": material_id,
            "objective": "balanced",
        },
    )
    assert created.status_code == 201, created.text
    job = created.json()
    job_id = job["id"]
    assert job["status"] == "COMPLETED", f"job did not complete: {job}"

    from app.db.session import session_scope
    from app.models.slicing import SliceJob

    with session_scope() as db:
        sliced_job = db.get(SliceJob, job_id)
        assert sliced_job.model_input_sha256 == transformed.json()["analysis"]["input_sha256"]

    # -- stages really ran ------------------------------------------------
    stages = client.get(f"/api/v1/slice/{job_id}/stages", headers=account).json()
    by_stage = {row["stage"]: row for row in stages}
    for expected in ("analyzing", "optimizing", "selecting_engine", "slicing",
                     "validating", "parsing_toolpath"):
        assert expected in by_stage, f"stage {expected} never ran"
        assert by_stage[expected]["status"] in {"succeeded", "skipped"}

    assert by_stage["slicing"]["status"] == "succeeded"
    assert by_stage["validating"]["status"] == "succeeded"
    assert by_stage["selecting_engine"]["detail"]["engine"] == "cura"
    # Unimplemented stages must say so rather than claim success.
    for unimplemented in ("repairing", "simulating"):
        assert by_stage[unimplemented]["status"] == "skipped"
        assert by_stage[unimplemented]["detail"]["implemented"] is False

    # -- validation --------------------------------------------------------
    validation = client.get(f"/api/v1/slice/{job_id}/validation", headers=account).json()
    assert validation["passed"] is True, validation
    assert validation["blocks_output"] is False
    assert validation["severity_counts"].get("critical", 0) == 0

    # -- generated file, downloadable, real G-code ---------------------------
    files = client.get(f"/api/v1/slice/{job_id}/files", headers=account).json()
    assert len(files) == 1
    assert files[0]["kind"] == "gcode"
    assert files[0]["is_blocked"] is False

    download = client.get(
        f"/api/v1/slice/{job_id}/files/{files[0]['id']}/download", headers=account
    )
    assert download.status_code == 200
    gcode = download.content.decode("utf-8", errors="replace")
    assert len(download.content) > 10_000
    assert ";FLAVOR:" in gcode
    layers = re.findall(r"^;LAYER:\d+", gcode, re.M)
    assert len(layers) >= 60, f"expected ~67 layers, got {len(layers)}"
    assert re.search(r"^M104 S2[0-9][0-9]", gcode, re.M), "no nozzle heat-up"
    assert len(re.findall(r"^G[01]\b", gcode, re.M)) > 500

    # -- toolpath for the viewer ---------------------------------------------
    toolpath = client.get(f"/api/v1/slice/{job_id}/toolpath", headers=account).json()
    assert toolpath["blocked"] is False
    assert toolpath["layer_count"] >= 60
    assert len(toolpath["segments"]) > 100
    assert toolpath["segments_elided"] == 0
    assert toolpath["build_volume"]["x"] == 220.0
    kinds = {s["kind"] for s in toolpath["segments"]}
    assert "extrude" in kinds and "travel" in kinds
    # Every segment must lie inside the declared build volume.
    low_x, high_x = 0.0, toolpath["build_volume"]["x"]
    low_y, high_y = 0.0, toolpath["build_volume"]["y"]
    for segment in toolpath["segments"]:
        for point in (segment["from"], segment["to"]):
            assert low_x - 0.5 <= point[0] <= high_x + 0.5
            assert low_y - 0.5 <= point[1] <= high_y + 0.5
            assert -0.5 <= point[2] <= toolpath["build_volume"]["z"] + 0.5

    # -- the job is listed and its events are real ------------------------------
    listed = client.get("/api/v1/slice", headers=account).json()
    assert any(row["id"] == job_id for row in listed)

    events = client.get(f"/api/v1/slice/{job_id}/events", headers=account).json()
    messages = " ".join(e["message"] for e in events)
    assert "Geometry analysed" in messages
    assert "engine selected" in messages
    assert "Sliced in" in messages

    changed = client.post(
        f"/api/v1/models/versions/{version_id}/transform",
        headers=account,
        json={"scale_percent": {"x": 1500, "y": 1500, "z": 1500}, "preserve_aspect_ratio": True},
    )
    assert changed.status_code == 200, changed.text
    stale = client.get(
        f"/api/v1/slice/{job_id}/files/{files[0]['id']}/download", headers=account
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "SLICE_ARTIFACT_STALE"
    stale_toolpath = client.get(f"/api/v1/slice/{job_id}/toolpath", headers=account)
    assert stale_toolpath.status_code == 409
    assert stale_toolpath.json()["error"]["code"] == "SLICE_ARTIFACT_STALE"

    # Uploads are deduplicated by content hash, so leaving this version scaled
    # to 300 mm would hand that geometry to every later test that uploads the
    # same cube. Restore the original so the shared fixture stays clean.
    reset = client.post(
        f"/api/v1/models/versions/{version_id}/transform",
        headers=account,
        json={"reset": True},
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["dimensions_mm"] == {"x": 20.0, "y": 20.0, "z": 20.0}


def test_oversized_model_is_rejected_before_slicing(client, account, engine_available):
    """A 300 mm part on a 220 mm machine must fail fast, not be clipped."""
    profile = _validated_profile(client, account, "Ender 3")
    from app.db.session import session_scope
    from app.models.catalog import MaterialProfile

    with session_scope() as db:
        material_id = str(
            db.query(MaterialProfile).filter_by(brand_sku="pla-generic").first().id
        )

    printer = client.post(
        "/api/v1/printers",
        headers=account,
        json={"name": "Small", "machine_profile_id": profile["id"], "nozzle_diameter_mm": 0.4},
    ).json()

    upload = client.post(
        "/api/v1/models",
        headers=account,
        files={"file": ("cube.stl", (FIXTURES / "cube_20mm.stl").read_bytes(), "application/octet-stream")},
    )
    version_id = upload.json()["version"]["id"]
    scaled = client.post(
        f"/api/v1/models/versions/{version_id}/transform",
        headers=account,
        json={"scale_percent": {"x": 1500, "y": 1500, "z": 1500}, "preserve_aspect_ratio": True},
    )
    assert scaled.status_code == 200, scaled.text
    assert scaled.json()["dimensions_mm"] == {"x": 300.0, "y": 300.0, "z": 300.0}

    response = client.post(
        "/api/v1/slice",
        headers=account,
        json={
            "model_version_id": version_id,
            "printer_id": printer["id"],
            "material_profile_id": material_id,
            "objective": "speed",
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_FAILED"
    assert "larger than" in body["message"]
    assert body["details"]["dimensions_mm"] == {"x": 300.0, "y": 300.0, "z": 300.0}
    assert body["details"]["available_build_volume_mm"]["x"] == 220.0

    # As above: this version is deduplicated by content hash, so a later test
    # uploading the same cube would otherwise inherit this 300 mm scaling.
    reset = client.post(
        f"/api/v1/models/versions/{version_id}/transform",
        headers=account,
        json={"reset": True},
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["dimensions_mm"] == {"x": 20.0, "y": 20.0, "z": 20.0}


def test_material_requiring_an_enclosure_blocks_the_job(client, account, engine_available):
    """Nylon on an open-chamber Ender 3 must be refused, not warned about."""
    profile = _validated_profile(client, account, "Ender 3")
    from app.db.session import session_scope
    from app.models.catalog import MaterialProfile

    with session_scope() as db:
        nylon = db.query(MaterialProfile).filter_by(brand_sku="nylon-generic").first()
        assert nylon is not None and nylon.requires_enclosure
        material_id = str(nylon.id)

    printer = client.post(
        "/api/v1/printers",
        headers=account,
        json={"name": "NoChamber", "machine_profile_id": profile["id"], "nozzle_diameter_mm": 0.4},
    ).json()

    upload = client.post(
        "/api/v1/models",
        headers=account,
        files={"file": ("cube.stl", (FIXTURES / "cube_20mm.stl").read_bytes(), "application/octet-stream")},
    )

    response = client.post(
        "/api/v1/slice",
        headers=account,
        json={
            "model_version_id": upload.json()["version"]["id"],
            "printer_id": printer["id"],
            "material_profile_id": material_id,
            "objective": "balanced",
        },
    )
    assert response.status_code in {201, 409}
    if response.status_code == 201:
        job_id = response.json()["id"]
        # Blocked at the configuration gate, before any toolpath exists.
        assert response.json()["status"] == "BLOCKED"
        assert "heated chamber" in response.json()["error_detail"]
        validation = client.get(f"/api/v1/slice/{job_id}/validation", headers=account).json()
        assert validation["validated"] is False
    else:
        assert "heated chamber" in response.json()["error"]["message"]


def test_cross_tenant_job_access_is_not_found(client, account, engine_available, fresh_headers):
    from app.db.session import session_scope
    from app.models.slicing import SliceJob

    with session_scope() as db:
        any_job = db.query(SliceJob).first()
        if any_job is None:
            pytest.skip("no job to test against")
        job_id = any_job.id

    assert client.get(f"/api/v1/slice/{job_id}", headers=fresh_headers).status_code == 404
    assert client.get(f"/api/v1/slice/{job_id}/files", headers=fresh_headers).status_code == 404
    assert client.get(f"/api/v1/slice/{job_id}/toolpath", headers=fresh_headers).status_code == 404
