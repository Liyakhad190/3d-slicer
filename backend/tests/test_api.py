"""Integration tests for the HTTP API.

These drive the real FastAPI app through TestClient against an isolated
database, so they exercise routing, auth, validation and the error envelope
together.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "correct-horse-battery-staple"


def _upload(client, headers, name: str, data: bytes):
    return client.post(
        "/api/v1/models",
        headers=headers,
        files={"file": (name, data, "application/octet-stream")},
    )


# --------------------------------------------------------------------------- #
# system
# --------------------------------------------------------------------------- #


def test_health_and_version(client):
    assert client.get("/health").json()["status"] == "ok"
    version = client.get("/version").json()
    assert version["app"] == "slicing-platform"


def test_ready_reports_dependency_checks(client):
    body = client.get("/ready").json()
    assert body["status"] in {"ready", "degraded"}
    assert body["checks"]["database"]["ok"] is True
    assert "slicer" in body["checks"]


def test_openapi_is_served(client):
    schema = client.get("/openapi.json").json()
    assert "/api/v1/models" in schema["paths"]


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #


def test_register_returns_token_and_user(client, fresh_headers):
    # fresh_headers already registered this account; verify it works.
    me = client.get("/api/v1/auth/me", headers=fresh_headers)
    assert me.status_code == 200
    assert me.json()["role"] == "user"


def test_duplicate_email_conflicts(client, auth_headers):
    body = client.get("/api/v1/auth/me", headers=auth_headers).json()
    email = body["email"]
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Impostor"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_email_login_is_case_insensitive(client, auth_headers):
    email = client.get("/api/v1/auth/me", headers=auth_headers).json()["email"]
    response = client.post(
        "/api/v1/auth/login", json={"email": email.upper(), "password": PASSWORD}
    )
    assert response.status_code == 200


def test_wrong_password_is_unauthenticated(client, auth_headers):
    email = client.get("/api/v1/auth/me", headers=auth_headers).json()["email"]
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "definitely-wrong"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_unknown_account_gives_the_same_error_as_a_wrong_password(client, auth_headers):
    unknown = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "whatever-password"},
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"]["message"] == "Incorrect email or password."


def test_short_password_rejected(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": f"x{uuid.uuid4().hex[:6]}@example.com", "password": "short", "display_name": "X"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_missing_token_is_unauthenticated(client):
    assert client.get("/api/v1/models").status_code == 401


def test_garbage_token_is_rejected(client):
    response = client.get("/api/v1/models", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_validation_error_does_not_echo_the_password(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "password": "super-secret-value", "display_name": "X"},
    )
    assert response.status_code == 422
    assert "super-secret-value" not in response.text


# --------------------------------------------------------------------------- #
# catalogue
# --------------------------------------------------------------------------- #


def test_machines_and_profiles_are_seeded(client, auth_headers):
    machines = client.get("/api/v1/machines", headers=auth_headers).json()
    assert len(machines) >= 1
    assert {"id", "vendor", "model", "technology", "motion_system"} <= set(machines[0])

    profiles = client.get("/api/v1/machine-profiles", headers=auth_headers).json()
    assert len(profiles) >= 1
    assert profiles[0]["build_volume"]["x"] > 0
    # Community seed data must not claim validation it has not had.
    assert all(p["validation_status"] == "unvalidated" for p in profiles)


def test_material_profiles_carry_real_constraints(client, auth_headers):
    profiles = client.get("/api/v1/material-profiles", headers=auth_headers).json()
    by_sku = {p["brand_sku"]: p for p in profiles}

    assert "pla-generic" in by_sku
    pla = by_sku["pla-generic"]
    assert pla["nozzle_temp"]["min"] < pla["nozzle_temp"]["typical"] < pla["nozzle_temp"]["max"]
    assert pla["layer_height_range"]["min"] < pla["layer_height_range"]["max"]
    assert pla["moisture_sensitivity"] in {"low", "medium", "high", "very_high"}

    assert by_sku["pla-generic"]["abrasiveness"] == "none"

    # Composites must be flagged abrasive so a brass nozzle gets a warning.
    assert by_sku["pa-cf-generic"]["abrasiveness"] == "high"

    # Nylon must demand an enclosure.
    assert by_sku["nylon-generic"]["requires_enclosure"] is True


def test_nozzles_are_seeded(client, auth_headers):
    nozzles = client.get("/api/v1/nozzles", headers=auth_headers).json()
    diameters = {float(n["diameter_mm"]) for n in nozzles}
    assert {0.4, 0.6, 0.8}.issubset(diameters)


def test_create_printer_and_reject_unsupported_nozzle(client, fresh_headers, seeded):
    profile_id = next(iter(seeded["profiles"]))
    profile = seeded["profiles"][profile_id]
    supported = float(profile["nozzle_diameters"][0])

    created = client.post(
        "/api/v1/printers",
        headers=fresh_headers,
        json={
            "name": "Workshop FDM",
            "machine_profile_id": profile_id,
            "nozzle_diameter_mm": supported,
            "nozzle_material": "brass",
        },
    )
    assert created.status_code == 201, created.text

    rejected = client.post(
        "/api/v1/printers",
        headers=fresh_headers,
        json={
            "name": "Bad nozzle",
            "machine_profile_id": profile_id,
            "nozzle_diameter_mm": 99.0,
        },
    )
    assert rejected.status_code == 422


def test_archiving_printer_hides_it_but_preserves_its_slice_history(
    client, fresh_headers
):
    from app.db.session import session_scope
    from app.models.identity import User
    from tests.job_factory import build_sliced_job

    account = client.get("/api/v1/auth/me", headers=fresh_headers).json()
    with session_scope() as db:
        owner = db.get(User, uuid.UUID(account["id"]))
        refs = build_sliced_job(db, user_id=owner.id)
        printer_id = refs["printer_id"]

    archived = client.delete(f"/api/v1/printers/{printer_id}", headers=fresh_headers)
    assert archived.status_code == 204
    active = client.get("/api/v1/printers", headers=fresh_headers).json()
    assert all(row["id"] != str(printer_id) for row in active)
    assert client.get(f"/api/v1/printers/{printer_id}", headers=fresh_headers).status_code == 404
    assert client.get(
        f"/api/v1/slice/{refs['job_id']}/toolpath", headers=fresh_headers
    ).status_code == 200
    preparation = client.post(
        "/api/v1/preparation",
        headers=fresh_headers,
        json={
            "model_version_id": str(refs["version_id"]),
            "printer_id": str(printer_id),
            "material_profile_id": str(refs["material_profile_id"]),
            "objective": "balanced",
        },
    )
    assert preparation.status_code == 404


def test_printer_creation_requires_a_real_profile(client, fresh_headers):
    response = client.post(
        "/api/v1/printers",
        headers=fresh_headers,
        json={
            "name": "Ghost",
            "machine_profile_id": str(uuid.uuid4()),
            "nozzle_diameter_mm": 0.4,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #


def test_upload_and_analyse_a_clean_cube(client, fresh_headers):
    upload = _upload(client, fresh_headers, "cube.stl", (FIXTURES / "cube_20mm.stl").read_bytes())
    assert upload.status_code == 201, upload.text
    body = upload.json()
    version_id = body["version"]["id"]
    assert body["version"]["format"] == "stl_binary"

    analysis = client.post(f"/api/v1/models/versions/{version_id}/analyze", headers=fresh_headers)
    assert analysis.status_code == 200
    payload = analysis.json()
    assert payload["metrics"]["is_watertight"] is True
    assert payload["metrics"]["dimensions_mm"] == {"x": 20.0, "y": 20.0, "z": 20.0}
    assert payload["printability_score"] == 100.0
    assert payload["risk_level"] == "low"
    assert payload["findings"] == []


def test_model_transform_modes_change_authoritative_geometry_and_reset(client, fresh_headers):
    upload = _upload(client, fresh_headers, "cube.stl", (FIXTURES / "cube_20mm.stl").read_bytes())
    version_id = upload.json()["version"]["id"]
    endpoint = f"/api/v1/models/versions/{version_id}/transform"

    uniform = client.post(
        endpoint,
        headers=fresh_headers,
        json={"scale_percent": {"x": 150, "y": 150, "z": 150}, "preserve_aspect_ratio": True},
    )
    assert uniform.status_code == 200, uniform.text
    assert uniform.json()["dimensions_mm"] == {"x": 30.0, "y": 30.0, "z": 30.0}
    transformed_download = client.get(
        f"/api/v1/models/versions/{version_id}/file", headers=fresh_headers
    )
    assert transformed_download.status_code == 200
    assert transformed_download.headers["content-disposition"].endswith("cube.stl\"")
    assert transformed_download.content != (FIXTURES / "cube_20mm.stl").read_bytes()

    non_uniform = client.post(
        endpoint,
        headers=fresh_headers,
        json={"scale_percent": {"x": 100, "y": 125, "z": 50}},
    )
    assert non_uniform.status_code == 200, non_uniform.text
    assert non_uniform.json()["dimensions_mm"] == {"x": 20.0, "y": 25.0, "z": 10.0}

    direct_dimensions = client.post(
        endpoint,
        headers=fresh_headers,
        json={"target_dimensions_mm": {"x": 12, "y": 14, "z": 16}},
    )
    assert direct_dimensions.status_code == 200, direct_dimensions.text
    assert direct_dimensions.json()["dimensions_mm"] == {"x": 12.0, "y": 14.0, "z": 16.0}
    assert direct_dimensions.json()["analysis"]["metrics"]["dimensions_mm"] == {
        "x": 12.0, "y": 14.0, "z": 16.0
    }

    locked_mismatch = client.post(
        endpoint,
        headers=fresh_headers,
        json={"scale_percent": {"x": 150, "y": 100, "z": 100}, "preserve_aspect_ratio": True},
    )
    assert locked_mismatch.status_code == 422

    reset = client.post(endpoint, headers=fresh_headers, json={"reset": True})
    assert reset.status_code == 200, reset.text
    assert reset.json()["dimensions_mm"] == {"x": 20.0, "y": 20.0, "z": 20.0}
    assert reset.json()["version"]["transformed_blob_id"] is None


def test_meters_exported_model_can_be_recovered_to_a_printable_size(client, fresh_headers):
    """A model exported in meters must be rescalable, not rejected as absurd.

    The workbench's two recovery buttons both send a large percentage (1000x
    for the meters fix, and more for "fit inside printer"). Capping percentages
    at 100000 made the first exactly on the boundary and the second 422, which
    left a real model permanently unprintable.
    """
    data = (FIXTURES / "meters_20mm.stl").read_bytes()
    version_id = _upload(client, fresh_headers, "meters.stl", data).json()["version"]["id"]
    endpoint = f"/api/v1/models/versions/{version_id}/transform"

    analysed = client.post(
        f"/api/v1/models/versions/{version_id}/analyze", headers=fresh_headers
    ).json()
    assert analysed["metrics"]["dimensions_mm"] == {"x": 0.02, "y": 0.02, "z": 0.03}

    # The meters fix: x1000 turns the 0.02 mm numbers back into 20 mm.
    meters = client.post(
        endpoint,
        headers=fresh_headers,
        json={"scale_percent": {"x": 100000, "y": 100000, "z": 100000}, "preserve_aspect_ratio": True},
    )
    assert meters.status_code == 200, meters.text
    assert meters.json()["dimensions_mm"] == {"x": 20.0, "y": 20.0, "z": 30.0}

    # "Fit inside printer" on a 220x220x250 bed at 70%: the limiting axis is Z,
    # needing 250*0.7/0.03 = 5833.33x, i.e. 583333.33% -- far past the old cap.
    fit = client.post(
        endpoint,
        headers=fresh_headers,
        json={
            "scale_percent": {"x": 583333.3333, "y": 583333.3333, "z": 583333.3333},
            "preserve_aspect_ratio": True,
        },
    )
    assert fit.status_code == 200, fit.text
    dimensions = fit.json()["dimensions_mm"]
    assert dimensions == pytest.approx({"x": 116.6667, "y": 116.6667, "z": 175.0}, rel=1e-4)

    # And the recovered geometry is analysed as the new geometry, not the old.
    assert fit.json()["version"]["bbox_z"] == pytest.approx(175.0, rel=1e-4)


def test_transform_request_bounds_each_unit_by_what_that_unit_can_mean():
    """Percentages and millimetres are different units and get their own ceiling."""
    from app.schemas.api import ModelTransformRequest

    def build(**kwargs):
        return ModelTransformRequest(**kwargs)

    # A recovery percentage is accepted; a percentage past the ceiling is not.
    build(scale_percent={"x": 1_000_000, "y": 1_000_000, "z": 1_000_000})
    with pytest.raises(ValueError):
        build(scale_percent={"x": 2_000_000, "y": 2_000_000, "z": 2_000_000})

    # A millimetre target is capped in millimetres, which is far lower, so the
    # same number is valid as a percentage and invalid as a size.
    build(target_dimensions_mm={"x": 10_000, "y": 10_000, "z": 10_000})
    with pytest.raises(ValueError):
        build(target_dimensions_mm={"x": 20_000, "y": 20_000, "z": 20_000})

    # Degenerate and non-finite values stay rejected in both units.
    for bad in (0, -5, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            build(scale_percent={"x": bad, "y": 100, "z": 100})
        with pytest.raises(ValueError):
            build(target_dimensions_mm={"x": bad, "y": 1, "z": 1})


def test_slice_forwards_accepted_ai_settings_into_the_pipeline(
    client, fresh_headers, seeded, monkeypatch
):
    from app.api.routers import slicing as slicing_router

    profile = next(iter(seeded["profiles"].values()))
    material = next(iter(seeded["material_profiles"].values()))
    printer = client.post(
        "/api/v1/printers",
        headers=fresh_headers,
        json={
            "name": "AI settings test printer",
            "machine_profile_id": profile["id"],
            "nozzle_diameter_mm": float(profile["nozzle_diameters"][0]),
        },
    )
    assert printer.status_code == 201, printer.text
    version = _upload(
        client, fresh_headers, "cube.stl", (FIXTURES / "cube_20mm.stl").read_bytes()
    ).json()["version"]
    received: dict[str, object] = {}

    def capture_pipeline(_db, **kwargs):
        received.update(kwargs)

    monkeypatch.setattr(slicing_router, "run_pipeline", capture_pipeline)
    overrides = {
        "layer_height": 0.16,
        "wall_line_count": 5,
        "top_layer_count": 6,
        "bottom_layer_count": 5,
        "infill_sparse_density": 35.0,
        "infill_pattern": "grid",
        "wall_speed_mm_s": 60,
    }
    response = client.post(
        "/api/v1/slice",
        headers=fresh_headers,
        json={
            "model_version_id": version["id"],
            "printer_id": printer.json()["id"],
            "material_profile_id": material["id"],
            "objective": "balanced",
            "engine_settings_overrides": overrides,
            "locked_parameters": list(overrides),
        },
    )
    assert response.status_code == 201, response.text
    expected = {
        "layer_height_mm": 0.16,
        "wall_count": 5,
        "top_bottom_layers": 6,
        "infill_density_percent": 35.0,
        "infill_pattern": "grid",
        "outer_wall_speed_mm_s": 60,
    }
    assert received["overrides"] == expected
    assert received["locked"] == expected


def test_upload_and_analyse_the_thin_wall_model(client, fresh_headers):
    upload = _upload(client, fresh_headers, "thin.stl", (FIXTURES / "thin_wall.stl").read_bytes())
    version_id = upload.json()["version"]["id"]
    analysis = client.post(f"/api/v1/models/versions/{version_id}/analyze", headers=fresh_headers).json()

    codes = {f["code"] for f in analysis["findings"]}
    assert "THIN_WALL" in codes
    thin = next(f for f in analysis["findings"] if f["code"] == "THIN_WALL")
    assert thin["measured_value"] == pytest.approx(0.28, abs=0.03)
    assert analysis["risk_level"] == "high"


def test_analyse_a_broken_mesh(client, fresh_headers):
    upload = _upload(client, fresh_headers, "open.stl", (FIXTURES / "open_mesh.stl").read_bytes())
    version_id = upload.json()["version"]["id"]
    analysis = client.post(f"/api/v1/models/versions/{version_id}/analyze", headers=fresh_headers).json()
    assert analysis["metrics"]["is_watertight"] is False
    assert "MESH_NOT_WATERTIGHT" in {f["code"] for f in analysis["findings"]}


def test_identical_upload_is_deduplicated(client, fresh_headers):
    """Same bytes twice returns the same model version, writing nothing new."""
    data = (FIXTURES / "assembly.stl").read_bytes()
    first = _upload(client, fresh_headers, "a.stl", data)
    second = _upload(client, fresh_headers, "b.stl", data)
    assert first.status_code == 201
    assert first.json()["deduplicated"] is False

    assert second.status_code == 201
    assert second.json()["deduplicated"] is True
    assert second.json()["version"]["id"] == first.json()["version"]["id"]
    assert second.json()["model"]["id"] == first.json()["model"]["id"]

    # And the model list holds exactly one entry, not two.
    listed = client.get("/api/v1/models", headers=fresh_headers).json()
    assert len(listed) == 1


def test_upload_rejects_a_spoofed_extension(client, fresh_headers):
    response = _upload(client, fresh_headers, "evil.stl", b"MZ\x90\x00" * 500)
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_FORMAT"


def test_upload_does_not_let_a_filename_escape(client, fresh_headers):
    data = (FIXTURES / "tiny_2mm.stl").read_bytes()
    response = _upload(client, fresh_headers, "../../../etc/cron.d/evil.stl", data)
    assert response.status_code == 201
    # The name is stored as data, and the sanitised form has no separators.
    stored = response.json()["version"]["original_filename"]
    assert stored == "../../../etc/cron.d/evil.stl"  # preserved verbatim for display
    download = client.get(
        f'/api/v1/models/versions/{response.json()["version"]["id"]}/file',
        headers=fresh_headers,
    )
    assert download.status_code == 200
    assert ".." not in download.headers["content-disposition"]


def test_download_returns_the_original_bytes(client, fresh_headers):
    data = (FIXTURES / "cube_20mm.stl").read_bytes()
    version_id = _upload(client, fresh_headers, "cube.stl", data).json()["version"]["id"]
    download = client.get(f"/api/v1/models/versions/{version_id}/file", headers=fresh_headers)
    assert download.status_code == 200
    assert download.content == data
    assert "X-Content-SHA256" in download.headers


def test_list_models_only_shows_your_own(client, fresh_headers, auth_headers):
    _upload(client, fresh_headers, "mine.stl", (FIXTURES / "cube_20mm.stl").read_bytes())
    mine = client.get("/api/v1/models", headers=fresh_headers).json()
    assert len(mine) == 1

    theirs = client.get("/api/v1/models", headers=auth_headers).json()
    assert theirs == []


def test_cross_tenant_access_is_not_found(client, fresh_headers, auth_headers):
    version_id = _upload(
        client, fresh_headers, "private.stl", (FIXTURES / "cube_20mm.stl").read_bytes()
    ).json()["version"]["id"]

    # A different account must not be able to read, download or analyse it.
    assert client.get(f"/api/v1/models/versions/{version_id}/file", headers=auth_headers).status_code == 404
    assert client.post(f"/api/v1/models/versions/{version_id}/analyze", headers=auth_headers).status_code == 404


def test_analysis_is_cached_by_content_hash(client, fresh_headers):
    version_id = _upload(
        client, fresh_headers, "cache.stl", (FIXTURES / "cube_20mm.stl").read_bytes()
    ).json()["version"]["id"]
    first = client.post(f"/api/v1/models/versions/{version_id}/analyze", headers=fresh_headers)
    second = client.post(f"/api/v1/models/versions/{version_id}/analyze", headers=fresh_headers)
    assert first.json() == second.json()


def test_format_matrix_is_public_and_honest(client, auth_headers):
    matrix = client.get("/api/v1/models/formats", headers=auth_headers).json()
    formats = {row["format"] for row in matrix}
    assert {"stl_binary", "obj", "three_mf", "ply", "glb"} <= formats
    # Nothing claims a writer that does not exist.
    for row in matrix:
        if row["write"]:
            assert row["format"] in {"stl_binary", "three_mf"}


def test_empty_upload_is_rejected(client, fresh_headers):
    response = _upload(client, fresh_headers, "empty.stl", b"")
    assert response.status_code == 422
