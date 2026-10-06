"""The preparation API: authentication, ownership, formats, and the guards.

Every rule is asserted through HTTP - the surface a client actually uses,
because these guarantees only count if they hold at the boundary:

* a refused plan cannot reach a slicer (409 `PREPARATION_REJECTED`);
* a plan that no longer validates is re-checked before slicing, with no token
  spent on the way;
* a format the printer never declared is refused with 409 before the model
  runs, and one this deployment cannot produce with 503 - never renamed bytes;
* a failed attempt still leaves a row behind, and its id comes back with the
  error that reports it;
* host paths never cross the response boundary.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import session_scope  # noqa: E402
from app.intelligence.providers.base import LLMError  # noqa: E402
from app.models.catalog import (  # noqa: E402
    Machine,
    MachineProfile,
    MaterialProfile,
    ValidationStatus,
)
from app.models.preparation import PreparationStatus, PrintPreparationJob  # noqa: E402
from app.models.slicing import GeneratedFile  # noqa: E402
from app.preparation.planner import MAX_ATTEMPTS  # noqa: E402
from tests.job_factory import ScriptedProvider  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

#: Phrases that would put this machine's layout on the wire.
HOST_PATH_LEAKS = ("C:\\", "Program Files", "/home/", "/Users/", "\\Users\\")


# --------------------------------------------------------------------------- #
# fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def engine_available() -> None:
    from app.slicing.registry import get_registry

    if not get_registry().available():
        pytest.skip("no slicing engine is available")


# provider fixture is defined in conftest.py (shared)


# _material_profile_id, _workspace_on, and workspace fixture now live in
# conftest.py so they are shared with test_preparation_phase3 and other
# modules.  We import the helpers here for the extended helpers below.
from tests.conftest import _material_profile_id, _workspace_on  # noqa: E402


def _profile_for(machine_model: str, *, validated: bool) -> str:
    """The machine's newest profile, optionally promoted like an operator would."""
    from app.db.base import utcnow

    with session_scope() as db:
        machine = db.query(Machine).filter(Machine.model == machine_model).one()
        profile = (
            db.query(MachineProfile)
            .filter(MachineProfile.machine_id == machine.id)
            .order_by(MachineProfile.profile_version.desc())
            .first()
        )
        if validated:
            profile.validation_status = ValidationStatus.validated
            profile.validated_at = utcnow()
            profile.validated_by = "preparation-api-test"
            db.flush()
        return str(profile.id)


def _create(ws: SimpleNamespace, client, **extra: Any):
    body = {
        "model_version_id": ws.version_id,
        "printer_id": ws.printer_id,
        "material_profile_id": ws.material_profile_id,
        "user_request": "print this",
        **extra,
    }
    return client.post("/api/v1/preparation", headers=ws.headers, json=body)


def _slice(client, headers, job_id: str, **payload: Any):
    return client.post(
        f"/api/v1/preparation/{job_id}/slice", headers=headers, json=payload
    )


@contextmanager
def _declares(machine_profile_id: str, formats: list[str]):
    """Teach one profile a format declaration, then put it back."""
    with session_scope() as db:
        profile = db.get(MachineProfile, uuid.UUID(machine_profile_id))
        original = dict(profile.machine_restrictions or {})
        profile.machine_restrictions = {**original, "output_formats": formats}
        db.flush()
    try:
        yield
    finally:
        with session_scope() as db:
            profile = db.get(MachineProfile, uuid.UUID(machine_profile_id))
            profile.machine_restrictions = original
            db.flush()


def _set(
    job_id: str,
    *,
    status: PreparationStatus | None = None,
    validation: dict[str, Any] | None = None,
    rejected_reason: str | None = None,
    orientation: str | None = None,
) -> None:
    """Edit an attempt's stored verdict the way a second review would.

    Only the *stored* record changes: that is the point of the two tests that
    use it, because the API must tell a stored verdict from a re-measured one.
    """
    with session_scope() as db:
        job = db.get(PrintPreparationJob, uuid.UUID(job_id))
        if status is not None:
            job.status = status
        if validation is not None:
            job.validation = validation
        if rejected_reason is not None:
            job.rejected_reason = rejected_reason
        if orientation is not None:
            plan = dict(job.plan or {})
            pose = dict(plan.get("orientation") or {})
            pose["candidate_id"] = orientation
            plan["orientation"] = pose
            job.plan = plan
        db.flush()


