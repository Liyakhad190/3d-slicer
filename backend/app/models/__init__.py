"""Model registry.

Every module must be imported here so that Alembic's autogenerate and
`Base.metadata.create_all` see the complete schema.
"""

from __future__ import annotations

from app.db.base import Base
from app.models.assets import (
    Blob,
    MeshRepair,
    Model,
    ModelAnalysis,
    ModelVersion,
    Orientation,
)
from app.models.catalog import (
    Abrasiveness,
    GcodeDialect,
    Machine,
    MachineProfile,
    Material,
    MaterialProfile,
    MotionSystem,
    Nozzle,
    Printer,
    ProfileStatus,
    Technology,
    ValidationStatus,
)
from app.models.identity import ApiToken, AuditLog, RateLimitBucket, User, UserRole
from app.models.preparation import PreparationStatus, PrintPreparationJob
from app.models.printing import (
    AiDecision,
    AiDecisionEvidence,
    Calibration,
    FailurePrediction,
    PrintHistory,
    PrintJob,
    PrintOutcome,
    PrintStatus,
    TelemetrySample,
)
from app.models.slicing import (
    STAGES,
    GeneratedFile,
    JobEvent,
    JobStageRun,
    JobStatus,
    SimulationResult,
    SliceJob,
    SliceParameter,
    SliceParameterSet,
    SlicerEngine,
    SlicerProfile,
    StageStatus,
    ValidationFinding,
    ValidationResult,
)

__all__ = [
    "STAGES",
    "Abrasiveness",
    "AiDecision",
    "AiDecisionEvidence",
    "ApiToken",
    "AuditLog",
    "Blob",
    "Calibration",
    "FailurePrediction",
    "GeneratedFile",
    "GcodeDialect",
    "JobEvent",
    "JobStageRun",
    "JobStatus",
    "Machine",
    "MachineProfile",
    "Material",
    "MaterialProfile",
    "MeshRepair",
    "Model",
    "ModelAnalysis",
    "ModelVersion",
    "MotionSystem",
    "Nozzle",
    "Orientation",
    "PreparationStatus",
    "PrintHistory",
    "PrintJob",
    "PrintOutcome",
    "PrintPreparationJob",
    "PrintStatus",
    "Printer",
    "ProfileStatus",
    "RateLimitBucket",
    "SimulationResult",
    "SliceJob",
    "SliceParameter",
    "SliceParameterSet",
    "SlicerEngine",
    "SlicerProfile",
    "StageStatus",
    "TelemetrySample",
    "Technology",
    "User",
    "UserRole",
    "ValidationFinding",
    "ValidationResult",
    "ValidationStatus",
    "Base",
]
