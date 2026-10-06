"""Shared pytest fixtures.

Every test runs against a throwaway SQLite database and a throwaway blob
directory, so tests never touch the developer's real data directory and can
run in parallel with a live dev server.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(scope="session")
def test_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("slicing-platform-test-data")


@pytest.fixture(scope="session")
def test_client(test_data_dir: Path) -> Iterator:
    """A TestClient over the real app, wired to an isolated database."""
    from fastapi.testclient import TestClient

    db_path = test_data_dir / "test.db"
    os.environ.update(
        ENVIRONMENT="test",
        DEBUG="false",
        DATA_DIR=str(test_data_dir),
        DATABASE_URL=f"sqlite+pysqlite:///{db_path.as_posix()}",
        AUTH_SECRET="test-secret-that-is-definitely-long-enough-1234",
        RATE_LIMIT_ENABLED="false",
        LOG_LEVEL="WARNING",
        LOG_JSON="false",
    )

    from app.core.config import reset_settings_cache

    reset_settings_cache()

    from app.db.session import dispose_engine
    from app.main import create_app

    dispose_engine()

    with TestClient(create_app()) as client:
        yield client

    dispose_engine()
    reset_settings_cache()


@pytest.fixture(scope="session")
def client(test_client):
    """Session-scoped alias so module-scoped fixtures can depend on it."""
    return test_client


@pytest.fixture(scope="session")
def auth_headers(test_client) -> dict[str, str]:
    """One registered account, shared by tests that only read."""
    email = f"test-{uuid.uuid4().hex[:8]}@example.com"
    response = test_client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Test User"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def fresh_headers(test_client) -> dict[str, str]:
    """A brand-new account, for tests that need an empty tenant."""
    email = f"fresh-{uuid.uuid4().hex[:8]}@example.com"
    response = test_client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Fresh User"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(scope="session")
def seeded(test_client) -> dict[str, dict]:
    """Catalog ids shared across the test session."""
    headers = _session_headers(test_client)
    machines = test_client.get("/api/v1/machines", headers=headers).json()
    profiles = test_client.get("/api/v1/machine-profiles", headers=headers).json()
    materials = test_client.get("/api/v1/materials", headers=headers).json()
    material_profiles = test_client.get("/api/v1/material-profiles", headers=headers).json()
    nozzles = test_client.get("/api/v1/nozzles", headers=headers).json()
    return {
        "machines": {m["model"]: m for m in machines},
        "profiles": {p["id"]: p for p in profiles},
        "materials": {m["key"]: m for m in materials},
        "material_profiles": {p["id"]: p for p in material_profiles},
        "nozzles": {f'{n["diameter_mm"]}-{n["material"]}': n for n in nozzles},
    }


_HEADERS_CACHE: dict[str, str] = {}


def _session_headers(test_client) -> dict[str, str]:
    if "h" not in _HEADERS_CACHE:
        email = f"session-{uuid.uuid4().hex[:8]}@example.com"
        response = test_client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": PASSWORD, "display_name": "Session"},
        )
        assert response.status_code == 201, response.text
        _HEADERS_CACHE["h"] = f"Bearer {response.json()['access_token']}"
    return {"Authorization": _HEADERS_CACHE["h"]}


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()

# ---------------------------------------------------------------------------
# Shared preparation workspace helpers (used across multiple test modules)
# ---------------------------------------------------------------------------

import pytest as _pytest
from types import SimpleNamespace
from pathlib import Path

_FIXTURES_PATH = Path(__file__).parent / "fixtures"


def _material_profile_id(seeded):
    return next(
        p["id"]
        for p in seeded["material_profiles"].values()
        if p["brand_sku"] == "pla-generic"
    )


def _workspace_on(client, headers, seeded, machine_profile_id):
    material_id = _material_profile_id(seeded)
    printer = client.post(
        "/api/v1/printers",
        headers=headers,
        json={
            "name": "Prep Printer",
            "machine_profile_id": machine_profile_id,
            "nozzle_diameter_mm": 0.4,
            "nozzle_material": "brass",
        },
    )
    assert printer.status_code == 201, printer.text
    upload = client.post(
        "/api/v1/models",
        headers=headers,
        files={
            "file": (
                "cube.stl",
                (_FIXTURES_PATH / "cube_20mm.stl").read_bytes(),
                "application/octet-stream",
            )
        },
    )
    assert upload.status_code == 201, upload.text
    return SimpleNamespace(
        headers=headers,
        printer_id=printer.json()["id"],
        version_id=upload.json()["version"]["id"],
        material_profile_id=material_id,
        machine_profile_id=machine_profile_id,
    )


@_pytest.fixture
def workspace(client, fresh_headers, seeded):
    profile_id = next(iter(seeded["profiles"]))
    return _workspace_on(client, fresh_headers, seeded, profile_id)


@_pytest.fixture
def provider(monkeypatch):
    from app.api.routers import preparation as preparation_router
    from tests.job_factory import ScriptedProvider

    scripted = ScriptedProvider(payloads=[{}, {}, {}])
    monkeypatch.setattr(
        preparation_router, "get_llm_provider", lambda settings=None: scripted
    )
    return scripted
