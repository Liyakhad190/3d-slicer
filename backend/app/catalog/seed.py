"""Catalog seeding from `seed_data.json`.

Profiles are loaded as **data**, never as code. That is the whole point of the
printer knowledge base: adding a printer means adding a row, not a branch in a
conditional.

Every machine profile is inserted with `validation_status = 'unvalidated'`.
The platform refuses to generate output for an unvalidated profile, so a new
contributor cannot accidentally have their untested numbers sent to a real
machine. Promotion to `validated` is an explicit, recorded act.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.catalog import (
    Abrasiveness,
    GcodeDialect,
    Machine,
    MachineProfile,
    Material,
    MaterialProfile,
    MotionSystem,
    Nozzle,
    ProfileStatus,
    Technology,
    ValidationStatus,
)

log = get_logger(__name__)
SEED_PATH = Path(__file__).with_name("seed_data.json")


def _hash_payload(payload: dict[str, Any]) -> str:
    """Stable content hash, so identical data never creates a duplicate version."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    return value


def load_seed_payload() -> dict[str, Any]:
    with SEED_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def seed_catalog(db: Session) -> dict[str, int]:
    """Idempotently load the seed data. Safe to run on every startup."""
    payload = load_seed_payload()
    counts = {
        "dialects": 0,
        "nozzles": 0,
        "materials": 0,
        "material_profiles": 0,
        "machines": 0,
        "machine_profiles": 0,
        "skipped": 0,
    }

    dialects = _seed_dialects(db, payload.get("gcode_dialects", []), counts)
    _seed_nozzles(db, payload.get("nozzles", []), counts)
    _seed_materials(db, payload.get("materials", []), counts)
    _seed_machines(db, payload.get("machines", []), dialects, counts)

    db.flush()
    log.info("catalog.seeded", **counts)
    return counts


def _seed_dialects(
    db: Session, rows: list[dict[str, Any]], counts: dict[str, int]
) -> dict[str, GcodeDialect]:
    out: dict[str, GcodeDialect] = {}
    for row in rows:
        existing = db.execute(
            select(GcodeDialect).where(GcodeDialect.name == row["name"])
        ).scalar_one_or_none()
        if existing is None:
            existing = GcodeDialect(
                name=row["name"],
                description=row.get("description"),
                known_commands=row.get("known_commands", []),
                temperature_commands=row.get("temperature_commands", []),
                extrusion_axis=row.get("extrusion_axis", []),
                quirks=row.get("quirks", {}),
            )
            db.add(existing)
            db.flush()
            counts["dialects"] += 1
        out[row["name"]] = existing
    return out


def _seed_nozzles(db: Session, rows: list[dict[str, Any]], counts: dict[str, int]) -> None:
    for row in rows:
        exists = db.execute(
            select(Nozzle.id).where(
                Nozzle.diameter_mm == Decimal(str(row["diameter_mm"])),
                Nozzle.material == row.get("material", "brass"),
                Nozzle.variant == row.get("variant", "standard"),
            )
        ).first()
        if exists:
            continue
        db.add(
            Nozzle(
                diameter_mm=Decimal(str(row["diameter_mm"])),
                material=row.get("material", "brass"),
                variant=row.get("variant", "standard"),
                wear_resistance_rating=int(row.get("wear_resistance_rating", 2)),
                notes=row.get("notes"),
            )
        )
        counts["nozzles"] += 1
    db.flush()