# --------------------------------------------------------------------------- #
# creating and reading attempts
# --------------------------------------------------------------------------- #


def test_preparation_requires_a_bearer_token(client, workspace):
    response = client.post(
        "/api/v1/preparation",
        json={
            "model_version_id": workspace.version_id,
            "printer_id": workspace.printer_id,
            "material_profile_id": workspace.material_profile_id,
        },
    )
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] in {"AUTHENTICATION_ERROR", "UNAUTHENTICATED"}


def test_create_returns_the_attempt_with_its_provenance(client, workspace, provider):
    response = _create(workspace, client, output_format="gcode")
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["status"] == "completed"
    assert body["passed"] is True
    assert body["attempt"] == 1
    assert body["max_attempts"] == MAX_ATTEMPTS
    # Who answered, with what, and what it said - verbatim.
    assert body["provider"] == "scripted"
    assert body["model_identifier"] == "scripted-1b"
    assert body["prompt"] and body["prompt_sha256"]
    assert body["raw_response"] == "{}"
    assert body["plan"]
    assert body["validation"]["summary"]["failed"] == 0
    assert body["duration_ms"] >= 0

    # And what it was planned against, by name rather than by id alone.
    assert body["model"]["filename"] == "cube.stl"
    assert body["printer"]["name"] == "Prep Printer"
    assert body["material"]["brand_sku"] == "pla-generic"

    assert body["slice"] is None
    assert body["outputs"] == []
    gcode = next(f for f in body["output_formats"] if f["key"] == "gcode")
    assert gcode["declared_by_profile"] and gcode["available"] and gcode["advertised"]

    # A read of the same attempt says exactly what the write did.
    again = client.get(f"/api/v1/preparation/{body['id']}", headers=workspace.headers)
    assert again.status_code == 200
    assert again.json()["id"] == body["id"]
    assert again.json()["prompt_sha256"] == body["prompt_sha256"]
    assert again.json()["plan"] == body["plan"]


def test_an_attempt_is_readable_only_by_its_owner(
    client, workspace, provider, auth_headers
):
    job_id = _create(workspace, client).json()["id"]

    assert (
        client.get(f"/api/v1/preparation/{job_id}", headers=workspace.headers).status_code
        == 200
    )
    stranger = client.get(f"/api/v1/preparation/{job_id}", headers=auth_headers)
    assert stranger.status_code == 404
    assert stranger.json()["error"]["code"] == "NOT_FOUND"

    assert _slice(client, auth_headers, job_id).status_code == 404
    assert (
        client.get(
            f"/api/v1/preparation/{job_id}/outputs", headers=auth_headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/preparation/{job_id}/validate", headers=auth_headers
        ).status_code
        == 404
    )
    theirs = client.get("/api/v1/preparation", headers=auth_headers).json()
    assert job_id not in {row["id"] for row in theirs}


def test_a_malformed_or_unknown_job_id_is_refused(client, workspace, provider):
    malformed = client.get("/api/v1/preparation/not-a-uuid", headers=workspace.headers)
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"

    unknown = client.get(f"/api/v1/preparation/{uuid.uuid4()}", headers=workspace.headers)
    assert unknown.status_code == 404


def test_the_list_is_scoped_and_bounded(client, workspace, provider, auth_headers):
    job_id = _create(workspace, client).json()["id"]

    listed = client.get("/api/v1/preparation", headers=workspace.headers)
    assert listed.status_code == 200
    rows = listed.json()
    assert [row["id"] for row in rows] == [job_id]
    # A summary row: identity and verdict, not every prompt ever written.
    assert "prompt" not in rows[0] and "plan" not in rows[0]
    assert rows[0]["status"] == "completed"

    assert (
        client.get("/api/v1/preparation?limit=0", headers=workspace.headers).status_code
        == 422
    )
    assert (
        client.get("/api/v1/preparation?limit=201", headers=workspace.headers).status_code
        == 422
    )
    filtered = client.get(
        f"/api/v1/preparation?model_version_id={uuid.uuid4()}",
        headers=workspace.headers,
    )
    assert filtered.json() == []


# --------------------------------------------------------------------------- #
# the guards in front of the slicer
# --------------------------------------------------------------------------- #


