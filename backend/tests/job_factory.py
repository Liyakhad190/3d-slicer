"""Row builders and model stubs for tests.

Everything here writes the same rows the production code writes - no stub
models, no relaxed constraints. The point is that a test can stand up a real
`SliceJob` with a real `GeneratedFile` without going through the whole upload
and slice flow. The scripted provider stands in for Ollama the same way: it
returns canned payloads through the real `LLMRequest`/`LLMResponse` interface,
so planning, coercion and validation all still run.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "correct-horse-battery-staple"

#: A plausible Marlin toolpath. Small enough to assert on, complete enough to
#: survive the G-code validator.
GCODE = b"""; FLAVOR:Marlin
G21
G90
M82
M140 S60
M104 S200
M190 S60
M109 S200
G28
G92 E0
G1 Z0.2 F300
G1 X10 Y10 F3000
G1 X50 Y10 E1.0 F1500
G1 X50 Y50 E2.0
G1 X10 Y50 E3.0
G1 X10 Y10 E4.0
M104 S0
M140 S0
G91
G1 Z10 F300
G90
M84
"""


def register_user(test_client: Any, email: str) -> str:
    """Register an account and return its id."""
    response = test_client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Fixture"},
    )
    assert response.status_code == 201, response.text
    return _user_id_for(test_client, email)


def _user_id_for(test_client: Any, email: str) -> uuid.UUID:
    from app.db.session import session_scope
    from app.models.identity import User

    with session_scope() as db:
        return db.query(User).filter(User.email == email).one().id


def build_sliced_job(
    db,
    *,
    user_id: uuid.UUID,
    gcode: bytes = GCODE,
    filename: str = "part.gcode",
    is_blocked: bool = False,
    extra_formats: list[str] | None = None,
) -> dict[str, Any]:
    """A completed `SliceJob` with one G-code `GeneratedFile` on it."""
    from app.models.assets import Blob, Model, ModelVersion
    from app.models.catalog import MachineProfile, MaterialProfile, Nozzle, Printer
    from app.models.slicing import GeneratedFile, JobStatus, SliceJob
    from app.services import storage

    machine_profile = db.query(MachineProfile).first()
    material_profile = db.query(MaterialProfile).first()
    nozzle = (
        db.query(Nozzle)
        .filter(Nozzle.diameter_mm == Decimal("0.4"), Nozzle.material == "brass")
        .first()
        or db.query(Nozzle).first()
    )
    assert machine_profile and material_profile and nozzle, "catalog is not seeded"

    printer = Printer(
        user_id=user_id,
        name="Fixture Printer",
        machine_profile_id=machine_profile.id,
        nozzle_diameter_mm=Decimal("0.4"),
        nozzle_material="brass",
        firmware="marlin",
    )
    db.add(printer)

    mesh_bytes = (FIXTURES / "cube_20mm.stl").read_bytes()
    mesh_blob, _ = storage.put_blob(
        mesh_bytes,
        media_type="model/stl",
        detected_format="stl_binary",
        session=db,
    )
    model = Model(user_id=user_id, name="Fixture Cube")
    db.add(model)
    db.flush()

    version = ModelVersion(
        model_id=model.id,
        version=1,
        original_filename="cube_20mm.stl",
        sanitized_filename="cube_20mm.stl",
        blob_id=mesh_blob.id,
        format="stl_binary",
        byte_size=len(mesh_bytes),
        sha256=mesh_blob.sha256,
    )
    db.add(version)
    db.flush()

    job = SliceJob(
        user_id=user_id,
        model_version_id=version.id,
        printer_id=printer.id,
        machine_profile_id=machine_profile.id,
        material_profile_id=material_profile.id,
        nozzle_id=nozzle.id,
        status=JobStatus.completed,
        model_input_sha256=mesh_blob.sha256,
    )
    db.add(job)
    db.flush()

    gcode_blob, _ = storage.put_blob(
        gcode, media_type="text/x-gcode", detected_format="gcode", session=db
    )
    generated = GeneratedFile(
        slice_job_id=job.id,
        blob_id=gcode_blob.id,
        kind="gcode",
        filename=filename,
        sha256=gcode_blob.sha256,
        byte_size=len(gcode),
        is_blocked=is_blocked,
        blocked_reason="fixture" if is_blocked else None,
    )
    db.add(generated)

    produced = {
        "job_id": job.id,
        "printer_id": printer.id,
        "version_id": version.id,
        "machine_profile_id": machine_profile.id,
        "material_profile_id": material_profile.id,
        "generated_id": generated.id,
        "input_sha256": mesh_blob.sha256,
        "gcode": gcode,
    }

    # Optional extra artifacts already on the job (e.g. a previously made .gx).
    for kind in extra_formats or []:
        blob, _ = storage.put_blob(
            b"already-produced", media_type="application/octet-stream",
            detected_format=kind, session=db,
        )
        db.add(
            GeneratedFile(
                slice_job_id=job.id,
                blob_id=blob.id,
                kind=kind,
                filename=f"part.{kind}",
                sha256=blob.sha256,
                byte_size=len(b"already-produced"),
            )
        )

    db.flush()
    produced["generated_id"] = generated.id
    return produced


class ScriptedProvider:
    """Returns canned payloads, or raises, in order.

    Stands in for the Ollama provider at the same interface the planner uses,
    so `propose`, coercion, validation and persistence all still run for real.
    """

    name = "scripted"

    def __init__(
        self, payloads: list[Any] | None = None, errors: list[Exception] | None = None
    ):
        self.payloads = list(payloads or [])
        self.errors = list(errors or [])
        self.requests: list[Any] = []

    def generate(self, request: Any) -> Any:
        from app.intelligence.providers.base import LLMResponse

        self.requests.append(request)
        if self.errors:
            raise self.errors.pop(0)
        payload = self.payloads.pop(0) if self.payloads else {}
        return LLMResponse(
            raw_text=json.dumps(payload),
            parsed=payload if isinstance(payload, dict) else None,
            model="scripted-1b",
            provider=self.name,
            duration_ms=5,
            schema_enforced=request.json_schema is not None,
        )

    def health(self) -> Any:
        from app.intelligence.providers.base import ProviderHealth

        return ProviderHealth(
            provider=self.name, available=True, model="scripted-1b", status="ready"
        )

    def list_models(self) -> list[str]:
        return ["scripted-1b"]
