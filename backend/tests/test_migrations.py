"""Migration tests.

The schema is only trustworthy if migrations actually work, so these run
`alembic upgrade head` against a throwaway database and assert the resulting
schema matches the ORM metadata. A drift between the two is the classic way a
project ends up with a database the code does not expect.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

BACKEND = Path(__file__).resolve().parents[1]


def _alembic_env(db_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    env["DATABASE_URL"] = f"sqlite+pysqlite:///{db_path.as_posix()}"
    return env


def _run_alembic(*args: str, db_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=_alembic_env(db_path),
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.fixture(scope="module")
def migrated_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_path = tmp_path_factory.mktemp("migrations") / "migrated.db"
    result = _run_alembic("upgrade", "head", db_path=db_path)
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}"
    return db_path


def test_upgrade_head_succeeds(migrated_db: Path):
    engine = create_engine(f"sqlite+pysqlite:///{migrated_db.as_posix()}")
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    assert "users" in tables
    assert "slice_jobs" in tables
    assert "alembic_version" in tables


def test_migrated_schema_matches_orm_metadata(migrated_db: Path):
    """No drift: every table and column in the ORM exists in the migrated DB."""
    sys.path.insert(0, str(BACKEND))
    from app.db.base import Base
    from app.db.schema import import_all_models

    import_all_models()

    engine = create_engine(f"sqlite+pysqlite:///{migrated_db.as_posix()}")
    inspector = inspect(engine)
    actual_tables = set(inspector.get_table_names()) - {"alembic_version"}
    expected_tables = set(Base.metadata.tables)

    assert actual_tables == expected_tables, (
        f"table drift: only in db={actual_tables - expected_tables}, "
        f"only in models={expected_tables - actual_tables}"
    )

    for table_name, table in Base.metadata.tables.items():
        actual_columns = {col["name"] for col in inspector.get_columns(table_name)}
        expected_columns = {col.name for col in table.columns}
        assert actual_columns == expected_columns, f"column drift in {table_name}"

    engine.dispose()


def test_downgrade_then_upgrade_round_trips(migrated_db: Path):
    down = _run_alembic("downgrade", "base", db_path=migrated_db)
    assert down.returncode == 0, down.stderr

    engine = create_engine(f"sqlite+pysqlite:///{migrated_db.as_posix()}")
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()
    assert remaining == set(), f"downgrade left tables behind: {remaining}"

    up = _run_alembic("upgrade", "head", db_path=migrated_db)
    assert up.returncode == 0, up.stderr


def test_no_pending_autogenerate_operations(migrated_db: Path):
    """`alembic check` must find no drift."""
    result = _run_alembic("check", db_path=migrated_db)
    assert result.returncode == 0, (
        "models and migrations have diverged:\n" f"{result.stdout}\n{result.stderr}"
    )


def test_migration_files_import_standalone(migrated_db: Path):
    """A migration must not need the `app` package merely to be importable."""
    versions = sorted((BACKEND / "alembic" / "versions").glob("*.py"))
    assert versions, "expected at least one migration"
    for version in versions:
        source = version.read_text(encoding="utf-8")
        assert "app.db.base.UUIDType(" not in source, (
            f"{version.name} renders UUIDType fully qualified, which couples the "
            "migration to the application package"
        )
        assert "from app.db.base import" in source