def test_a_rejected_attempt_cannot_be_sliced(client, workspace, provider):
    job_id = _create(workspace, client).json()["id"]
    _set(
        job_id,
        status=PreparationStatus.rejected,
        validation={
            "passed": False,
            "can_replan": True,
            "checks": [
                {
                    "category": "orientation",
                    "code": "ORIENTATION_UNKNOWN",
                    "status": "FAILED",
                    "reason": "pose-that-never-existed is not one of the measured poses",
                }
            ],
        },
        rejected_reason="the chosen orientation is not one the analysis measured",
        orientation="pose-that-never-existed",
    )

    read = client.get(f"/api/v1/preparation/{job_id}", headers=workspace.headers).json()
    assert read["status"] == "rejected"
    assert read["passed"] is False
    assert read["rejected_reason"]

    refused = _slice(client, workspace.headers, job_id)
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "PREPARATION_REJECTED"
    assert "ORIENTATION_UNKNOWN" in error["details"]["failed_checks"]
    assert error["details"]["can_replan"] is True

    # No slice job, no files, and the stored verdict is still the stored one.
    assert (
        client.get(
            f"/api/v1/preparation/{job_id}/outputs", headers=workspace.headers
        ).json()
        == []
    )
    check = client.post(
        f"/api/v1/preparation/{job_id}/validate", headers=workspace.headers
    )
    assert check.status_code == 200
    assert check.json()["recheck"]["passed"] is False
    assert check.json()["stored"]["passed"] is False


def test_a_plan_that_no_longer_validates_is_not_sliced(client, workspace, provider):
    """Stored says pass, measurement says no - the measurement wins."""
    job_id = _create(workspace, client).json()["id"]
    _set(job_id, orientation="pose-that-never-existed")

    stored = client.get(f"/api/v1/preparation/{job_id}", headers=workspace.headers).json()
    assert stored["status"] == "completed"
    assert stored["passed"] is True, "the stored verdict must be untouched"

    refused = _slice(client, workspace.headers, job_id)
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "PREPARATION_REJECTED"
    codes = {c["code"] for c in error["details"]["failed_checks"]}
    assert "ORIENTATION_UNKNOWN" in codes

    # Nothing was produced and no model was asked to fix it.
    assert (
        client.get(
            f"/api/v1/preparation/{job_id}/outputs", headers=workspace.headers
        ).json()
        == []
    )


def test_scaling_invalidates_preparation_and_stale_artifact_download(client, workspace, provider):
    from app.models.slicing import SliceJob
    from tests.job_factory import build_sliced_job

    created = _create(workspace, client)
    assert created.status_code == 201, created.text
    preparation_id = created.json()["id"]

    with session_scope() as db:
        legacy = build_sliced_job(db, user_id=uuid.UUID(client.get(
            "/api/v1/auth/me", headers=workspace.headers
        ).json()["id"]))
        slice_job = db.get(SliceJob, legacy["job_id"])
        slice_job.model_version_id = uuid.UUID(workspace.version_id)
        slice_job.preparation_job_id = uuid.UUID(preparation_id)
        slice_job.model_input_sha256 = legacy["input_sha256"]
        stale_file_id = str(legacy["generated_id"])

    scaled = client.post(
        f"/api/v1/models/versions/{workspace.version_id}/transform",
        headers=workspace.headers,
        json={"scale_percent": {"x": 125, "y": 125, "z": 125}, "preserve_aspect_ratio": True},
    )
    assert scaled.status_code == 200, scaled.text
    assert client.get(
        f"/api/v1/preparation/{preparation_id}/outputs", headers=workspace.headers
    ).json() == []

    refused_slice = _slice(client, workspace.headers, preparation_id)
    assert refused_slice.status_code == 409
    assert refused_slice.json()["error"]["code"] == "PREPARATION_STALE"

    refused_download = client.get(
        f"/api/v1/preparation/{preparation_id}/outputs/{stale_file_id}/download",
        headers=workspace.headers,
    )
    assert refused_download.status_code == 409
    assert refused_download.json()["error"]["code"] == "PREPARATION_STALE"


