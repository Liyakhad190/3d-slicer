"""Pydantic request/response schemas.

Schemas are the API's contract. They are deliberately explicit about types
rather than leaning on `Any` so that a malformed profile is rejected at the
edge instead of reaching the slicer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Display name cannot be blank.")
        return cleaned


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    user: UserResponse


# --------------------------------------------------------------------------- #
# models / assets
# --------------------------------------------------------------------------- #


class ModelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    current_version_id: uuid.UUID | None
    created_at: datetime


class ModelVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    model_id: uuid.UUID
    version: int
    original_filename: str
    format: str
    byte_size: int
    sha256: str
    analysis_status: str
    repair_status: str
    triangle_count: int | None
    bbox_x: float | None
    bbox_y: float | None
    bbox_z: float | None
    created_at: datetime


class UploadResponse(BaseModel):
    model: ModelResponse
    version: ModelVersionResponse
    deduplicated: bool = Field(
        description="True when an identical file was already stored; no new bytes were written."
    )


class FindingResponse(BaseModel):
    code: str
    severity: str
    message: str
    measured_value: float | None = None
    limit_value: float | None = None
    unit: str | None = None
    region: str | None = None
    mitigation: str | None = None


class AnalysisResponse(BaseModel):
    model_version_id: uuid.UUID | None
    input_sha256: str
    analyzer: str
    analyzer_version: str
    trimesh_version: str
    metrics: dict[str, Any]
    findings: list[FindingResponse]
    printability_score: float
    risk_level: str
    score_breakdown: dict[str, Any]
    duration_ms: int


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #


class MachineProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    machine_id: uuid.UUID
    profile_version: int
    status: str
    validation_status: str
    build_volume: dict[str, Any]
    nozzle_diameters: list[Any]
    extruders: int
    temperature_limits: dict[str, Any]
    speed_limits: dict[str, Any]
    max_volumetric_flow_mm3s: Decimal | None
    start_gcode: str | None
    end_gcode: str | None
    machine_restrictions: dict[str, Any]
    profile_hash: str
    created_at: datetime


class MachineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vendor: str
    model: str
    generation: str | None
    technology: str
    motion_system: str
    known_limitations: list[Any]


class PrinterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    machine_profile_id: uuid.UUID
    nozzle_diameter_mm: Decimal = Field(gt=0, le=10)
    nozzle_material: str = Field(default="brass", max_length=40)
    firmware: str | None = Field(default=None, max_length=80)
    bed_surface: str | None = Field(default=None, max_length=80)
    serial: str | None = Field(default=None, max_length=80)
    is_default: bool = False


class PrinterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    machine_profile_id: uuid.UUID
    nozzle_diameter_mm: Decimal
    nozzle_material: str
    firmware: str | None
    bed_surface: str | None
    is_default: bool
    created_at: datetime


class MaterialProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    material_id: uuid.UUID
    vendor: str
    brand_sku: str
    profile_version: int
    nozzle_temp: dict[str, Any]
    bed_temp: dict[str, Any]
    max_volumetric_flow_mm3s: Decimal | None
    retraction: dict[str, Any]
    print_speed: dict[str, Any]
    layer_height_range: dict[str, Any]
    abrasiveness: str
    requires_enclosure: bool
    glass_transition_c: float | None
    moisture_sensitivity: str
    shrinkage_pct: Decimal | None
    adhesion: dict[str, Any]
    mechanical: dict[str, Any]
    manufacturer_notes: str | None
    profile_hash: str


class MaterialResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    display_name: str
    family: str
    is_composite: bool


class NozzleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    diameter_mm: Decimal
    material: str
    variant: str
    wear_resistance_rating: int


class FormatCapability(BaseModel):
    format: str
    extension: str
    media_type: str
    read: bool
    write: bool
    is_archive: bool
    notes: str | None = None


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #


class SliceJobCreate(BaseModel):
    model_version_id: uuid.UUID
    printer_id: uuid.UUID
    material_profile_id: uuid.UUID
    objective: Literal["quality", "strength", "speed", "material", "balanced"] = "balanced"
    engine_settings_overrides: dict[str, Any] = Field(default_factory=dict)
    locked_parameters: list[str] = Field(default_factory=list)


class SliceJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    model_version_id: uuid.UUID
    printer_id: uuid.UUID
    machine_profile_id: uuid.UUID
    material_profile_id: uuid.UUID
    nozzle_id: uuid.UUID
    status: str
    stage_progress: dict[str, Any]
    error_code: str | None
    error_detail: str | None
    input_fingerprint: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class JobEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    at: datetime
    level: str
    stage: str | None
    message: str
    data: dict[str, Any]


class GeneratedFileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    filename: str
    sha256: str
    byte_size: int
    is_blocked: bool
    blocked_reason: str | None
    created_at: datetime
