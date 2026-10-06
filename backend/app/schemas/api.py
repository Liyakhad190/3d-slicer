"""Pydantic request/response schemas.

Schemas are the API's contract. They are deliberately explicit about types
rather than leaning on `Any` so that a malformed profile is rejected at the
edge instead of reaching the slicer.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


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
    transformed_blob_id: uuid.UUID | None = None
    transformation: dict[str, Any] | None = None
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


#: Largest accepted multiplier, as a percentage. 1e6% is a 10,000x scale, which
#: is what recovering a model exported in *meters* needs: the smallest model the
#: platform will scale is bounded by the millimetre guard below, so a model at
#: the 0.01 mm floor still reaches a 250 mm bed at this ceiling. Anything beyond
#: that is a unit mistake, not a scale the user meant.
MAX_SCALE_PERCENT = 1_000_000.0

#: Largest accepted target edge, in millimetres. 10 m is far beyond any build
#: volume, so it bounds a typo rather than a legitimate print.
MAX_TARGET_DIMENSION_MM = 10_000.0

#: Smallest accepted value. Dimensions and factors are float64 all the way down,
#: but below this the geometry is degenerate for printing and almost always a
#: unit error (microns, metres, inches), which the recovery paths exist to fix.
MIN_TRANSFORM_VALUE = 0.01


class ModelTransformRequest(BaseModel):
    scale_percent: dict[str, float] | None = None
    target_dimensions_mm: dict[str, float] | None = None
    preserve_aspect_ratio: bool = False
    reset: bool = False

    @model_validator(mode="after")
    def validate_transform(self) -> "ModelTransformRequest":
        if self.reset:
            if self.scale_percent is not None or self.target_dimensions_mm is not None:
                raise ValueError("Reset cannot be combined with scale values.")
            return self
        if (self.scale_percent is None) == (self.target_dimensions_mm is None):
            raise ValueError("Provide either scale_percent or target_dimensions_mm.")
        # The two modes carry different units, so each is bounded by what its own
        # unit can physically mean. One shared number silently caps percentages
        # far below what a meters-exported model needs to reach a build volume.
        for values, ceiling, label in (
            (self.scale_percent, MAX_SCALE_PERCENT, "Scale percentages"),
            (self.target_dimensions_mm, MAX_TARGET_DIMENSION_MM, "Target dimensions"),
        ):
            if values is None:
                continue
            if set(values) != {"x", "y", "z"}:
                raise ValueError("Scale values must include X, Y and Z.")
            for axis, value in values.items():
                if not math.isfinite(value):
                    raise ValueError(f"{label} must be finite ({axis} was {value}).")
                if not MIN_TRANSFORM_VALUE <= value <= ceiling:
                    raise ValueError(
                        f"{label} must be between {MIN_TRANSFORM_VALUE:g} and "
                        f"{ceiling:g} ({axis} was {value:g})."
                    )
        return self


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


class ModelTransformResponse(BaseModel):
    version: ModelVersionResponse
    analysis: AnalysisResponse
    dimensions_mm: dict[str, float]


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
    validation_reason: str | None = None
    provenance: str | None = "seed"
    build_volume: dict[str, Any]
    nozzle_diameters: list[Any]
    default_nozzle_diameter_mm: float = 0.4
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


class TemperatureRangeResponse(BaseModel):
    status: Literal["valid", "conflict"]
    safe_min_c: float
    safe_max_c: float
    recommended_c: int
    first_layer_recommended_c: int
    printer_min_c: float
    printer_max_c: float
    material_min_c: float
    material_max_c: float
    bed_safe_min_c: float
    bed_safe_max_c: float
    bed_recommended_c: int
    provenance: str
    reason: str
    conflict_code: str | None = None
    conflict_message: str | None = None
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #


class SliceJobCreate(BaseModel):
    model_version_id: uuid.UUID
    printer_id: uuid.UUID
    material_profile_id: uuid.UUID
    objective: Literal["quality", "strength", "speed", "material", "balanced"] = "balanced"
    nozzle_diameter_mm: Decimal | None = None
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


# --------------------------------------------------------------------------- #
# AI print preparation
# --------------------------------------------------------------------------- #


class PreparationCreate(BaseModel):
    model_version_id: uuid.UUID
    printer_id: uuid.UUID
    material_profile_id: uuid.UUID
    objective: Literal["quality", "strength", "speed", "material", "balanced"] = "balanced"
    user_request: str = Field(
        default="",
        max_length=4000,
        description="Free-text instruction shown to the model, e.g. 'print it strong'.",
    )
    output_format: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "Optional deliverable format. Checked against the printer's own "
            "declaration immediately: an undeclared format is refused with 409, "
            "a declared one this deployment cannot produce with 503."
        ),
    )


class PreparationReplanRequest(BaseModel):
    user_request: str = Field(default="", max_length=4000)
    feedback: str = Field(
        default="",
        max_length=4000,
        description="What to change about the refused plan.",
    )
    objective: Literal["quality", "strength", "speed", "material", "balanced"] = "balanced"


class PreparationOrientRequest(BaseModel):
    candidate_id: str | None = Field(
        default=None,
        max_length=64,
        description="ID of the candidate orientation to apply. If omitted, returns candidate options.",
    )


class PreparationSliceRequest(BaseModel):
    objective: Literal["quality", "strength", "speed", "material", "balanced"] = "balanced"
    output_format: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "Deliverable to produce from the sliced toolpath. Omit to keep the "
            "default selection semantics (G-code only)."
        ),
    )


class PreparationOutputFormat(BaseModel):
    """One output format, as the *printer profile* declares it."""

    key: str
    format: str
    extension: str
    declared_by_profile: bool
    advertised: bool
    available: bool
    reason: str = ""
    remedy: str = ""


class PreparationSliceRef(BaseModel):
    slice_job_id: uuid.UUID
    status: str
    input_fingerprint: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


class OutputArtifactResponse(BaseModel):
    id: uuid.UUID
    filename: str
    format: str
    bytes: int
    checksum: str
    printer: str | None = None
    material: str | None = None
    slicer: str | None = None
    slicer_version: str | None = None
    configuration_fingerprint: str | None = None
    validation: dict[str, Any] = Field(default_factory=dict)
    media_type: str = "application/octet-stream"
    created_at: datetime | None = None


class PreparationJobSummary(BaseModel):
    """One row of history: identity and verdict, no derived payload."""

    id: uuid.UUID
    status: str
    passed: bool
    attempt: int
    max_attempts: int
    model_version_id: uuid.UUID
    printer_id: uuid.UUID
    material_profile_id: uuid.UUID
    rejected_reason: str | None = None
    prompt_sha256: str = ""
    duration_ms: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class PreparationJobResponse(BaseModel):
    """Everything the attempt recorded, plus what the printer may produce."""

    id: uuid.UUID
    status: str
    passed: bool
    attempt: int
    max_attempts: int
    model_version_id: uuid.UUID
    printer_id: uuid.UUID
    material_profile_id: uuid.UUID

    model: dict[str, Any] | None = None
    printer: dict[str, Any] | None = None
    material: dict[str, Any] | None = None

    prompt: str = ""
    prompt_sha256: str = ""
    raw_response: str | None = None
    provider: str | None = None
    model_identifier: str | None = None

    plan: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    normalisations: list[Any] = Field(default_factory=list)
    rejected_reason: str | None = None
    input_sha256: str = ""
    duration_ms: int = 0

    output_formats: list[PreparationOutputFormat] = Field(default_factory=list)
    slice: PreparationSliceRef | None = None
    outputs: list[OutputArtifactResponse] = Field(default_factory=list)

    created_at: str | None = None
    updated_at: str | None = None
