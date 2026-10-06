/**
 * Read-only helpers for the print-preparation flow.
 *
 * Nothing here decides anything about printing. The backend owns the plan,
 * the 22 checks, the output formats and every limit; this module only maps
 * what it returns onto labels, phases and user-facing error copy so the UI
 * never has to parse prose to work out what happened.
 */

import { ApiError } from './api'
import type { MachineProfile, PreparationJob } from './api'

/** What the workbench shows while an attempt exists or is in flight. */
export type PreparationPhase =
  | 'idle'
  | 'preparing'
  | 'analysing'
  | 'planning'
  | 'validating'
  | 'replanning'
  | 'ready'
  | 'rejected'
  | 'failed'

export const PHASE_LABELS: Record<PreparationPhase, string> = {
  idle: 'Not prepared',
  preparing: 'Preparing',
  analysing: 'Analysing',
  planning: 'Planning',
  validating: 'Validating',
  replanning: 'Re-planning',
  ready: 'Ready to slice',
  rejected: 'Rejected',
  failed: 'Failed',
}

/** Backend status -> display phase. Unknown statuses fail closed. */
const STATUS_PHASES: Record<string, PreparationPhase> = {
  queued: 'preparing',
  analysing: 'analysing',
  proposing: 'planning',
  validating: 'validating',
  completed: 'ready',
  rejected: 'rejected',
  failed: 'failed',
}

export function phaseOf(job: PreparationJob | null): PreparationPhase {
  if (!job) return 'idle'
  return STATUS_PHASES[job.status] ?? 'failed'
}

/** An error reduced to what the UI needs: a stable code plus safe copy. */
export interface PreparationErrorInfo {
  code: string
  message: string
  /** Guidance shown under the message. Never an internal path. */
  hint: string
  /** Which recovery control, if any, to offer. */
  action: 'signin' | 'replan' | 'change-format' | 'retry' | 'none'
  status: number
}

const ERROR_COPY: Record<
  string,
  { message: string; hint: string; action: PreparationErrorInfo['action'] }
> = {
  UNAUTHENTICATED: {
    message: 'Your session has expired. Sign in again to continue.',
    hint: '',
    action: 'signin',
  },
  NOT_FOUND: {
    message: 'That preparation or file no longer exists.',
    hint: '',
    action: 'none',
  },
  PERMISSION_DENIED: {
    message: 'You do not have access to that preparation.',
    hint: '',
    action: 'none',
  },
  RATE_LIMITED: {
    message: 'Too many preparation requests. Wait a moment and try again.',
    hint: '',
    action: 'retry',
  },
  PREPARATION_REJECTED: {
    message: 'Deterministic validation rejected this plan, so it cannot be sliced.',
    hint: 'Re-plan with the failing checks addressed.',
    action: 'replan',
  },
  PREPARATION_NOT_READY: {
    message: 'This preparation has not finished yet.',
    hint: 'Wait for the attempt to complete, then try again.',
    action: 'retry',
  },
  PREPARATION_FAILED: {
    message: 'This attempt failed before a plan was produced.',
    hint: 'Start a new preparation.',
    action: 'retry',
  },
  OUTPUT_FORMAT_UNSUPPORTED: {
    message: 'This printer does not declare the requested output format.',
    hint: 'Choose one of the formats its profile offers.',
    action: 'change-format',
  },
  OUTPUT_FORMAT_UNAVAILABLE: {
    message: 'This server cannot produce the requested output format yet.',
    hint: 'The encoder for it is not installed on this deployment.',
    action: 'change-format',
  },
  OUTPUT_FORMAT_REJECTED: {
    message: 'The generated file failed validation and was discarded.',
    hint: 'Nothing was stored. Slice again or choose another format.',
    action: 'retry',
  },
  OUTPUT_NOT_VALIDATED: {
    message: 'This file did not pass validation, so it cannot be downloaded.',
    hint: '',
    action: 'none',
  },
  INSUFFICIENT_INFORMATION: {
    message: 'The model did not have enough information to propose a plan.',
    hint: 'Add a more specific print objective and try again.',
    action: 'retry',
  },
  VALIDATION_FAILED: {
    message: 'The request was rejected as invalid.',
    hint: '',
    action: 'none',
  },
  REQUEST_VALIDATION_FAILED: {
    message: 'The request was rejected as invalid.',
    hint: '',
    action: 'none',
  },
  LLM_UNAVAILABLE: {
    message: 'The local model server is not reachable.',
    hint: 'Start it with `ollama serve`, or check OLLAMA_BASE_URL.',
    action: 'retry',
  },
  LLM_NOT_CONFIGURED: {
    message: 'The AI service is not configured on this server.',
    hint: '',
    action: 'none',
  },
  LLM_MODEL_MISSING: {
    message: 'The configured model is not installed on the server.',
    hint: '',
    action: 'none',
  },
  LLM_TIMEOUT: {
    message: 'The local model took too long to answer.',
    hint: 'Try again, or use a smaller model.',
    action: 'retry',
  },
  LLM_INVALID_OUTPUT: {
    message: 'The model returned an answer the server could not use.',
    hint: 'Rephrase the print objective and prepare again.',
    action: 'retry',
  },
  LLM_PROVIDER_ERROR: {
    message: 'The model backend returned an error.',
    hint: 'Check the provider logs, then try again.',
    action: 'retry',
  },
}

