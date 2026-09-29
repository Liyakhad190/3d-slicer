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
  build_volume: { x: number; y: number; z: number }
  nozzle_diameters: number[]
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

  slice: (body: {
    model_version_id: string
    printer_id: string
    material_profile_id: string
    objective: 'quality' | 'strength' | 'speed' | 'material' | 'balanced'
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

  downloadUrl: (jobId: string, fileId: string) => `${BASE}/slice/${jobId}/files/${fileId}/download`,
}
