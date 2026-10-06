/** API client. Typed, with the error envelope from the backend understood. */

const BASE = '/api/v1'

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
    readonly status = 0,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

let token: string | null = localStorage.getItem('sp.token')

export function setToken(value: string | null): void {
  token = value
  if (value) localStorage.setItem('sp.token', value)
  else localStorage.removeItem('sp.token')
}

export function getToken(): string | null {
  return token
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(`${BASE}${path}`, { ...init, headers })

  if (!response.ok) {
    let code = `HTTP_${response.status}`
    let message = response.statusText
    let details: Record<string, unknown> = {}
    try {
      const body = (await response.json()) as {
        error?: { code: string; message: string; details?: Record<string, unknown> }
      }
      if (body.error) {
        code = body.error.code
        message = body.error.message
        details = body.error.details ?? {}
      }
    } catch {
      /* a non-JSON error body is not worth failing over */
    }
    throw new ApiError(code, message, details, response.status)
  }

  if (response.status === 204) return undefined as T
  const type = response.headers.get('content-type') ?? ''
  if (type.includes('application/json')) return (await response.json()) as T
  return (await response.text()) as unknown as T
}

/** A file fetched from the API with its auth header attached. */
export interface DownloadedFile {
  blob: Blob
  filename: string
  mediaType: string
  checksum: string | null
  format: string | null
}

/**
 * GET an authenticated file. The bearer token cannot travel on an <a href>,
 * so downloads go through fetch and the browser is handed the bytes instead.
 * Filenames come from the server's Content-Disposition, never from a path.
 */
async function download(path: string, fallbackFilename: string): Promise<DownloadedFile> {
  const headers = new Headers()
  if (token) headers.set('Authorization', `Bearer ${token}`)

  const response = await fetch(`${BASE}${path}`, { headers })
  if (!response.ok) {
    let code = `HTTP_${response.status}`
    let message = response.statusText
    let details: Record<string, unknown> = {}
    try {
      const body = (await response.json()) as {
        error?: { code: string; message: string; details?: Record<string, unknown> }
      }
      if (body.error) {
        code = body.error.code
        message = body.error.message
        details = body.error.details ?? {}
      }
    } catch {
      /* a non-JSON error body is not worth failing over */
    }
    throw new ApiError(code, message, details, response.status)
  }

  const disposition = response.headers.get('content-disposition') ?? ''
  const named = /filename="([^"]+)"/.exec(disposition)
  return {
    blob: await response.blob(),
    filename: named?.[1] ?? fallbackFilename,
    mediaType: response.headers.get('content-type') ?? 'application/octet-stream',
    checksum: response.headers.get('x-content-sha256'),
    format: response.headers.get('x-output-format'),
  }
}

// --- types ---------------------------------------------------------------

export interface User {
  id: string
  email: string
  display_name: string
  role: string
  is_active: boolean
  created_at: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
  expires_at: string
  user: User
}

export interface ModelSummary {
  id: string
  name: string
  description: string | null
  current_version_id: string | null
  created_at: string
}

export interface ModelVersion {
  id: string
  model_id: string
  version: number
  original_filename: string
  format: string
  byte_size: number
  sha256: string
  transformed_blob_id: string | null
  transformation: Record<string, unknown> | null
  analysis_status: string
  repair_status: string
  triangle_count: number | null
  bbox_x: number | null
  bbox_y: number | null
  bbox_z: number | null
  created_at: string
}

export interface Finding {
  code: string
  severity: 'critical' | 'warning' | 'info'
  message: string
  measured_value: number | null
  limit_value: number | null
  unit: string | null
  region: string | null
  mitigation: string | null
}

export interface Analysis {
  model_version_id: string | null
  input_sha256: string
  analyzer: string
  analyzer_version: string
  trimesh_version: string
  metrics: Record<string, unknown>
  findings: Finding[]
  printability_score: number
  risk_level: string
  score_breakdown: Record<string, unknown>
  duration_ms: number
}