def test_a_provider_failure_is_reported_and_keeps_its_attempt(
    client, workspace, monkeypatch
):
    from app.api.routers import preparation as preparation_router

    failing = ScriptedProvider(
        errors=[
            LLMError("LLM_UNAVAILABLE", "no model is reachable", recoverable=True)
            for _ in range(MAX_ATTEMPTS)
        ]
    )
    monkeypatch.setattr(
        preparation_router, "get_llm_provider", lambda settings=None: failing
    )

    response = _create(workspace, client)
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "LLM_UNAVAILABLE"
    assert "ollama" in error["details"]["hint"].lower()
    job_id = error["details"]["job_id"]

    # The attempt survived the error that reported it: `job_id` resolves.
    stored = client.get(f"/api/v1/preparation/{job_id}", headers=workspace.headers)
    assert stored.status_code == 200
    assert stored.json()["status"] == "failed"
    assert stored.json()["raw_response"] is None
    assert stored.json()["passed"] is False

    # Nothing to slice, and nothing to validate either.
    assert _slice(client, workspace.headers, job_id).status_code == 409
    check = client.post(
        f"/api/v1/preparation/{job_id}/validate", headers=workspace.headers
    )
    assert check.status_code == 409
    assert check.json()["error"]["code"] == "PREPARATION_NOT_READY"


def test_an_unvalidated_machine_profile_cannot_be_sliced(
    client, fresh_headers, seeded, provider
):
    """No profile, no output - enforced through this route too."""
    profile_id = _profile_for("MK4S", validated=False)
    ws = _workspace_on(client, fresh_headers, seeded, profile_id)
    job_id = _create(ws, client).json()["id"]

    refused = _slice(client, ws.headers, job_id)
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "INSUFFICIENT_INFORMATION"
    assert "not been validated" in error["message"]
    assert client.get(
        f"/api/v1/preparation/{job_id}/outputs", headers=ws.headers
    ).json() == []


# --------------------------------------------------------------------------- #
# output formats
# --------------------------------------------------------------------------- #


def test_an_undeclared_output_format_fails_before_the_model_runs(
    client, workspace, provider
):
    response = _create(workspace, client, output_format="gx")
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "OUTPUT_FORMAT_UNSUPPORTED"
    assert error["details"]["declared_by_profile"] == ["gcode"]
    # Checked before a single token was spent, and no attempt was written.
    assert provider.requests == []
    assert client.get("/api/v1/preparation", headers=workspace.headers).json() == []


def test_declared_but_unproducible_gx_is_503_with_actionable_detail(
    client, workspace, provider
):
    with _declares(workspace.machine_profile_id, ["gcode", "gx"]):
        response = _create(workspace, client, output_format="gx")

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "OUTPUT_FORMAT_UNAVAILABLE"
    assert "GPX" in error["message"] or "encoder" in error["message"]
    # Actionable: where to get the encoder. Not diagnostic: no host paths.
    assert error["details"]["install"]
    for leak in (*HOST_PATH_LEAKS, '"searched"', '"executable"', '"flashprint_engine"'):
        assert leak not in response.text, leak
    assert provider.requests == []


def test_output_formats_reflect_the_printer_not_the_platform(
    client, workspace, provider
):
    gcode_only = {f["key"]: f for f in _create(workspace, client).json()["output_formats"]}
    assert set(gcode_only) == {"gcode", "gx"}
    assert gcode_only["gcode"]["declared_by_profile"] is True
    assert gcode_only["gx"]["declared_by_profile"] is False
    assert gcode_only["gx"]["advertised"] is False

    with _declares(workspace.machine_profile_id, ["gcode", "gx"]):
        both = {f["key"]: f for f in _create(workspace, client).json()["output_formats"]}
    # Visible because the profile declares it, unavailable because no encoder
    # is installed - reported rather than hidden.
    assert both["gx"]["declared_by_profile"] is True
    assert both["gx"]["available"] is False
    assert both["gx"]["advertised"] is False
    assert both["gx"]["reason"] and both["gx"]["remedy"]


def test_gx_request_is_refused_before_any_slice_job_exists(
    client, workspace, provider
):
    with _declares(workspace.machine_profile_id, ["gcode", "gx"]):
        job_id = _create(workspace, client).json()["id"]
        refused = _slice(client, workspace.headers, job_id, output_format="gx")

    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == "OUTPUT_FORMAT_UNAVAILABLE"

    read = client.get(f"/api/v1/preparation/{job_id}", headers=workspace.headers).json()
    assert read["slice"] is None, "the format gate must run before slicing"
    assert read["outputs"] == []


