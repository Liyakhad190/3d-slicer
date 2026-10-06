/**
 * The preparation state machine for one (model, printer, material) triple.
 *
 * The backend owns every decision; this hook only tracks which attempt we are
 * showing, refuses to show a result that belongs to inputs the user has since
 * changed, and forwards the user's actions to the API. It holds no print
 * settings of its own — plan, checks, formats and artifacts all arrive from
 * the server and leave with it.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from './api'
import type {
  OutputArtifact,
  PreparationJob,
  PreparationValidationResponse,
  PrintObjective,
} from './api'
import { describePreparationError, phaseOf, saveBlob } from './preparation'
import type { PreparationErrorInfo, PreparationPhase } from './preparation'

export interface PreparationInputs {
  modelVersionId: string | null
  printerId: string
  materialProfileId: string
}

export interface PrepareArgs {
  objective: PrintObjective
  userRequest: string
}

export interface ReplanArgs extends PrepareArgs {
  feedback: string
}

export interface PreparationController {
  job: PreparationJob | null
  phase: PreparationPhase
  /** The loaded attempt belongs to inputs that have since changed. */
  stale: boolean
  /** A previous attempt was discarded because the inputs changed. */
  invalidated: boolean
  /** Label of the request in flight. Real work only — never a fake %. */
  busy: string | null
  error: PreparationErrorInfo | null
  outputs: OutputArtifact[]
  formatKey: string | null
  setFormatKey: (key: string) => void
  recheck: PreparationValidationResponse | null
  prepare: (args: PrepareArgs) => Promise<string | null>
  replan: (args: ReplanArgs) => Promise<string | null>
  validate: () => Promise<void>
  /** Returns the slice job id when, and only when, slicing completed. */
  slice: (args: { objective: PrintObjective }) => Promise<string | null>
  download: (artifact: OutputArtifact) => Promise<string | null>
  clearError: () => void
  reset: () => void
}

function composeKey(inputs: PreparationInputs): string {
  return `${inputs.modelVersionId ?? ''}|${inputs.printerId}|${inputs.materialProfileId}`
}

/** First format the server says it can actually produce, else none. */
function pickFormat(job: PreparationJob, preferred: string | null): string | null {
  const usable = job.output_formats.filter((format) => format.available)
  const kept = preferred ? usable.find((format) => format.key === preferred) : undefined
  return (kept ?? usable[0])?.key ?? null
}

