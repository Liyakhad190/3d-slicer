"""The one place a `SliceJob` row is created from resolved profiles.

Both entry points — `POST /slice` and the preparation slice path — need the
same nozzle resolution and the same row shape. Writing them twice would let
the fallback rules drift, and a drifted nozzle lookup is exactly how a job
ends up attributed to the wrong hardware.

Nothing here slices. It resolves a nozzle and adds a row.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.assets import ModelVersion
from app.models.catalog import MachineProfile, MaterialProfile, Nozzle, Printer
from app.models.slicing import JobStatus, SliceJob

__all__ = ["create_slice_job", "nozzle_for"]


def nozzle_for(db: Session, printer: Printer) -> Nozzle:
    """The nozzle record this printer slices with.

    Falls back to any nozzle of the same diameter so the job is still runnable
    and the material mismatch stays visible, rather than blocking a request
    over a table that is a catalogue convenience rather than a fact.
    """
    nozzle = db.execute(
        select(Nozzle).where(
            Nozzle.diameter_mm == printer.nozzle_diameter_mm,
            Nozzle.material == printer.nozzle_material,
        )
    ).scalars().first()
    if nozzle is None:
        nozzle = db.execute(
            select(Nozzle).where(Nozzle.diameter_mm == printer.nozzle_diameter_mm)
        ).scalars().first()
    if nozzle is None:
        raise NotFoundError("No nozzle record exists for this printer's nozzle diameter.")
    return nozzle


def create_slice_job(
    db: Session,
    *,
    user_id: uuid.UUID,
    version: ModelVersion,
    printer: Printer,
    material_profile: MaterialProfile,
    machine_profile: MachineProfile | None = None,
    preparation_job_id: uuid.UUID | None = None,
) -> SliceJob:
    """Add a queued `SliceJob` for already-resolved, already-owned inputs."""
    machine_profile = machine_profile or db.get(MachineProfile, printer.machine_profile_id)
    if machine_profile is None:  # pragma: no cover - FK guarantees presence
        raise NotFoundError("The printer's machine profile does not exist.")

    job = SliceJob(
        user_id=user_id,
        model_version_id=version.id,
        printer_id=printer.id,
        machine_profile_id=machine_profile.id,
        material_profile_id=material_profile.id,
        nozzle_id=nozzle_for(db, printer).id,
        status=JobStatus.queued,
        preparation_job_id=preparation_job_id,
    )
    db.add(job)
    db.flush()
    return job
