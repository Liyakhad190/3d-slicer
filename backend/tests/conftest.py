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