def test_no_host_paths_cross_the_boundary(client, workspace, provider):
    with _declares(workspace.machine_profile_id, ["gcode", "gx"]):
        job_id = _create(workspace, client, output_format="gcode").json()["id"]
        detail = client.get(f"/api/v1/preparation/{job_id}", headers=workspace.headers)
        listing = client.get("/api/v1/preparation", headers=workspace.headers)
        unavailable = _create(workspace, client, output_format="gx")

    assert detail.status_code == 200, detail.text
    assert listing.status_code == 200, listing.text
    assert unavailable.status_code == 503, unavailable.text
    for response in (detail, listing, unavailable):
        for leak in HOST_PATH_LEAKS:
            assert leak not in response.text, f"{leak!r} leaked in {response.text[:400]}"


# --------------------------------------------------------------------------- #
# re-check and re-plan
# --------------------------------------------------------------------------- #


def test_validate_rechecks_without_spending_a_token(client, workspace, provider):
    job_id = _create(workspace, client).json()["id"]
    before = len(provider.requests)

    response = client.post(
        f"/api/v1/preparation/{job_id}/validate", headers=workspace.headers
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(provider.requests) == before, "re-validation must not spend a token"
    assert body["stored"]["passed"] is True
    assert body["recheck"]["passed"] is True
    assert len(body["recheck"]["checks"]) >= 20
    # Nothing is persisted: the stored verdict stays the one that was recorded.
    assert client.get(
        f"/api/v1/preparation/{job_id}", headers=workspace.headers
    ).json()["validation"] == body["stored"]


def test_replan_walks_the_chain_up_to_the_cap(client, workspace, provider):
    first = _create(workspace, client).json()
    assert first["attempt"] == 1

    second = client.post(
        f"/api/v1/preparation/{first['id']}/replan",
        headers=workspace.headers,
        json={"user_request": "stronger", "feedback": "use more walls"},
    )
    assert second.status_code == 201, second.text
    assert second.json()["attempt"] == 2
    assert "use more walls" in second.json()["prompt"]
    assert second.json()["id"] != first["id"], "attempts are never rewritten"

    third = client.post(
        f"/api/v1/preparation/{second.json()['id']}/replan",
        headers=workspace.headers,
        json={"user_request": "stronger"},
    )
    assert third.status_code == 201
    assert third.json()["attempt"] == 3

    capped = client.post(
        f"/api/v1/preparation/{third.json()['id']}/replan",
        headers=workspace.headers,
        json={"user_request": "one more go"},
    )
    assert capped.status_code == 409
    assert capped.json()["error"]["details"]["max_attempts"] == MAX_ATTEMPTS

    listed = client.get("/api/v1/preparation", headers=workspace.headers).json()
    assert len(listed) == 3


def test_no_outputs_exist_before_a_slice(client, workspace, provider):
    job_id = _create(workspace, client).json()["id"]
    assert (
        client.get(f"/api/v1/preparation/{job_id}/outputs", headers=workspace.headers).json()
        == []
    )
    missing = client.get(
        f"/api/v1/preparation/{job_id}/outputs/{uuid.uuid4()}/download",
        headers=workspace.headers,
    )
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# slicing: the real engine, the real file
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_slice_produces_a_validated_gcode_artifact(
    client, fresh_headers, seeded, provider, engine_available, auth_headers
):
    profile_id = _profile_for("Ender 3", validated=True)
    ws = _workspace_on(client, fresh_headers, seeded, profile_id)
    job_id = _create(ws, client, output_format="gcode").json()["id"]

    sliced = _slice(client, ws.headers, job_id, objective="balanced")
    assert sliced.status_code == 201, sliced.text
    body = sliced.json()

    assert body["slice"]["status"] == "COMPLETED"
    assert body["slice"]["input_fingerprint"]
    assert body["slice"]["error_code"] is None

    outputs = body["outputs"]
    assert [o["format"] for o in outputs] == ["gcode"]
    artifact = outputs[0]
    assert artifact["checksum"].startswith("sha256:")
    assert artifact["validation"]["status"] == "passed"
    assert artifact["bytes"] > 10_000
    assert artifact["slicer"] and artifact["configuration_fingerprint"]

    listed = client.get(
        f"/api/v1/preparation/{job_id}/outputs", headers=ws.headers
    ).json()
    assert [o["id"] for o in listed] == [artifact["id"]]

    download = client.get(
        f"/api/v1/preparation/{job_id}/outputs/{artifact['id']}/download",
        headers=ws.headers,
    )
    assert download.status_code == 200, download.text
    assert download.headers["X-Output-Format"] == "gcode"
    assert download.headers["X-Content-SHA256"] == artifact["checksum"].split(":", 1)[1]
    assert download.headers["Content-Disposition"].startswith("attachment;")
    gcode = download.content.decode("utf-8", errors="replace")
    assert ";FLAVOR" in gcode and "M104" in gcode

    # The slice job is the same one the existing endpoint serves.
    slice_job_id = body["slice"]["slice_job_id"]
    assert (
        client.get(f"/api/v1/slice/{slice_job_id}", headers=ws.headers).status_code == 200
    )

    # And none of it is visible to anyone else.
    assert (
        client.get(
            f"/api/v1/preparation/{job_id}/outputs", headers=auth_headers
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/preparation/{job_id}/outputs/{artifact['id']}/download",
            headers=auth_headers,
        ).status_code
        == 404
    )


@pytest.mark.slow
def test_gx_is_produced_when_an_encoder_exists(
    client, fresh_headers, seeded, provider, engine_available
):
    """The real FlashForge deliverable, through the same route as G-code."""
    from app.slicing.output import gx as gx_module

    profile_id = _profile_for("Ender 3", validated=True)
    ws = _workspace_on(client, fresh_headers, seeded, profile_id)

    registry = gx_module.get_encoder_registry()
    registry.register(gx_module.RecordingEncoder())
    try:
        with _declares(ws.machine_profile_id, ["gcode", "gx"]):
            job_id = _create(ws, client, output_format="gx").json()["id"]
            sliced = _slice(client, ws.headers, job_id, output_format="gx")
            assert sliced.status_code == 201, sliced.text
            outputs = sliced.json()["outputs"]
            formats = {o["format"] for o in outputs}
            assert formats == {"gcode", "gx"}, outputs

            gx = next(o for o in outputs if o["format"] == "gx")
            assert gx["validation"]["status"] == "passed"
            assert gx["filename"].endswith(".gx")

            download = client.get(
                f"/api/v1/preparation/{job_id}/outputs/{gx['id']}/download",
                headers=ws.headers,
            )
            assert download.status_code == 200, download.text
            assert download.headers["X-Output-Format"] == "gx"
            # A real container, not a renamed text file.
            assert download.content.startswith(b"\x99\x00\x00\x00\x00")
            assert b"G21" not in download.content[:64]
    finally:
        registry._encoders.pop("recording", None)


@pytest.mark.slow
def test_a_blocked_file_is_neither_listed_nor_downloadable(
    client, fresh_headers, seeded, provider, engine_available
):
    profile_id = _profile_for("Ender 3", validated=True)
    ws = _workspace_on(client, fresh_headers, seeded, profile_id)
    job_id = _create(ws, client).json()["id"]

    sliced = _slice(client, ws.headers, job_id)
    assert sliced.status_code == 201, sliced.text
    slice_job_id = sliced.json()["slice"]["slice_job_id"]
    artifact_id = sliced.json()["outputs"][0]["id"]

    with session_scope() as db:
        generated = db.query(GeneratedFile).filter(
            GeneratedFile.id == uuid.UUID(artifact_id)
        ).one()
        generated.is_blocked = True
        generated.blocked_reason = "test: failed a mandatory safety validation"
        db.flush()

    assert (
        client.get(f"/api/v1/preparation/{job_id}/outputs", headers=ws.headers).json()
        == []
    )
    download = client.get(
        f"/api/v1/preparation/{job_id}/outputs/{artifact_id}/download",
        headers=ws.headers,
    )
    assert download.status_code == 409
    assert download.json()["error"]["code"] == "OUTPUT_NOT_VALIDATED"

    # The slice job still reports the file, marked, as /slice does.
    files = client.get(f"/api/v1/slice/{slice_job_id}/files", headers=ws.headers).json()
    assert files and files[0]["is_blocked"] is True