export interface ModelTransformResponse {
  version: ModelVersion
  analysis: Analysis
  dimensions_mm: { x: number; y: number; z: number }
}

/** The body `POST /models/versions/{id}/transform` accepts: one mode at a time. */
export type ModelTransformBody =
  | { scale_percent: { x: number; y: number; z: number }; preserve_aspect_ratio: boolean }
  | { target_dimensions_mm: { x: number; y: number; z: number }; preserve_aspect_ratio: boolean }
  | { reset: true }

export interface Machine {
  id: string
  vendor: string
  model: string
  generation: string | null
  technology: string
  motion_system: string
  known_limitations: string[]
}

export interface MachineProfile {
  id: string
  machine_id: string
  profile_version: number
  status: string
  validation_status: string
  validation_reason?: string | null
  provenance?: string | null
  build_volume: { x: number; y: number; z: number }
  nozzle_diameters: number[]
  default_nozzle_diameter_mm: number
  extruders: number
  temperature_limits: Record<string, { min: number; max: number }>
  speed_limits: Record<string, number>
  max_volumetric_flow_mm3s: string | null
  start_gcode: string | null
  end_gcode: string | null
  machine_restrictions: Record<string, unknown>
  profile_hash: string
  created_at: string
}

export interface Material {
  id: string
  key: string
  display_name: string
  family: string
  is_composite: boolean
}

export interface MaterialProfile {
  id: string
  material_id: string
  vendor: string
  brand_sku: string
  profile_version: number
  nozzle_temp: { min: number; typical: number; max: number }
  bed_temp: { min: number; typical: number; max: number }
  max_volumetric_flow_mm3s: string | null
  retraction: Record<string, number>
  print_speed: Record<string, number>
  layer_height_range: { min: number; max: number }
  abrasiveness: string
  requires_enclosure: boolean
  glass_transition_c: number | null
  moisture_sensitivity: string
  manufacturer_notes: string | null
  profile_hash: string
}

export interface Nozzle {
  id: string
  diameter_mm: string
  material: string
  variant: string
  wear_resistance_rating: number
}

export interface Printer {
  id: string
  name: string
  machine_profile_id: string
  nozzle_diameter_mm: string
  nozzle_material: string
  firmware: string | null
  bed_surface: string | null
  is_default: boolean
  created_at: string
}

