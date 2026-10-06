"""Catalogue routes: machines, profiles, materials, nozzles, printers."""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, owned_printer
from app.core.errors import ValidationFailedError
from app.core.logging import get_logger
from app.models.catalog import (
    Machine,
    MachineProfile,
    Material,
    MaterialProfile,
    Nozzle,
    Printer,
    ValidationStatus,
)
from app.models.identity import AuditLog
from app.db.base import utcnow
from app.schemas.api import (
    MachineProfileResponse,
    MachineResponse,
    MaterialProfileResponse,
    MaterialResponse,
    NozzleResponse,
    PrinterCreate,
    PrinterResponse,
    TemperatureRangeResponse,
)

log = get_logger(__name__)
router = APIRouter(tags=["catalog"])


# --------------------------------------------------------------------------- #
# read-only knowledge base
# --------------------------------------------------------------------------- #


@router.get("/machines", response_model=list[MachineResponse], summary="Machine catalogue")
def list_machines(db: DbSession, _: CurrentUser) -> list[MachineResponse]:
    rows = db.execute(select(Machine).order_by(Machine.vendor, Machine.model)).scalars()
    return [MachineResponse.model_validate(r) for r in rows]


@router.get(
    "/machine-profiles",
    response_model=list[MachineProfileResponse],
    summary="Versioned machine profiles",
)
def list_machine_profiles(
    db: DbSession,
    _: CurrentUser,
    machine_id: uuid.UUID | None = None,
    validated_only: bool = Query(
        default=False,
        description="Only profiles that have been bench-validated on real hardware.",
    ),
) -> list[MachineProfileResponse]:
    stmt = select(MachineProfile)
    if machine_id is not None:
        stmt = stmt.where(MachineProfile.machine_id == machine_id)
    if validated_only:
        stmt = stmt.where(MachineProfile.validation_status == ValidationStatus.validated)
    stmt = stmt.order_by(MachineProfile.machine_id, MachineProfile.profile_version.desc())
    return [MachineProfileResponse.model_validate(r) for r in db.execute(stmt).scalars()]


@router.get("/materials", response_model=list[MaterialResponse], summary="Material families")
def list_materials(db: DbSession, _: CurrentUser) -> list[MaterialResponse]:
    rows = db.execute(select(Material).order_by(Material.display_name)).scalars()
    return [MaterialResponse.model_validate(r) for r in rows]


@router.get(
    "/material-profiles",
    response_model=list[MaterialProfileResponse],
    summary="Manufacturer-specific material profiles",
)
def list_material_profiles(
    db: DbSession, _: CurrentUser, material_id: uuid.UUID | None = None
) -> list[MaterialProfileResponse]:
    stmt = select(MaterialProfile)
    if material_id is not None:
        stmt = stmt.where(MaterialProfile.material_id == material_id)
    stmt = stmt.order_by(MaterialProfile.vendor, MaterialProfile.brand_sku)
    return [MaterialProfileResponse.model_validate(r) for r in db.execute(stmt).scalars()]


@router.get("/nozzles", response_model=list[NozzleResponse], summary="Nozzle catalogue")
def list_nozzles(
    db: DbSession,
    _: CurrentUser,
    diameter_mm: Decimal | None = Query(default=None, gt=0, le=10),
) -> list[NozzleResponse]:
    stmt = select(Nozzle)
    if diameter_mm is not None:
        stmt = stmt.where(Nozzle.diameter_mm == diameter_mm)
    rows = db.execute(stmt.order_by(Nozzle.diameter_mm)).scalars()
    return [NozzleResponse.model_validate(r) for r in rows]


# --------------------------------------------------------------------------- #
# the user's own printers
# --------------------------------------------------------------------------- #


@router.get("/printers", response_model=list[PrinterResponse], summary="Your printers")
def list_printers(db: DbSession, user: CurrentUser) -> list[PrinterResponse]:
    rows = db.execute(
        select(Printer)
        .where(Printer.user_id == user.id, Printer.archived_at.is_(None))
        .order_by(Printer.created_at)
    ).scalars()
    return [PrinterResponse.model_validate(r) for r in rows]