def _seed_materials(
    db: Session, rows: list[dict[str, Any]], counts: dict[str, int]
) -> None:
    for row in rows:
        material = db.execute(
            select(Material).where(Material.key == row["key"])
        ).scalar_one_or_none()
        if material is None:
            material = Material(
                key=row["key"],
                display_name=row["display_name"],
                family=row["family"],
                is_composite=bool(row.get("is_composite", False)),
            )
            db.add(material)
            db.flush()
            counts["materials"] += 1

        for profile in row.get("profiles", []):
            profile_hash = _hash_payload(profile)
            existing = db.execute(
                select(MaterialProfile).where(
                    MaterialProfile.material_id == material.id,
                    MaterialProfile.profile_hash == profile_hash,
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue

            db.add(
                MaterialProfile(
                    material_id=material.id,
                    vendor=profile.get("vendor", "generic"),
                    brand_sku=profile.get("brand_sku", "generic"),
                    profile_version=1,
                    profile_hash=profile_hash,
                    nozzle_temp=profile["nozzle_temp"],
                    bed_temp=profile["bed_temp"],
                    chamber_temp=profile.get("chamber_temp"),
                    glass_transition_c=profile.get("glass_transition_c"),
                    material_diameter=Decimal(
                        str(profile.get("material_diameter_mm", 1.75))
                    ),
                    max_volumetric_flow_mm3s=Decimal(
                        str(profile.get("max_volumetric_flow_mm3s", 0)) or 0
                    ),
                    retraction=profile.get("retraction", {}),
                    print_speed=profile.get("print_speed", {}),
                    layer_height_range=profile.get("layer_height_range", {}),
                    requires_enclosure=bool(profile.get("requires_enclosure", False)),
                    adhesion=profile.get("adhesion", {}),
                    mechanical=profile.get("mechanical", {}),
                    moisture_sensitivity=profile.get("moisture_sensitivity", "medium"),
                    shrinkage_pct=Decimal(str(profile.get("shrinkage_pct", 0)) or 0),
                    abrasiveness=Abrasiveness(profile.get("abrasiveness", "none")),
                    manufacturer_notes=profile.get("manufacturer_notes"),
                )
            )
            counts["material_profiles"] += 1
    db.flush()


def _seed_machines(
    db: Session,
    rows: list[dict[str, Any]],
    dialects: dict[str, GcodeDialect],
    counts: dict[str, int],
) -> None:
    for row in rows:
        machine = db.execute(
            select(Machine).where(
                Machine.vendor == row["vendor"],
                Machine.model == row["model"],
                Machine.generation == row.get("generation"),
            )
        ).scalar_one_or_none()
        if machine is None:
            machine = Machine(
                vendor=row["vendor"],
                model=row["model"],
                generation=row.get("generation"),
                technology=Technology(row.get("technology", "fdm")),
                motion_system=MotionSystem(row.get("motion_system", "cartesian")),
                documentation_url=row.get("documentation_url"),
                known_limitations=row.get("known_limitations", []),
            )
            db.add(machine)
            db.flush()
            counts["machines"] += 1

        profile = row["profile"]
        dialect_name = profile.get("gcode_dialect", "reprap")
        dialect = dialects.get(dialect_name)

        # `asdict`-equivalent without importing dataclasses: the profile dict
        # is already the exact column set, minus the two bookkeeping fields.
        profile_body: dict[str, Any] = {
            key: _to_jsonable(value)
            for key, value in profile.items()
            if key not in {"gcode_dialect"}
        }
        profile_hash = _hash_payload(profile_body)

        existing = db.execute(
            select(MachineProfile).where(
                MachineProfile.machine_id == machine.id,
                MachineProfile.profile_hash == profile_hash,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue

        next_version = (
            db.execute(
                select(MachineProfile.profile_version)
                .where(MachineProfile.machine_id == machine.id)
                .order_by(MachineProfile.profile_version.desc())
                .limit(1)
            ).scalar_one_or_none()
            or 0
        ) + 1

        db.add(
            MachineProfile(
                machine_id=machine.id,
                profile_version=next_version,
                status=ProfileStatus.draft,
                # Community data. Promotion to `validated` is a deliberate,
                # recorded act performed after bench testing a real machine.
                validation_status=ValidationStatus.unvalidated,
                profile_hash=profile_hash,
                build_volume=profile["build_volume"],
                bed_type=profile.get("bed_type"),
                heated_bed=bool(profile.get("heated_bed", True)),
                chamber=profile.get("chamber", {}),
                nozzle_diameters=profile.get("nozzle_diameters", []),
                extruders=int(profile.get("extruders", 1)),
                temperature_limits=profile["temperature_limits"],
                speed_limits=profile["speed_limits"],
                max_volumetric_flow_mm3s=Decimal(
                    str(profile.get("max_volumetric_flow_mm3s", 0)) or 0
                ),
                machine_restrictions=profile.get("machine_restrictions", {}),
                start_gcode=profile.get("start_gcode"),
                end_gcode=profile.get("end_gcode"),
                gcode_dialect_id=dialect.id if dialect else None,
                calibration_requirements=profile.get("calibration_requirements", {}),
                changelog=(
                    "Community-sourced seed data. Values must be checked against "
                    "the specific machine before validation."
                ),
            )
        )
        counts["machine_profiles"] += 1

    db.flush()