export interface SliceJob {
  id: string
  model_version_id: string
  printer_id: string
  machine_profile_id: string
  material_profile_id: string
  nozzle_id: string
  status: string
  stage_progress: Record<string, unknown>
  error_code: string | null
  error_detail: string | null
  input_fingerprint: string
  queued_at: string
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface StageRun {
  stage: string
  attempt: number
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'skipped' | 'blocked'
  progress: number
  duration_ms: number
  detail: Record<string, unknown>
  started_at: string
  ended_at: string | null
}

export interface JobEvent {
  id: string
  at: string
  level: string
  stage: string | null
  message: string
  data: Record<string, unknown>
}

export interface ValidationFinding {
  severity: 'critical' | 'warning' | 'info'
  code: string
  message: string
  line_number: number | null
  axis: string | null
  measured_value: number | null
  limit_value: number | null
  unit: string | null
  mitigation: string | null
}

export interface ValidationResponse {
  validated: boolean
  passed?: boolean
  blocks_output?: boolean
  summary?: string
  severity_counts?: Record<string, number>
  validator?: string
  validator_version?: string
  reason?: string
  findings: ValidationFinding[]
}

export interface GeneratedFile {
  id: string
  kind: string
  filename: string
  sha256: string
  byte_size: number
  is_blocked: boolean
  blocked_reason: string | null
  created_at: string
}

export interface ToolpathSegment {
  kind: 'extrude' | 'travel' | 'retract'
  from: [number, number, number]
  to: [number, number, number]
  layer: number | null
  e: number
}

export interface Toolpath {
  job_id: string
  blocked: boolean
  build_volume: { x: number; y: number; z: number }
  version: string
  layer_count: number
  layer_z: number[]
  bounds: { min: [number, number, number]; max: [number, number, number] }
  stats: {
    extrusion_moves: number
    travel_moves: number
    retractions: number
    filament_extruded_mm: number
    build_volume: { x: number; y: number; z: number }
  }
  segments: ToolpathSegment[]
  segments_elided: number
}

export interface Technology {
  key: string
  display_name: string
  input_formats: string[]
  output_formats: string[]
  supported: boolean
  pipeline: string
  notes: string
}

export interface SlicerEngine {
  engine: string
  display_name: string
  license: string
  available: boolean
  version: string | null
  capabilities: Record<string, unknown>
}

export interface FormatCapability {
  format: string
  extension: string
  media_type: string
  read: boolean
  write: boolean
  is_archive: boolean
  notes: string | null
}

// --- temperature range ---------------------------------------------------

export interface TemperatureRange {
  status: 'valid' | 'conflict'
  safe_min_c: number
  safe_max_c: number
  recommended_c: number
  first_layer_recommended_c: number
  printer_min_c: number
  printer_max_c: number
  material_min_c: number
  material_max_c: number
  bed_safe_min_c: number
  bed_safe_max_c: number
  bed_recommended_c: number
  provenance: string
  reason: string
  conflict_code: string | null
  conflict_message: string | null
  warnings: string[]
}

// --- ai -------------------------------------------------------------------

/** Priorities the model scored, each 0..1 after validation. */
export interface IntentPriorities {
  strength: number
  speed: number
  material_efficiency: number
  surface_quality: number
}

export type SupportPreference = 'avoid' | 'allowed' | 'required'
export type OrientationPreference = 'user' | 'optimized' | 'quality' | 'speed'

export interface IntentConstraints {
  max_print_time_minutes: number | null
  max_material_grams: number | null
  allow_scaling: boolean
}

export interface PrintPlan {
  intent: IntentPriorities
  constraints: IntentConstraints
  support_preference: SupportPreference
  orientation_preference: OrientationPreference
  rationale: string
  confidence: number | null
}

/** A value the model produced that the server had to bring into range. */
export interface Normalisation {
  field: string
  model_value: unknown
  applied_value: unknown
  reason: string
}

export interface PlanValidation {
  valid: boolean
  rejected_reason: string | null
  plan: PrintPlan
  normalisations: Normalisation[]
}

export interface AiHealth {
  provider: string
  model: string
  available: boolean
  status: string
  code: string | null
  message: string
  latency_ms: number | null
  models_available: string[]
  hint: string
}

export interface AiPlanRequest {
  request: string
  model_version_id?: string | null
  printer_id?: string | null
  material_profile_id?: string | null
  conversation?: { role: string; content: string }[]
}

export interface AiDecision {
  id: string
  decision_type: string
  reason: string
  evidence_count: number
  normalised: boolean
}

export interface AiPlan {
  request: string
  provider: string
  model: string
  duration_ms: number
  attempts: number
  validation: PlanValidation
  context: {
    tools: { name: string; summary: string; data: Record<string, unknown> }[]
    retrieved: string[]
  }
  /** The `ai_decisions` row for this plan, readable back from /ai/decisions. */
  decision: AiDecision | null
  decision_id: string | null
}

export interface AiApplyRequest {
  plan: Record<string, unknown>
  parameters?: Record<string, unknown>
  printer_id?: string | null
  /** Links the application row to the plan row it came from. */
  plan_decision_id?: string | null
}

export interface ParameterChange {
  field: string
  value: unknown
  previous: unknown
  reason: string
}

export interface AiApply {
  parameters: Record<string, unknown>
  changes: ParameterChange[]
  warnings: string[]
  dominant_priority: 'strength' | 'speed' | 'material_efficiency' | 'surface_quality' | 'balanced'
  decision: AiDecision | null
  decision_id: string | null
}

export interface AiRefineRequest {
  previous_plan: Record<string, unknown>
  instruction: string
  previous_decision_id?: string | null
}

export interface DecisionEvidence {
  kind: string
  ref_type: string
  ref_id: string | null
  excerpt: string | null
  weight: number
}

export interface RecordedDecision {
  id: string
  decision_type: string
  reason: string
  confidence: number | null
  calibration: string
  source: string
  model_identifier: string | null
  prompt_hash: string | null
  proposed_value: Record<string, unknown> | null
  applied_value: Record<string, unknown> | null
  slice_job_id: string | null
  overridden_by: string | null
  overridden_at: string | null
  override_reason: string | null
  created_at: string
  evidence: DecisionEvidence[]
}

// --- print preparation ----------------------------------------------------
//
// Shapes mirrored from backend/app/schemas/api.py and the preparation
// package. The backend is authoritative: nothing here is derived, defaulted
// or re-computed in the browser.

/** What the user asked the print to optimise. */
export type PrintObjective = 'quality' | 'strength' | 'speed' | 'material' | 'balanced'

/** Lifecycle of one preparation attempt (backend `PreparationStatus`). */
export type PreparationStatus =
  | 'queued'
  | 'analysing'
  | 'proposing'
  | 'validating'
  | 'completed'
  | 'rejected'
  | 'failed'

/** One of the 22 deterministic checks' outcomes. */
export type ValidationCheckStatus =
  | 'PASS'
  | 'WARNING'
  | 'FAILED'
  | 'NOT_REQUIRED'
  | 'UNSUPPORTED'

/** One checklist row, exactly as the validator recorded it. */
export interface ValidationCheck {
  category: string
  status: ValidationCheckStatus
  code: string
  reason: string
  evidence: string[]
  correctable: boolean
  downgradable: boolean
  normalisations: Normalisation[]
}

export interface ValidationSummary {
  total: number
  pass: number
  warning: number
  failed: number
  not_required: number
  unsupported: number
}

/** `ValidationOutcome.to_dict()`; empty for an attempt that produced no plan. */
export interface ValidationResult {
  validator_version?: string
  passed?: boolean
  can_replan?: boolean
  summary?: ValidationSummary
  checks?: ValidationCheck[]
  /** Keys the optimizer must not move. */
  locked?: Record<string, unknown>
  /** Canonical keys merged over the optimizer's output. */
  overrides?: Record<string, unknown>
  normalisations?: Normalisation[]
  /** Only on a failed attempt: why no plan exists. */
  error?: {
    type?: string
    code?: string | null
    message?: string
    details?: Record<string, unknown>
  }
}

/** Shared envelope of every checklist category in the plan. */
export interface PlanCategory {
  status: string
  reason: string
  evidence: string[]
}

export interface PlanModelValidity extends PlanCategory {
  mesh_repair_required: boolean
  watertight: boolean
}
export interface PlanOrientation extends PlanCategory {
  candidate_id: string
  strategy: string
}
export interface PlanBedPlacement extends PlanCategory {
  strategy: string
}
export interface PlanLayerHeight extends PlanCategory {
  value_mm: number | null
}
export interface PlanWalls extends PlanCategory {
  count: number | null
}
export interface PlanTopBottom extends PlanCategory {
  top_layers: number | null
  bottom_layers: number | null
}
export interface PlanInfill extends PlanCategory {
  density_percent: number | null
  pattern: string | null
}
export interface PlanSupports extends PlanCategory {
  enabled: boolean
  placement: string
  angle_deg: number | null
  density_percent: number | null
  pattern: string | null
}
export interface PlanSupportInterface extends PlanCategory {
  enabled: boolean
  height_mm: number | null
  density_percent: number | null
}
export interface PlanBrim extends PlanCategory {
  enabled: boolean
  width_mm: number | null
}
export interface PlanRaft extends PlanCategory {
  enabled: boolean
}
export interface PlanBedAdhesion extends PlanCategory {
  risk: string
}
export interface PlanTemperature extends PlanCategory {
  nozzle_c: number | null
  bed_c: number | null
  first_layer_nozzle_c: number | null
  first_layer_bed_c: number | null
  source: string
}
export interface PlanCooling extends PlanCategory {
  fan_percent: number | null
  min_layer_time_s: number | null
}
export interface PlanSpeed extends PlanCategory {
  print_mm_s: number | null
  outer_wall_mm_s: number | null
  inner_wall_mm_s: number | null
  infill_mm_s: number | null
  first_layer_mm_s: number | null
  support_mm_s: number | null
  travel_mm_s: number | null
}
export interface PlanFlow extends PlanCategory {
  strategy: string
}
export interface PlanRetraction extends PlanCategory {
  distance_mm: number | null
  speed_mm_s: number | null
  z_hop_mm: number | null
}
export interface PlanFirstLayer extends PlanCategory {
  height_mm: number | null
  speed_mm_s: number | null
  line_width_mm: number | null
  fan_percent: number | null
}
export interface PlanAcceleration extends PlanCategory {
  value_mm_s2: number | null
  wall_mm_s2: number | null
  travel_mm_s2: number | null
}
export interface PlanMinLayerTime extends PlanCategory {
  seconds: number | null
}
export interface PlanMaterialSpecific extends PlanCategory {
  honoured: string[]
}
export interface PlanSlicerSpecific extends PlanCategory {
  requested_features: string[]
}

/**
 * The AI print plan. Every category is present in a completed response;
 * they are optional here because a failed attempt stores an empty plan.
 * Extra keys the backend may add are kept by the index signature.
 */
export interface PreparationPlan {
  model_validity?: PlanModelValidity
  orientation?: PlanOrientation
  bed_placement?: PlanBedPlacement
  layer_height?: PlanLayerHeight
  walls?: PlanWalls
  top_bottom?: PlanTopBottom
  infill?: PlanInfill
  supports?: PlanSupports
  support_interface?: PlanSupportInterface
  brim?: PlanBrim
  raft?: PlanRaft
  bed_adhesion?: PlanBedAdhesion
  temperature?: PlanTemperature
  cooling?: PlanCooling
  speed?: PlanSpeed
  flow?: PlanFlow
  retraction?: PlanRetraction
  first_layer?: PlanFirstLayer
  acceleration?: PlanAcceleration
  min_layer_time?: PlanMinLayerTime
  material_specific?: PlanMaterialSpecific
  slicer_specific?: PlanSlicerSpecific
  objective?: { intent: IntentPriorities }
  summary?: string
  confidence?: number | null
  [key: string]: unknown
}

/** One output format, as the printer's profile declares it. */
export interface PreparationOutputFormat {
  key: string
  format: string
  extension: string
  declared_by_profile: boolean
  advertised: boolean
  available: boolean
  /** Why the format cannot be produced here (backend wording). */
  reason: string
  /** Install/dependency guidance, e.g. the encoder's URL. */
  remedy: string
}

export interface PreparationSlice {
  slice_job_id: string
  status: string
  input_fingerprint: string | null
  error_code: string | null
  error_detail: string | null
}

/** A stored file that passed its own validation. */
export interface OutputArtifact {
  id: string
  filename: string
  format: string
  bytes: number
  checksum: string
  printer: string | null
  material: string | null
  slicer: string | null
  slicer_version: string | null
  configuration_fingerprint: string | null
  validation: Record<string, unknown>
  media_type: string
  created_at: string | null
}

export interface PreparationModelInfo {
  model_version_id: string
  model_id: string
  model_name: string | null
  filename: string | null
  format: string | null
  triangles: number | null
  bbox_mm: { x: number | null; y: number | null; z: number | null } | null
}

export interface PreparationPrinterInfo {
  printer_id: string
  name: string
  machine: string | null
  technology: string | null
  nozzle_diameter_mm: number
  firmware: string | null
  machine_profile_id: string
  machine_profile_version: number | null
}

export interface PreparationMaterialInfo {
  material_profile_id: string
  brand_sku: string
  vendor: string
  material: string | null
  family: string | null
}

/** One preparation attempt, in full. */
export interface PreparationJob {
  id: string
  status: PreparationStatus
  passed: boolean
  attempt: number
  max_attempts: number
  model_version_id: string
  printer_id: string
  material_profile_id: string
  model: PreparationModelInfo | null
  printer: PreparationPrinterInfo | null
  material: PreparationMaterialInfo | null
  prompt: string
  prompt_sha256: string
  raw_response: string | null
  provider: string | null
  model_identifier: string | null
  plan: PreparationPlan
  validation: ValidationResult
  normalisations: Normalisation[]
  rejected_reason: string | null
  input_sha256: string
  duration_ms: number
  output_formats: PreparationOutputFormat[]
  slice: PreparationSlice | null
  outputs: OutputArtifact[]
  created_at: string | null
  updated_at: string | null
}

export interface PreparationJobSummary {
  id: string
  status: PreparationStatus
  passed: boolean
  attempt: number
  max_attempts: number
  model_version_id: string
  printer_id: string
  material_profile_id: string
  rejected_reason: string | null
  prompt_sha256: string
  duration_ms: number
  created_at: string | null
  updated_at: string | null
}

export interface PreparationCreateRequest {
  model_version_id: string
  printer_id: string
  material_profile_id: string
  objective: PrintObjective
  user_request?: string
  output_format?: string | null
}

export interface PreparationReplanRequest {
  user_request?: string
  feedback?: string
  objective?: PrintObjective
}

export interface PreparationSliceRequest {
  objective?: PrintObjective
  output_format?: string | null
}

/** `POST /preparation/{id}/validate`: stored verdict vs today's measurement. */
export interface PreparationValidationResponse {
  job_id: string
  attempt: number
  stored: ValidationResult
  recheck: ValidationResult
}

// --- endpoints -----------------------------------------------------------

export const api = {
  register: (email: string, password: string, displayName: string) =>
    request<TokenResponse>('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password, display_name: displayName }),
    }),