/** Reduce any thrown value to safe, code-driven copy for the UI. */
export function describePreparationError(error: unknown): PreparationErrorInfo {
  if (error instanceof ApiError) {
    const copy = ERROR_COPY[error.code]
    const backendHint =
      typeof error.details['hint'] === 'string' ? (error.details['hint'] as string) : ''
    const install =
      typeof error.details['install'] === 'string' ? (error.details['install'] as string) : ''
    return {
      code: error.code,
      message: copy?.message ?? error.message,
      hint: copy?.hint || backendHint || install || '',
      action: copy?.action ?? 'none',
      status: error.status,
    }
  }
  return {
    code: 'CLIENT_ERROR',
    message: 'Could not reach the server.',
    hint: '',
    action: 'retry',
    status: 0,
  }
}

/** `"layer_height" -> "Layer height"` for checklist categories. */
export function categoryLabel(key: string): string {
  const words = key.replace(/_/g, ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/**
 * Display names for the canonical parameter keys the validator emits
 * (`locked` / `overrides`). Presentation only: the key and the value both
 * come from the backend.
 */
const PARAMETER_LABELS: Record<string, string> = {
  layer_height_mm: 'Layer height',
  wall_count: 'Wall count',
  infill_density_percent: 'Infill density',
  infill_pattern: 'Infill pattern',
  top_bottom_layers: 'Top/bottom layers',
  top_layers: 'Top layers',
  bottom_layers: 'Bottom layers',
  support_enable: 'Supports',
  support_density_percent: 'Support density',
  support_angle_deg: 'Support angle',
  support_pattern: 'Support pattern',
  support_interface_enable: 'Support interface',
  support_interface_height_mm: 'Interface height',
  support_interface_density_percent: 'Interface density',
  adhesion_type: 'Bed adhesion',
  brim_width_mm: 'Brim width',
  nozzle_temperature_c: 'Nozzle temp',
  bed_temperature_c: 'Bed temp',
  first_layer_temperature_c: 'First-layer nozzle',
  first_layer_bed_temperature_c: 'First-layer bed',
  cooling_fan_percent: 'Cooling fan',
  min_layer_time_s: 'Min layer time',
  print_speed_mm_s: 'Print speed',
  travel_speed_mm_s: 'Travel speed',
  first_layer_speed_mm_s: 'First-layer speed',
  outer_wall_speed_mm_s: 'Outer wall speed',
  inner_wall_speed_mm_s: 'Inner wall speed',
  infill_speed_mm_s: 'Infill speed',
  support_speed_mm_s: 'Support speed',
  acceleration_mm_s2: 'Acceleration',
  wall_acceleration_mm_s2: 'Wall acceleration',
  travel_acceleration_mm_s2: 'Travel acceleration',
  retraction: 'Retraction',
  z_hop_mm: 'Z-hop',
}

export function parameterLabel(key: string): string {
  return PARAMETER_LABELS[key] ?? categoryLabel(key)
}

/** Render any backend-supplied value for display. Never interprets it. */
export function formatPlanValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'on' : 'off'
  if (typeof value === 'number') return String(value)
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map((item) => formatPlanValue(item)).join(', ')
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `${parameterLabel(key)} ${formatPlanValue(item)}`)
      .join(', ')
  }
  return String(value)
}

/** Display name for a format key the backend reported. */
export const FORMAT_NAMES: Record<string, string> = {
  gcode: 'G-code',
  gx: 'GX',
  bgcode: 'BGCODE',
  '3mf': '3MF',
  g: 'G',
}

export function formatName(key: string): string {
  return FORMAT_NAMES[key] ?? key.toUpperCase()
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} kB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

/** First 12 characters of a hex digest, for compact display. */
export function shortHash(hash: string | null | undefined): string {
  if (!hash) return '—'
  return hash.length > 16 ? `${hash.slice(0, 12)}…` : hash
}

/**
 * Output formats this printer's *profile* declares, read straight from the
 * verified machine profile. An absent or malformed value yields an empty
 * list — the frontend never supplies a format the backend did not.
 */
export function declaredOutputFormats(profile: MachineProfile | null): string[] {
  const raw = profile?.machine_restrictions?.['output_formats']
  if (typeof raw === 'string') return [raw]
  if (Array.isArray(raw)) {
    return raw.filter((value): value is string => typeof value === 'string')
  }
  return []
}

/** Hand downloaded bytes to the browser. The filename came from the server. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
