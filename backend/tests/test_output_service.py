"""Producing deliverable files from a sliced job.

Covers the seam between "what was sliced" and "what the operator downloads":
capability gating by the printer profile, refusal when an encoder is missing,
discard-on-invalid, idempotency, and the artifact record's provenance.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import ConflictError  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.models.slicing import GeneratedFile, SliceJob  # noqa: E402
from app.slicing.output import (  # noqa: E402
    OutputFormatRejected,
    OutputFormatUnsupported,
    declared_output_formats,
)
from app.slicing.output.gx import GXEncoderRegistry, RecordingEncoder  # noqa: E402
from app.slicing.output.service import (  # noqa: E402
    advertised_formats,
    formats_for,
    generate_output,
)
from tests.job_factory import GCODE, build_sliced_job, register_user  # noqa: E402


class _DeclaredProfile:
    """A machine profile that declares formats the seeded one does not."""

    def __init__(self, inner, formats):
        self._inner = inner
        self.machine_restrictions = {"output_formats": formats}

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def user_id(test_client) -> uuid.UUID:
    return register_user(test_client, f"output-{uuid.uuid4().hex[:8]}@example.com")


def _with_encoders(encoders: GXEncoderRegistry):
    """Install a encoder registry for the duration of a block."""
    from app.slicing.output import gx as gx_module

    class _Scope:
        def __enter__(self):
            self._original = gx_module._encoder_registry
            gx_module._encoder_registry = encoders
            return self

        def __exit__(self, *exc):
            gx_module._encoder_registry = self._original
            return False

    return _Scope()


# --------------------------------------------------------------------------- #
# capability gating
# --------------------------------------------------------------------------- #


def test_seeded_profile_declares_gcode_and_advertises_only_that(user_id):
    with session_scope() as db:
        ids = build_sliced_job(db, user_id=user_id)
        job = db.get(SliceJob, ids["job_id"])
        from app.models.catalog import MachineProfile

        profile = db.get(MachineProfile, job.machine_profile_id)

        assert declared_output_formats(profile) == ("gcode",)
        assert advertised_formats(profile) == ["gcode"]

        rows = {r["key"]: r for r in formats_for(profile)}
        assert rows["gcode"]["advertised"] is True
        # gx is registered, but this printer never declared it.
        assert rows["gx"]["declared_by_profile"] is False
        assert rows["gx"]["advertised"] is False


def test_undeclared_format_is_refused_before_any_work(user_id):
    with session_scope() as db:
        ids = build_sliced_job(db, user_id=user_id)
        job = db.get(SliceJob, ids["job_id"])
        from app.models.catalog import MachineProfile

        profile = db.get(MachineProfile, job.machine_profile_id)

        with pytest.raises(OutputFormatUnsupported) as exc:
            generate_output(db, job=job, machine_profile=profile, format_key="gx")
        assert exc.value.http_status == 409
        # Nothing was written.
        kinds = {
            f.kind
            for f in db.query(GeneratedFile)
            .filter(GeneratedFile.slice_job_id == job.id)
            .all()
        }
        assert kinds == {"gcode"}


# --------------------------------------------------------------------------- #
# gcode
# --------------------------------------------------------------------------- #


def test_gcode_request_returns_the_source_artifact_unchanged(user_id):
    with session_scope() as db:
        ids = build_sliced_job(db, user_id=user_id)
        job = db.get(SliceJob, ids["job_id"])
        from app.models.catalog import MachineProfile

        profile = db.get(MachineProfile, job.machine_profile_id)

        generated, artifact = generate_output(
            db, job=job, machine_profile=profile, format_key="gcode"
        )
        assert generated.id == ids["generated_id"]
        assert artifact.format == "gcode"
        assert artifact.filename == "part.gcode"
        assert artifact.checksum.startswith("sha256:")

        # Still exactly one G-code row: the source is never stored twice.
        count = (
            db.query(GeneratedFile)
            .filter(
                GeneratedFile.slice_job_id == job.id, GeneratedFile.kind == "gcode"
            )
            .count()
        )
        assert count == 1


def test_blocked_toolpath_is_never_reproduced(user_id):
    with session_scope() as db:
        ids = build_sliced_job(db, user_id=user_id, is_blocked=True)
        job = db.get(SliceJob, ids["job_id"])
        from app.models.catalog import MachineProfile

        profile = db.get(MachineProfile, job.machine_profile_id)

        with pytest.raises(ConflictError):
            generate_output(db, job=job, machine_profile=profile, format_key="gcode")


def test_a_job_with_no_toolpath_cannot_be_formatted(user_id):
    from app.core.errors import NotFoundError

    with session_scope() as db:
        ids = build_sliced_job(db, user_id=user_id, filename="other.gcode")
        job = db.get(SliceJob, ids["job_id"])
        db.query(GeneratedFile).filter(
            GeneratedFile.slice_job_id == job.id
        ).delete()
        from app.models.catalog import MachineProfile

        profile = db.get(MachineProfile, job.machine_profile_id)
        with pytest.raises(NotFoundError):
            generate_output(db, job=job, machine_profile=profile, format_key="gcode")


# --------------------------------------------------------------------------- #
# gx
# --------------------------------------------------------------------------- #


def test_gx_requires_the_printer_to_declare_it(user_id):
    with session_scope() as db:
        ids = build_sliced_job(db, user_id=user_id)
        job = db.get(SliceJob, ids["job_id"])
        from app.models.catalog import MachineProfile

        declared = _DeclaredProfile(db.get(MachineProfile, job.machine_profile_id), ["gcode", "gx"])

        # Declared, but no encoder is installed here -> 503 with the reason.
        from app.slicing.output.base import OutputFormatUnavailable

        with pytest.raises(OutputFormatUnavailable) as exc:
            generate_output(db, job=job, machine_profile=declared, format_key="gx")
        assert exc.value.http_status == 503
        assert "GPX" in str(exc.value) or "encoder" in str(exc.value)


def test_gx_produces_a_real_artifact_when_an_encoder_exists(user_id):
    with session_scope() as db:
        ids = build_sliced_job(db, user_id=user_id)
        job = db.get(SliceJob, ids["job_id"])
        from app.models.catalog import MachineProfile

        declared = _DeclaredProfile(db.get(MachineProfile, job.machine_profile_id), ["gcode", "gx"])
        registry = GXEncoderRegistry()
        registry.register(RecordingEncoder())

        with _with_encoders(registry):
            generated, artifact = generate_output(
                db, job=job, machine_profile=declared, format_key="gx"
            )

        assert generated.kind == "gx"
        assert generated.filename == "part.gx"
        assert generated.is_blocked is False
        assert generated.blob_id != ids["generated_id"]

        assert artifact.format == "gx"
        assert artifact.media_type == "application/octet-stream"
        assert artifact.bytes > 0
        assert artifact.checksum.startswith("sha256:")
        assert artifact.printer  # provenance from the machine profile
        assert artifact.configuration_fingerprint in (None, "") or isinstance(
            artifact.configuration_fingerprint, str
        )
        assert artifact.validation["status"] == "passed"
        assert artifact.validation["source_validation"]["status"] == "unvalidated"

        # The stored bytes are genuinely different from the G-code.
        from app.models.assets import Blob
        from app.services import storage

        blob = db.get(Blob, generated.blob_id)
        stored = storage.read_blob(blob)
        assert stored != GCODE
        assert stored[5:15] == b"FlashPrint"


def test_gx_request_is_idempotent(user_id):
    with session_scope() as db:
        ids = build_sliced_job(db, user_id=user_id)
        job = db.get(SliceJob, ids["job_id"])
        from app.models.catalog import MachineProfile

        declared = _DeclaredProfile(db.get(MachineProfile, job.machine_profile_id), ["gcode", "gx"])
        registry = GXEncoderRegistry()
        registry.register(RecordingEncoder())

        with _with_encoders(registry):
            first, _ = generate_output(
                db, job=job, machine_profile=declared, format_key="gx"
            )
            second, _ = generate_output(
                db, job=job, machine_profile=declared, format_key="gx"
            )

        assert first.id == second.id
        count = (
            db.query(GeneratedFile)
            .filter(GeneratedFile.slice_job_id == job.id, GeneratedFile.kind == "gx")
            .count()
        )
        assert count == 1


def test_output_that_fails_validation_is_discarded_not_stored(user_id):
    from app.slicing.output import gx as gx_module

    class _LyingEncoder:
        key = "lying"

        def availability(self):
            return gx_module.FormatterAvailability(available=True, reason="test")

        def encode(self, gcode, *, context):
            return gcode  # a rename, not a conversion

        def describe(self):
            return {"key": self.key}

    with session_scope() as db:
        ids = build_sliced_job(db, user_id=user_id)
        job = db.get(SliceJob, ids["job_id"])
        from app.models.catalog import MachineProfile

        declared = _DeclaredProfile(db.get(MachineProfile, job.machine_profile_id), ["gcode", "gx"])
        registry = GXEncoderRegistry()
        registry.register(_LyingEncoder())

        with _with_encoders(registry):
            with pytest.raises(OutputFormatRejected) as exc:
                generate_output(db, job=job, machine_profile=declared, format_key="gx")
            assert any("byte-identical" in p for p in exc.value.details["problems"])

        rows = (
            db.query(GeneratedFile)
            .filter(GeneratedFile.slice_job_id == job.id, GeneratedFile.kind == "gx")
            .count()
        )
        assert rows == 0, "a rejected file must never be stored"


def test_artifact_describes_a_previously_generated_file(user_id):
    from app.slicing.output.service import artifact_for

    with session_scope() as db:
        ids = build_sliced_job(db, user_id=user_id)
        job = db.get(SliceJob, ids["job_id"])
        generated = db.get(GeneratedFile, ids["generated_id"])

        artifact = artifact_for(db, generated, job=job)
        assert artifact.filename == "part.gcode"
        assert artifact.format == "gcode"
        assert artifact.validation["status"] == "unvalidated"
        assert artifact.metadata["byte_size"] == len(GCODE)