export function usePreparation(inputs: PreparationInputs): PreparationController {
  const modelVersionId = inputs.modelVersionId
  const printerId = inputs.printerId
  const materialProfileId = inputs.materialProfileId
  const key = composeKey({ modelVersionId, printerId, materialProfileId })

  const [job, setJob] = useState<PreparationJob | null>(null)
  const [jobKey, setJobKey] = useState<string | null>(null)
  const [outputs, setOutputs] = useState<OutputArtifact[]>([])
  const [formatKey, setFormatKey] = useState<string | null>(null)
  const [recheck, setRecheck] = useState<PreparationValidationResponse | null>(null)
  const [error, setError] = useState<PreparationErrorInfo | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [invalidated, setInvalidated] = useState(false)

  const keyRef = useRef(key)
  const jobRef = useRef<PreparationJob | null>(null)
  const recordedJobIdRef = useRef<string | null>(null)
  const sequenceRef = useRef(0)
  jobRef.current = job

  const applyJob = useCallback((next: PreparationJob | null, atKey: string) => {
    if (keyRef.current !== atKey) return // an input changed mid-flight
    jobRef.current = next
    setJob(next)
    setJobKey(next ? atKey : null)
    setOutputs(next?.outputs ?? [])
    setFormatKey((current) => (next ? pickFormat(next, current) : null))
    setRecheck(null)
    setInvalidated(false)
  }, [])

  // --- invalidation: an input change discards the previous attempt --------
  useEffect(() => {
    if (keyRef.current === key) return
    const hadJob = jobRef.current !== null
    keyRef.current = key
    sequenceRef.current += 1
    recordedJobIdRef.current = null
    jobRef.current = null
    setJob(null)
    setJobKey(null)
    setOutputs([])
    setFormatKey(null)
    setRecheck(null)
    setError(null)
    setBusy(null)
    setInvalidated((wasSet) => wasSet || hadJob)
  }, [key])

  /** Run one request, owning busy/error, discarding results from old inputs. */
  const run = useCallback(async <T,>(label: string, action: () => Promise<T>): Promise<T | null> => {
    const sequence = ++sequenceRef.current
    const startedFor = keyRef.current
    setBusy(label)
    setError(null)
    try {
      return await action()
    } catch (caught) {
      if (sequence === sequenceRef.current) {
        setError(describePreparationError(caught))
        recordedJobIdRef.current = recordedJobId(caught)
      }
      return null
    } finally {
      if (sequence === sequenceRef.current && keyRef.current === startedFor) setBusy(null)
    }
  }, [])

  /** The attempt a provider failure still recorded, read back for display. */
  const fetchRecordedJob = useCallback(async (atKey: string): Promise<void> => {
    const jobId = recordedJobIdRef.current
    recordedJobIdRef.current = null
    if (!jobId) return
    try {
      applyJob(await api.preparation(jobId), atKey)
    } catch {
      /* the error already on screen is the one that matters */
    }
  }, [applyJob])

  const prepare = useCallback(
    async (args: PrepareArgs): Promise<string | null> => {
      const startedFor = keyRef.current
      const created = await run<PreparationJob>('Preparing the print…', () =>
        api.preparationCreate({
          model_version_id: modelVersionId ?? '',
          printer_id: printerId,
          material_profile_id: materialProfileId,
          objective: args.objective,
          ...(args.userRequest.trim() ? { user_request: args.userRequest.trim() } : {}),
          ...(formatKey ? { output_format: formatKey } : {}),
        }),
      )
      if (created) {
        applyJob(created, startedFor)
        return created.id
      }
      await fetchRecordedJob(startedFor)
      return null
    },
    [applyJob, fetchRecordedJob, formatKey, modelVersionId, printerId, materialProfileId, run],
  )

  const replan = useCallback(
    async (args: ReplanArgs): Promise<string | null> => {
      const current = jobRef.current
      if (!current) return null
      const startedFor = keyRef.current
      const next = await run<PreparationJob>('Re-planning…', () =>
        api.preparationReplan(current.id, {
          user_request: args.userRequest.trim(),
          feedback: args.feedback.trim(),
          objective: args.objective,
        }),
      )
      if (next) {
        applyJob(next, startedFor)
        return next.id
      }
      await fetchRecordedJob(startedFor)
      return null
    },
    [applyJob, fetchRecordedJob, run],
  )

  const validate = useCallback(async (): Promise<void> => {
    const current = jobRef.current
    if (!current) return
    const startedFor = keyRef.current
    const result = await run<PreparationValidationResponse>('Re-checking the plan…', () =>
      api.preparationValidate(current.id),
    )
    if (result && keyRef.current === startedFor) setRecheck(result)
  }, [run])

  const slice = useCallback(
    async (args: { objective: PrintObjective }): Promise<string | null> => {
      const current = jobRef.current
      if (!current) return null
      const startedFor = keyRef.current
      const sliced = await run<PreparationJob>('Slicing with the real engine…', () =>
        api.preparationSlice(current.id, {
          objective: args.objective,
          ...(formatKey ? { output_format: formatKey } : {}),
        }),
      )
      if (!sliced) return null
      applyJob(sliced, startedFor)

      const sliceRef = sliced.slice
      if (!sliceRef || sliceRef.status !== 'COMPLETED') {
        // Blocked or failed: reported as-is, never as a printable artifact.
        if (sliceRef && keyRef.current === startedFor) {
          setError({
            code: sliceRef.error_code ?? 'OUTPUT_BLOCKED',
            message:
              sliceRef.error_detail ??
              'The toolpath was blocked by validation, so no file was produced.',
            hint: '',
            action: 'none',
            status: 201,
          })
        }
        return null
      }

      try {
        const list = await api.preparationOutputs(sliced.id)
        if (keyRef.current === startedFor) setOutputs(list)
      } catch {
        /* the attempt payload already carries the same artifacts */
      }
      return sliceRef.slice_job_id
    },
    [applyJob, formatKey, run],
  )

  const download = useCallback(
    async (artifact: OutputArtifact): Promise<string | null> => {
      const current = jobRef.current
      if (!current) return null
      const filename = await run<string>('Downloading…', async () => {
        const file = await api.preparationDownload(current.id, artifact)
        saveBlob(file.blob, file.filename)
        return file.filename
      })
      return filename
    },
    [run],
  )

  const clearError = useCallback(() => setError(null), [])

  const reset = useCallback(() => {
    sequenceRef.current += 1
    recordedJobIdRef.current = null
    jobRef.current = null
    setJob(null)
    setJobKey(null)
    setOutputs([])
    setFormatKey(null)
    setRecheck(null)
    setError(null)
    setBusy(null)
    setInvalidated(false)
  }, [])

  return {
    job,
    phase: phaseOf(job),
    stale: job !== null && jobKey !== key,
    invalidated,
    busy,
    error,
    outputs,
    formatKey,
    setFormatKey,
    recheck,
    prepare,
    replan,
    validate,
    slice,
    download,
    clearError,
    reset,
  }
}

/** `details.job_id` on a provider-failure envelope: the attempt that exists. */
function recordedJobId(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  const jobId = error.details['job_id']
  return typeof jobId === 'string' && jobId ? jobId : null
}
