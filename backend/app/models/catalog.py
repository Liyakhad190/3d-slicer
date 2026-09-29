"""Printer, material and nozzle knowledge base.

Profiles are **versioned and immutable**: an update inserts a new
`profile_version` row and moves a pointer. A `slice_job` stores the exact
profile ids it ran against, so a profile update can never silently change the
meaning of previously generated output.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin, TZDateTime, utcnow, uuid_pk


class Technology(str, enum.Enum):
    fdm = "fdm"
    msla = "msla"
    dlp = "dlp"
    sls = "sls"
    unknown = "unknown"


class MotionSystem(str, enum.Enum):
    cartesian = "cartesian"
    corexy = "corexy"
    corexz = "corexz"
    delta = "delta"
    idex = "idex"
    toolchanger = "toolchanger"
    other = "other"


class ProfileStatus(str, enum.Enum):
    draft = "draft"
    validated = "validated"
    deprecated = "deprecated"


class ValidationStatus(str, enum.Enum):
    unvalidated = "unvalidated"
    validated = "validated"
    rejected = "rejected"


class Abrasiveness(str, enum.Enum):
    none = "none"
    low = "low"
    medium = "medium"
    high = "high"


class GcodeDialect(Base):
    """Command vocabulary + quirks that drive the validator's whitelist."""

    __tablename__ = "gcode_dialects"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    known_commands: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    temperature_commands: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    extrusion_axis: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    quirks: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, default=utcnow)


class Machine(Base, TimestampMixin):
    """Catalogue row: a machine *product*, technology-agnostic."""

    __tablename__ = "machines"

    id: Mapped[uuid.UUID] = uuid_pk()
    vendor: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    generation: Mapped[str | None] = mapped_column(String(60), nullable=True)
    technology: Mapped[Technology] = mapped_column(
        SAEnum(Technology, native_enum=False, length=16),
        nullable=False,
        default=Technology.fdm,
        index=True,
    )
    motion_system: Mapped[MotionSystem] = mapped_column(
        SAEnum(MotionSystem, native_enum=False, length=24),
        nullable=False,
        default=MotionSystem.cartesian,
    )
    documentation_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    known_limitations: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)

    profiles: Mapped[list[MachineProfile]] = relationship(
        back_populates="machine", cascade="all, delete-orphan"
    )


class MachineProfile(Base):
    """Versioned capability + safety profile. Immutable once created.

    The UI refuses to offer a profile whose `validation_status` is not
    `validated` for output generation (see `catalog/service.py`).
    """

    __tablename__ = "machine_profiles"
    __table_args__ = (
        UniqueConstraint("machine_id", "profile_version", name="uq_machine_profile_version"),
        Index("ix_machine_profile_latest", "machine_id", "profile_version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[ProfileStatus] = mapped_column(
        SAEnum(ProfileStatus, native_enum=False, length=16),
        nullable=False,
        default=ProfileStatus.draft,
    )
    validation_status: Mapped[ValidationStatus] = mapped_column(
        SAEnum(ValidationStatus, native_enum=False, length=16),
        nullable=False,
        default=ValidationStatus.unvalidated,
        index=True,
    )
    validated_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    validated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    changelog: Mapped[str | None] = mapped_column(Text, nullable=True)

    build_volume: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    bed_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    heated_bed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    chamber: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    nozzle_diameters: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    extruders: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    temperature_limits: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    speed_limits: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    max_volumetric_flow_mm3s: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)
    machine_restrictions: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    start_gcode: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_gcode: Mapped[str | None] = mapped_column(Text, nullable=True)
    gcode_dialect_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gcode_dialects.id", ondelete="SET NULL"), nullable=True
    )
    calibration_requirements: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, index=True, default=utcnow
    )

    machine: Mapped[Machine] = relationship(back_populates="profiles")
    #: The dialect drives the validator's command whitelist. Loaded eagerly when
    #: a profile is fetched for slicing so the validator never has to guess the
    #: firmware's vocabulary.
    gcode_dialect: Mapped["GcodeDialect | None"] = relationship(
        "GcodeDialect", lazy="joined"
    )


class Nozzle(Base, TimestampMixin):
    __tablename__ = "nozzles"
    __table_args__ = (
        UniqueConstraint("diameter_mm", "material", "variant", name="uq_nozzle"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    diameter_mm: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False, index=True)
    material: Mapped[str] = mapped_column(String(40), nullable=False, default="brass")
    variant: Mapped[str] = mapped_column(String(40), nullable=False, default="standard")
    wear_resistance_rating: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Printer(Base, TimestampMixin):
    """A user's owned machine, bound to one specific profile *version*."""

    __tablename__ = "printers"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    machine_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machine_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    nozzle_diameter_mm: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    nozzle_material: Mapped[str] = mapped_column(String(40), nullable=False, default="brass")
    firmware: Mapped[str | None] = mapped_column(String(80), nullable=True)
    firmware_version: Mapped[str | None] = mapped_column(String(60), nullable=True)
    bed_surface: Mapped[str | None] = mapped_column(String(80), nullable=True)
    serial: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Material(Base, TimestampMixin):
    """Material *family* (PLA, PETG, ...). Not itself printable."""

    __tablename__ = "materials"

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    family: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    is_composite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    profiles: Mapped[list[MaterialProfile]] = relationship(
        back_populates="material", cascade="all, delete-orphan"
    )


class MaterialProfile(Base):
    """Versioned, manufacturer-specific material profile. Immutable."""

    __tablename__ = "material_profiles"
    __table_args__ = (
        UniqueConstraint(
            "material_id", "vendor", "brand_sku", "profile_version",
            name="uq_material_profile_version",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    material_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vendor: Mapped[str] = mapped_column(String(80), nullable=False, default="generic")
    brand_sku: Mapped[str] = mapped_column(String(80), nullable=False, default="generic")
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    nozzle_temp: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    bed_temp: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    chamber_temp: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    glass_transition_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_volumetric_flow_mm3s: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)
    retraction: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    print_speed: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    layer_height_range: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    requires_enclosure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    adhesion: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    mechanical: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    moisture_sensitivity: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    shrinkage_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 3), nullable=True)
    abrasiveness: Mapped[Abrasiveness] = mapped_column(
        SAEnum(Abrasiveness, native_enum=False, length=12),
        nullable=False,
        default=Abrasiveness.none,
    )
    manufacturer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, index=True, default=utcnow
    )

    material: Mapped[Material] = relationship(back_populates="profiles")