@router.post(
    "/printers",
    response_model=PrinterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a printer against a machine profile version",
)
def create_printer(payload: PrinterCreate, db: DbSession, user: CurrentUser) -> PrinterResponse:
    profile = db.get(MachineProfile, payload.machine_profile_id)
    if profile is None:
        raise ValidationFailedError(
            "That machine profile does not exist. Import or create a profile first.",
            details={"machine_profile_id": str(payload.machine_profile_id)},
        )

    # Refuse a nozzle the machine physically cannot accept. This is a hard
    # machine constraint, not a preference, so it is never overridable by the
    # optimiser or by an AI proposal.
    allowed = {Decimal(str(n)) for n in (profile.nozzle_diameters or [])}
    if allowed and payload.nozzle_diameter_mm not in allowed:
        raise ValidationFailedError(
            f"{profile.machine.vendor} {profile.machine.model} does not accept a "
            f"{payload.nozzle_diameter_mm} mm nozzle.",
            details={
                "requested_mm": str(payload.nozzle_diameter_mm),
                "supported_mm": sorted(str(a) for a in allowed),
            },
        )

    if payload.is_default:
        for existing in db.execute(
            select(Printer).where(Printer.user_id == user.id, Printer.is_default.is_(True))
        ).scalars():
            existing.is_default = False

    printer = Printer(
        user_id=user.id,
        name=payload.name.strip()[:120],
        machine_profile_id=payload.machine_profile_id,
        nozzle_diameter_mm=payload.nozzle_diameter_mm,
        nozzle_material=payload.nozzle_material,
        firmware=payload.firmware,
        bed_surface=payload.bed_surface,
        serial=payload.serial,
        is_default=payload.is_default,
    )
    db.add(printer)
    db.flush()

    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="printer.created",
            object_type="printer",
            object_id=printer.id,
            after={
                "name": printer.name,
                "machine_profile_id": str(profile.id),
                "profile_version": profile.profile_version,
                "nozzle_mm": str(printer.nozzle_diameter_mm),
            },
        )
    )
    log.info("printer.created", user_id=str(user.id), printer_id=str(printer.id))
    return PrinterResponse.model_validate(printer)


@router.get("/printers/{printer_id}", response_model=PrinterResponse, summary="One printer")
def get_printer(printer_id: uuid.UUID, db: DbSession, user: CurrentUser) -> PrinterResponse:
    return PrinterResponse.model_validate(owned_printer(db, user, printer_id))


@router.delete(
    "/printers/{printer_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a printer from the active printer list",
)
def archive_printer(printer_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Response:
    printer = owned_printer(db, user, printer_id)
    printer.archived_at = utcnow()
    printer.is_default = False
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="printer.archived",
            object_type="printer",
            object_id=printer.id,
            after={"name": printer.name},
        )
    )
    db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/temperature-range",
    response_model=TemperatureRangeResponse,
    summary="Deterministically resolve safe temperature limits for printer and material",
)
def get_temperature_range(
    printer_id: uuid.UUID,
    material_profile_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    nozzle_diameter_mm: Decimal | None = None,
) -> TemperatureRangeResponse:
    from app.core.errors import NotFoundError
    from app.slicing.temperature import resolve_safe_temperatures

    printer = owned_printer(db, user, printer_id)
    machine_profile = db.get(MachineProfile, printer.machine_profile_id)
    if machine_profile is None:
        raise NotFoundError("Machine profile not found.")
    material_profile = db.get(MaterialProfile, material_profile_id)
    if material_profile is None:
        raise NotFoundError("Material profile not found.")

    res = resolve_safe_temperatures(
        machine_profile=machine_profile,
        material_profile=material_profile,
    )
    return TemperatureRangeResponse(
        status="conflict" if res.is_conflict else "valid",
        safe_min_c=res.safe_min_c,
        safe_max_c=res.safe_max_c,
        recommended_c=res.target_nozzle_temp_c,
        first_layer_recommended_c=res.first_layer_nozzle_temp_c,
        printer_min_c=res.printer_min_c,
        printer_max_c=res.printer_max_c,
        material_min_c=res.material_min_c,
        material_max_c=res.material_max_c,
        bed_safe_min_c=res.bed_safe_min_c,
        bed_safe_max_c=res.bed_safe_max_c,
        bed_recommended_c=res.target_bed_temp_c,
        provenance=res.provenance,
        reason=res.reason,
        conflict_code=res.conflict_code,
        conflict_message=res.conflict_message,
        warnings=res.warnings,
    )
