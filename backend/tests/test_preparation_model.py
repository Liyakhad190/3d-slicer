"""Persistence tests for the AI print preparation job.

These cover only the storage layer: the table exists, its shape matches the
model, its constraints actually constrain, and a row survives a write/read
round trip. The preparation logic itself is tested elsewhere.
"""

from __future__ import annotations

import io
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.db.base import Base, JSONType
from app.db.schema import import_all_models
from app.db.session import session_scope
from app.models import PreparationStatus, PrintPreparationJob
from app.models.catalog import Printer

TABLE = "print_preparation_jobs"

REQUIRED_COLUMNS = {
    "id",
    "user_id",
    "model_version_id",
    "printer_id",
    "material_profile_id",
    "attempt",
    "status",
    "prompt",
    "raw_response",
    "plan",
    "validation",
    "normalisations",
    "rejected_reason",
    "input_sha256",
    "model_identifier",
    "provider",
    "duration_ms",
    "created_at",
    "updated_at",
}


def _cube() -> io.BytesIO:
    data = (Path(__file__).parent / "fixtures" / "cube_20mm.stl").read_bytes()
    return io.BytesIO(data)


def test_registered_in_the_model_registry() -> None:
    import_all_models()
    import app.models as models

    assert TABLE in Base.metadata.tables
    assert "PrintPreparationJob" in models.__all__
    assert "PreparationStatus" in models.__all__


def test_status_vocabulary_is_fixed() -> None:
    assert [s.value for s in PreparationStatus] == [
        "queued",
        "analysing",
        "proposing",
        "validating",
        "completed",
        "rejected",
        "failed",
    ]


class TestSchemaShape:
    """The table the migration produces must match the model exactly."""

    @pytest.fixture(scope="class")
    def table(self):
        import_all_models()
        return Base.metadata.tables[TABLE]

    def test_every_documented_column_exists(self, table) -> None:
        assert {c.name for c in table.columns} == REQUIRED_COLUMNS

    def test_columns_are_not_null_where_a_plan_needs_them(self, table) -> None:
        by_name = {c.name: c for c in table.columns}
        for name in (
            "prompt",
            "plan",
            "validation",
            "normalisations",
            "input_sha256",
            "attempt",
            "duration_ms",
        ):
            assert not by_name[name].nullable, name
        for name in ("raw_response", "rejected_reason", "model_identifier", "provider"):
            assert by_name[name].nullable, name

    def test_json_columns_use_the_shared_json_type(self, table) -> None:
        by_name = {c.name: c for c in table.columns}
        for name in ("plan", "validation", "normalisations"):
            assert isinstance(by_name[name].type, JSONType), name

    def test_primary_key_is_the_uuid(self, table) -> None:
        assert [c.name for c in table.primary_key] == ["id"]

    def test_foreign_keys_point_at_the_existing_tables(self, table) -> None:
        actual = {
            fk.parent.name: (fk.column.table.name, fk.ondelete)
            for fk in table.foreign_keys
        }
        assert actual == {
            "user_id": ("users", "CASCADE"),
            "model_version_id": ("model_versions", "CASCADE"),
            "printer_id": ("printers", "RESTRICT"),
            "material_profile_id": ("material_profiles", "RESTRICT"),
        }

    def test_indexes(self, table) -> None:
        # Single-column indexes come from `index=True`; the three below are the
        # composite/lookup ones the read paths depend on.
        assert {
            "ix_preparation_job_user",
            "ix_preparation_job_version",
            "ix_preparation_job_sha",
        } <= {i.name for i in table.indexes}