  login: (email: string, password: string) =>
    request<TokenResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),

  me: () => request<User>('/auth/me'),

  machines: () => request<Machine[]>('/machines'),
  machineProfiles: () => request<MachineProfile[]>('/machine-profiles'),
  materials: () => request<Material[]>('/materials'),
  materialProfiles: () => request<MaterialProfile[]>('/material-profiles'),
  nozzles: () => request<Nozzle[]>('/nozzles'),

  createPrinter: (body: {
    name: string
    machine_profile_id: string
    nozzle_diameter_mm: number
    nozzle_material?: string
  }) => request<Printer>('/printers', { method: 'POST', body: JSON.stringify(body) }),

  printers: () => request<Printer[]>('/printers'),

  deletePrinter: (printerId: string) =>
    request<void>(`/printers/${printerId}`, { method: 'DELETE' }),

  formats: () => request<FormatCapability[]>('/models/formats'),
  models: () => request<ModelSummary[]>('/models'),

  upload: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{ model: ModelSummary; version: ModelVersion; deduplicated: boolean }>(
      '/models',
      { method: 'POST', body: form },
    )
  },

  analyze: (versionId: string, nozzleMm = 0.4) =>
    request<Analysis>(`/models/versions/${versionId}/analyze?nozzle_mm=${nozzleMm}`, {
      method: 'POST',
    }),

  transformModel: (versionId: string, body: ModelTransformBody) =>
    request<ModelTransformResponse>(`/models/versions/${versionId}/transform`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  modelFile: (versionId: string) =>
    download(`/models/versions/${versionId}/file`, 'model.stl'),

  slice: (body: {
    model_version_id: string
    printer_id: string
    material_profile_id: string
    objective: 'quality' | 'strength' | 'speed' | 'material' | 'balanced'
    /** Concrete slicer parameters from the AI apply step. */
    engine_settings_overrides?: Record<string, unknown>
    /** Parameters that must not be overwritten by the optimizer. */
    locked_parameters?: string[]
  }) => request<SliceJob>('/slice', { method: 'POST', body: JSON.stringify(body) }),

  jobs: () => request<SliceJob[]>('/slice'),
  job: (id: string) => request<SliceJob>(`/slice/${id}`),
  jobStages: (id: string) => request<StageRun[]>(`/slice/${id}/stages`),
  jobEvents: (id: string) => request<JobEvent[]>(`/slice/${id}/events`),
  jobValidation: (id: string) => request<ValidationResponse>(`/slice/${id}/validation`),
  jobFiles: (id: string) => request<GeneratedFile[]>(`/slice/${id}/files`),
  jobToolpath: (id: string) => request<Toolpath>(`/slice/${id}/toolpath`),
  technologies: () => request<Technology[]>('/technologies'),
  slicers: () => request<SlicerEngine[]>('/slicers'),

  aiHealth: () => request<AiHealth>('/ai/health'),

  aiPlan: (body: AiPlanRequest) =>
    request<AiPlan>('/ai/plan', { method: 'POST', body: JSON.stringify(body) }),

  aiApply: (body: AiApplyRequest) =>
    request<AiApply>('/ai/apply', { method: 'POST', body: JSON.stringify(body) }),

  aiRefine: (body: AiRefineRequest) =>
    request<AiPlan>('/ai/refine', { method: 'POST', body: JSON.stringify(body) }),

  aiDecisions: (limit = 50) =>
    request<{ decisions: RecordedDecision[] }>(`/ai/decisions?limit=${limit}`),

  temperatureRange: (printerId: string, materialProfileId: string) =>
    request<TemperatureRange>(
      `/temperature-range?printer_id=${printerId}&material_profile_id=${materialProfileId}`,
    ),

  aiOverride: (decisionId: string, reason: string) =>
    request<{ id: string; overridden: boolean; reason: string }>(
      `/ai/decisions/${decisionId}/override`,
      { method: 'POST', body: JSON.stringify({ reason }) },
    ),

  // --- print preparation ------------------------------------------------

  preparationCreate: (body: PreparationCreateRequest) =>
    request<PreparationJob>('/preparation', { method: 'POST', body: JSON.stringify(body) }),

  preparations: (query: { model_version_id?: string; limit?: number } = {}) => {
    const params = new URLSearchParams()
    if (query.model_version_id) params.set('model_version_id', query.model_version_id)
    if (query.limit) params.set('limit', String(query.limit))
    const suffix = params.toString()
    return request<PreparationJobSummary[]>(`/preparation${suffix ? `?${suffix}` : ''}`)
  },

  preparation: (jobId: string) => request<PreparationJob>(`/preparation/${jobId}`),

  preparationReplan: (jobId: string, body: PreparationReplanRequest) =>
    request<PreparationJob>(`/preparation/${jobId}/replan`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  preparationValidate: (jobId: string) =>
    request<PreparationValidationResponse>(`/preparation/${jobId}/validate`, {
      method: 'POST',
    }),

  preparationSlice: (jobId: string, body: PreparationSliceRequest) =>
    request<PreparationJob>(`/preparation/${jobId}/slice`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  preparationOutputs: (jobId: string) =>
    request<OutputArtifact[]>(`/preparation/${jobId}/outputs`),

  /** Authenticated download of one validated artifact (server-set filename). */
  preparationDownload: (jobId: string, artifact: OutputArtifact) =>
    download(`/preparation/${jobId}/outputs/${artifact.id}/download`, artifact.filename),

  /** Authenticated download of a legacy slice job file. */
  jobFileDownload: (jobId: string, file: GeneratedFile) =>
    download(`/slice/${jobId}/files/${file.id}/download`, file.filename),

  downloadUrl: (jobId: string, fileId: string) => `${BASE}/slice/${jobId}/files/${fileId}/download`,
}