class TestMigratedDatabase:
    """The database the app actually opened, built on startup."""

    @pytest.fixture(scope="class")
    def inspector(self, test_client):
        from app.db.session import get_engine

        return inspect(get_engine())

    def test_table_exists(self, inspector) -> None:
        assert TABLE in inspector.get_table_names()

    def test_columns_match_the_model(self, inspector) -> None:
        names = {c["name"] for c in inspector.get_columns(TABLE)}
        assert names == REQUIRED_COLUMNS

    def test_indexes_exist(self, inspector) -> None:
        names = {i["name"] for i in inspector.get_indexes(TABLE)}
        assert {
            "ix_preparation_job_user",
            "ix_preparation_job_version",
            "ix_preparation_job_sha",
            "ix_print_preparation_jobs_status",
            "ix_print_preparation_jobs_created_at",
        } <= names

    def test_foreign_keys_are_declared(self, inspector) -> None:
        fks = {
            f["constrained_columns"][0]: (
                f["referred_table"],
                f["options"].get("ondelete"),
            )
            for f in inspector.get_foreign_keys(TABLE)
        }
        assert fks == {
            "user_id": ("users", "CASCADE"),
            "model_version_id": ("model_versions", "CASCADE"),
            "printer_id": ("printers", "RESTRICT"),
            "material_profile_id": ("material_profiles", "RESTRICT"),
        }

    def test_foreign_keys_are_enforced(self, test_client) -> None:
        with session_scope() as session:
            session.add(
                PrintPreparationJob(
                    user_id=uuid.uuid4(),
                    model_version_id=uuid.uuid4(),
                    printer_id=uuid.uuid4(),
                    material_profile_id=uuid.uuid4(),
                    prompt="p",
                    input_sha256="0" * 64,
                )
            )
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()


class TestRoundTrip:
    """Write a row against real foreign keys and read it back unchanged."""

    def test_insert_read_delete(self, test_client, seeded, fresh_headers) -> None:
        upload = test_client.post(
            "/api/v1/models",
            headers=fresh_headers,
            files={"file": ("cube.stl", _cube(), "application/octet-stream")},
        )
        assert upload.status_code == 201, upload.text
        version_id = uuid.UUID(upload.json()["version"]["id"])

        printer_resp = test_client.post(
            "/api/v1/printers",
            headers=fresh_headers,
            json={
                "name": "preparation-job-printer",
                "machine_profile_id": next(iter(seeded["profiles"])),
                "nozzle_diameter_mm": str(Decimal("0.4")),
            },
        )
        assert printer_resp.status_code in (200, 201), printer_resp.text
        printer_id = uuid.UUID(printer_resp.json()["id"])
        material_profile_id = uuid.UUID(next(iter(seeded["material_profiles"])))

        with session_scope() as session:
            printer = session.get(Printer, printer_id)
            assert printer is not None
            job = PrintPreparationJob(
                user_id=printer.user_id,
                model_version_id=version_id,
                printer_id=printer_id,
                material_profile_id=material_profile_id,
                attempt=1,
                status=PreparationStatus.completed,
                prompt="system + user prompt",
                raw_response='{"orientation": {"candidate_id": "rot_+0_+0_+0"}}',
                plan={"orientation": {"candidate_id": "rot_+0_+0_+0"}},
                validation={"passed": True, "summary": {"failed": 0}},
                normalisations=[{"code": "AI_SPEED_ABOVE_MACHINE_LIMIT"}],
                input_sha256="a" * 64,
                model_identifier="qwen2.5:1.5b",
                provider="ollama",
                duration_ms=1234,
            )
            session.add(job)
            session.flush()
            job_id = job.id
            user_id = printer.user_id

            reloaded = session.get(PrintPreparationJob, job_id)
            assert reloaded is not None
            assert reloaded.plan["orientation"]["candidate_id"] == "rot_+0_+0_+0"
            assert reloaded.validation["passed"] is True
            assert reloaded.normalisations[0]["code"] == (
                "AI_SPEED_ABOVE_MACHINE_LIMIT"
            )
            assert reloaded.status is PreparationStatus.completed
            assert reloaded.passed is True
            assert reloaded.attempt == 1
            assert reloaded.provider == "ollama"
            assert reloaded.created_at is not None
            assert reloaded.updated_at is not None

            # The per-user history query this backs must be well-formed.
            history = session.execute(
                select(PrintPreparationJob).where(
                    PrintPreparationJob.user_id == user_id
                )
            ).scalars().all()
            assert any(r.id == job_id for r in history)

            # A rejected attempt never reports as passing, whatever it claims.
            reloaded.status = PreparationStatus.rejected
            session.flush()
            assert reloaded.passed is False

            session.delete(reloaded)

        with session_scope() as session:
            assert session.get(PrintPreparationJob, job_id) is None
