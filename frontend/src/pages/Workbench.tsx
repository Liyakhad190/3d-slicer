/**
 * The slicing workbench: the product's main screen.
 *
 * Left: model, printer, material, nozzle, objective.
 * Centre: the 3D viewport, switchable between the model and the real toolpath.
 * Right: printability, validation verdict, stages, risks, download.
 *
 * Every number shown is one the backend measured. Nothing is estimated here.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AiAssistant } from '../components/AiAssistant'
import { PreparationPanel } from '../components/preparation/PreparationPanel'
import { Viewport } from '../components/Viewport'
import { usePreparation } from '../lib/usePreparation'
import {
  ApiError,
  api,
  type Analysis,
  type AiApply,
  type GeneratedFile,
  type JobEvent,
  type Machine,
  type MachineProfile,
  type Material,
  type MaterialProfile,
  type ModelTransformBody,
  type ModelVersion,
  type Printer,
  type SlicerEngine,
  type SliceJob,
  type StageRun,
  type Technology,
  type TemperatureRange,
  type Toolpath,
  type ValidationResponse,
} from '../lib/api'

type Objective = 'quality' | 'strength' | 'speed' | 'material' | 'balanced'

const OBJECTIVES: Array<{ key: Objective; label: string; hint: string }> = [
  { key: 'balanced', label: 'Balanced', hint: 'No extreme trade-offs' },
  { key: 'quality', label: 'Quality', hint: 'Finer layers, slower' },
  { key: 'strength', label: 'Strength', hint: 'More walls and infill' },
  { key: 'speed', label: 'Speed', hint: 'Thicker layers, minimal shell' },
  { key: 'material', label: 'Min material', hint: 'Least filament' },
]

/** Human-readable labels for known slicer parameter keys. */
const PARAM_LABELS: Record<string, string> = {
  layer_height: 'Layer height',
  infill_density: 'Infill density',
  infill_pattern: 'Infill pattern',
  wall_count: 'Wall count',
  wall_line_count: 'Wall count',
  top_layers: 'Top layers',
  bottom_layers: 'Bottom layers',
  support_enable: 'Supports',
  support_type: 'Support type',
  print_speed: 'Print speed',
  travel_speed: 'Travel speed',
  retraction_enable: 'Retraction',
  retraction_amount: 'Retraction amount',
  material_print_temperature: 'Nozzle temp',
  material_bed_temperature: 'Bed temp',
  cooling_fan_enabled: 'Fan',
  fan_speed: 'Fan speed',
}

function metric(value: number | null | undefined, unit: string, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value.toFixed(digits)} ${unit}`
}

function severityClass(severity: string): string {
  if (severity === 'critical') return 'bg-rose-950/60 text-rose-300 border-rose-900'
  if (severity === 'warning') return 'bg-amber-950/50 text-amber-300 border-amber-900'
  return 'bg-slate-900/60 text-slate-400 border-slate-800'
}

export default function Workbench(): JSX.Element {
  const navigate = useNavigate()
  // catalog
  const [machines, setMachines] = useState<Machine[]>([])
  const [profiles, setProfiles] = useState<MachineProfile[]>([])
  const [materials, setMaterials] = useState<Material[]>([])
  const [materialProfiles, setMaterialProfiles] = useState<MaterialProfile[]>([])
  const [printers, setPrinters] = useState<Printer[]>([])
  const [technologies, setTechnologies] = useState<Technology[]>([])
  const [slicers, setSlicers] = useState<SlicerEngine[]>([])

  // selections
  const [file, setFile] = useState<File | null>(null)
  const [version, setVersion] = useState<ModelVersion | null>(null)
  const [aspectLocked, setAspectLocked] = useState(true)
  const [scalePercent, setScalePercent] = useState({ x: '100', y: '100', z: '100' })
  const [targetDimensions, setTargetDimensions] = useState({ x: '', y: '', z: '' })
  const [dimensionMode, setDimensionMode] = useState<'percent' | 'target'>('percent')
  const [transformBusy, setTransformBusy] = useState(false)
  const [modelChanged, setModelChanged] = useState(false)
  const [removingPrinterId, setRemovingPrinterId] = useState<string | null>(null)
  const [printerId, setPrinterId] = useState('')
  const [materialProfileId, setMaterialProfileId] = useState('')
  const [objective, setObjective] = useState<Objective>('balanced')

  // results
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [job, setJob] = useState<SliceJob | null>(null)
  const [stages, setStages] = useState<StageRun[]>([])
  const [events, setEvents] = useState<JobEvent[]>([])
  const [validation, setValidation] = useState<ValidationResponse | null>(null)
  const [files, setFiles] = useState<GeneratedFile[]>([])
  const [toolpath, setToolpath] = useState<Toolpath | null>(null)

  // AI-to-slice: concrete parameters from /ai/apply, forwarded to /slice
  const [aiParameters, setAiParameters] = useState<Record<string, unknown> | null>(null)
  const [aiApplyResult, setAiApplyResult] = useState<AiApply | null>(null)
  const [aiParamsAccepted, setAiParamsAccepted] = useState(false)

  const [mode, setMode] = useState<'model' | 'toolpath'>('model')
  const [maxLayer, setMaxLayer] = useState(0)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Free-text print request forwarded to the preparation backend.
  const [userRequest, setUserRequest] = useState('')

  // Deterministic safe-temperature resolution for the selected pair.
  const [tempRange, setTempRange] = useState<TemperatureRange | null>(null)
  const [tempLoading, setTempLoading] = useState(false)

  // The preparation flow: plan + the 22 checks + slicing, all backend-owned.
  const prep = usePreparation({
    modelVersionId: version?.id ?? null,
    printerId,
    materialProfileId,
  })

  useEffect(() => {
    setAiParameters(null)
    setAiApplyResult(null)
    setAiParamsAccepted(false)
  }, [version?.id, printerId, materialProfileId])

  useEffect(() => {
    if (prep.job && analysis && prep.job.input_sha256 === analysis.input_sha256) {
      setModelChanged(false)
    }
  }, [prep.job, analysis])

  useEffect(() => {
    const dims = (analysis?.metrics.dimensions_mm ?? {}) as Record<string, number>
    if (dims.x === undefined || dims.y === undefined || dims.z === undefined) return
    setTargetDimensions({ x: dims.x.toFixed(2), y: dims.y.toFixed(2), z: dims.z.toFixed(2) })
  }, [analysis])

  function updatePercent(axis: 'x' | 'y' | 'z', value: string): void {
    setScalePercent((current) =>
      aspectLocked ? { x: value, y: value, z: value } : { ...current, [axis]: value },
    )
  }

  function updateTargetDimension(axis: 'x' | 'y' | 'z', value: string): void {
    const dims = (analysis?.metrics.dimensions_mm ?? {}) as Record<string, number>
    if (!aspectLocked || !dims[axis] || !dims.x || !dims.y || !dims.z || !Number(value)) {
      setTargetDimensions((current) => ({ ...current, [axis]: value }))
      return
    }
    const ratio = Number(value) / dims[axis]
    setTargetDimensions({
      x: (dims.x * ratio).toFixed(2),
      y: (dims.y * ratio).toFixed(2),
      z: (dims.z * ratio).toFixed(2),
    })
  }

  /**
   * Send a transform, adopt the bytes the backend actually produced, and clear
   * every downstream result. One place, because a transform invalidates the
   * slice job, its artifacts, the toolpath and the preparation, and missing any
   * one of them would leave the screen showing a stale result next to new
   * geometry.
   */
  async function runTransform(body: ModelTransformBody): Promise<void> {
    if (!version || transformBusy) return
    setTransformBusy(true)
    setError(null)
    try {
      const result = await api.transformModel(version.id, body)
      const transformedFile = await api.modelFile(version.id)
      setVersion(result.version)
      setAnalysis(result.analysis)
      setFile(new File([transformedFile.blob], transformedFile.filename, { type: transformedFile.mediaType }))
      setJob(null)
      setValidation(null)
      setFiles([])
      setToolpath(null)
      setStages([])
      setEvents([])
      setMode('model')
      setAiParameters(null)
      setAiApplyResult(null)
      setAiParamsAccepted(false)
      prep.reset()
      setModelChanged(true)
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? `${caught.message}${Object.keys(caught.details).length ? ` ${JSON.stringify(caught.details)}` : ''}`
          : 'Could not transform the model.',
      )
    } finally {
      setTransformBusy(false)
    }
  }

  async function applyModelTransform(reset = false): Promise<void> {
    if (!version || transformBusy) return
    if (reset) {
      await runTransform({ reset: true })
      return
    }
    const dims = (analysis?.metrics.dimensions_mm ?? {}) as { x: number; y: number; z: number }
    const targetX = Number(targetDimensions.x)
    const lockedRatio = aspectLocked && dims.x ? targetX / dims.x : null
    await runTransform(
      dimensionMode === 'target'
        ? {
            target_dimensions_mm: {
              x: targetX,
              y: lockedRatio ? dims.y * lockedRatio : Number(targetDimensions.y),
              z: lockedRatio ? dims.z * lockedRatio : Number(targetDimensions.z),
            },
            preserve_aspect_ratio: aspectLocked,
          }
        : {
            scale_percent: {
              x: Number(scalePercent.x),
              y: Number(scalePercent.y),
              z: Number(scalePercent.z),
            },
            preserve_aspect_ratio: aspectLocked,
          },
    )
  }

  // --- load the catalog -------------------------------------------------
  useEffect(() => {
    void (async () => {
      try {
        const [m, p, mat, mp, pr, tech, sl] = await Promise.all([
          api.machines(),
          api.machineProfiles(),
          api.materials(),
          api.materialProfiles(),
          api.printers(),
          api.technologies(),
          api.slicers(),
        ])
        setMachines(m)
        setProfiles(p)
        setMaterials(mat)
        setMaterialProfiles(mp)
        setPrinters(pr)
        setTechnologies(tech)
        setSlicers(sl)
        if (pr.length > 0) {
          setPrinterId(pr.find((printer) => printer.is_default)?.id ?? pr[0]?.id ?? '')
        }
        if (mp.length > 0) setMaterialProfileId(mp[0]?.id ?? '')
      } catch (caught) {
        setError(caught instanceof ApiError ? caught.message : 'Could not load the catalogue.')
      }
    })()
  }, [])

  const selectedPrinter = useMemo(
    () => printers.find((p) => p.id === printerId) ?? null,
    [printers, printerId],
  )
  const selectedProfile = useMemo(
    () => profiles.find((p) => p.id === selectedPrinter?.machine_profile_id) ?? null,
    [profiles, selectedPrinter],
  )
  const machineForProfile = useMemo(
    () => machines.find((m) => m.id === selectedProfile?.machine_id) ?? null,
    [machines, selectedProfile],
  )
  const selectedMaterialProfile = useMemo(
    () => materialProfiles.find((p) => p.id === materialProfileId) ?? null,
    [materialProfiles, materialProfileId],
  )
  const materialFamily = useMemo(
    () => materials.find((m) => m.id === selectedMaterialProfile?.material_id) ?? null,
    [materials, selectedMaterialProfile],
  )

  const nozzleMm = Number(selectedPrinter?.nozzle_diameter_mm ?? 0.4)

  const modelDims = (analysis?.metrics.dimensions_mm as { x: number; y: number; z: number } | undefined) ?? null
  const isTinyModel = Boolean(
    modelDims &&
    Math.max(modelDims.x, modelDims.y, modelDims.z) < 5 &&
    Math.max(modelDims.x, modelDims.y, modelDims.z) > 0,
  )
  const isOversizedModel = Boolean(
    modelDims &&
    selectedProfile?.build_volume &&
    (modelDims.x > selectedProfile.build_volume.x ||
     modelDims.y > selectedProfile.build_volume.y ||
     modelDims.z > selectedProfile.build_volume.z),
  )

  async function fitModelToPrinter(targetRatio = 0.70): Promise<void> {
    if (!version || !analysis || transformBusy) return
    const dims = analysis.metrics.dimensions_mm as { x: number; y: number; z: number } | undefined
    if (!dims || !dims.x || !dims.y || !dims.z) return
    const volume = selectedProfile?.build_volume ?? { x: 220, y: 220, z: 250 }

    const factor = Math.min(
      (volume.x * targetRatio) / dims.x,
      (volume.y * targetRatio) / dims.y,
      (volume.z * targetRatio) / dims.z,
    )
    if (!Number.isFinite(factor) || factor <= 0) return

    // Sent as a percentage rather than a target size: the percentage is the
    // exact factor the fit computed, whereas a target size would be re-derived
    // from dimensions the backend reports rounded, and the rounding makes the
    // three axes differ enough to trip the aspect-ratio lock.
    const pct = Number((factor * 100).toFixed(4))
    await runTransform({
      scale_percent: { x: pct, y: pct, z: pct },
      preserve_aspect_ratio: true,
    })
  }

  /**
   * The meters case: the file's numbers are metres, so the part is 1000x
   * smaller than intended. Expressed as a percentage so the backend keeps one
   * exact factor on every axis.
   */
  async function scaleMetersToMillimeters(): Promise<void> {
    if (!version || !analysis || transformBusy) return
    await runTransform({
      scale_percent: { x: 100000, y: 100000, z: 100000 },
      preserve_aspect_ratio: true,
    })
  }

  // Resolve validated temperatures whenever the printer/material pair changes.
  // The backend computes the intersection; the UI never clamps or guesses.
  useEffect(() => {
    if (!printerId || !materialProfileId) {
      setTempRange(null)
      return
    }
    setTempLoading(true)
    api
      .temperatureRange(printerId, materialProfileId)
      .then(setTempRange)
      .catch(() => setTempRange(null))
      .finally(() => setTempLoading(false))
  }, [printerId, materialProfileId])

  const canPrepare = Boolean(version && printerId && materialProfileId)

  async function removePrinterFromWorkbench(printer: Printer): Promise<void> {
    if (!window.confirm(`Remove "${printer.name}" from this printer list? Existing jobs will be kept.`)) {
      return
    }
    setRemovingPrinterId(printer.id)
    setError(null)
    try {
      await api.deletePrinter(printer.id)
      const remaining = printers.filter((item) => item.id !== printer.id)
      setPrinters(remaining)
      if (printerId === printer.id) {
        const next = remaining.find((item) => item.is_default) ?? remaining[0]
        setPrinterId(next?.id ?? '')
        setJob(null)
        setValidation(null)
        setFiles([])
        setToolpath(null)
        setStages([])
        setEvents([])
        setMode('model')
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not remove this printer.')
    } finally {
      setRemovingPrinterId(null)
    }
  }

  /** Why the prepared slice action is unavailable, or null when it is not. */
  const sliceBlockedReason: string | null = !version
    ? 'Upload a model to enable slicing.'
    : !materialProfileId
      ? 'Select a material profile.'
      : selectedProfile && selectedProfile.validation_status !== 'validated'
        ? selectedProfile.validation_reason
          ? `Profile not validated: ${selectedProfile.validation_reason}`
          : 'This machine profile has not been bench-validated, so the platform will refuse to generate G-code for it.'
        : null

  /**
   * Slice through the accepted plan, then read back the same stages, events,
   * validation, files and toolpath the direct slice flow shows in Result.
   */
  async function slicePrepared(): Promise<void> {
    const sliceJobId = await prep.slice({ objective })
    if (!sliceJobId) return
    try {
      const [s, e, v, f, created] = await Promise.all([
        api.jobStages(sliceJobId),
        api.jobEvents(sliceJobId),
        api.jobValidation(sliceJobId),
        api.jobFiles(sliceJobId),
        api.job(sliceJobId),
      ])
      setStages(s)
      setEvents(e)
      setValidation(v)
      setFiles(f)
      setJob(created)
      if (created.status === 'COMPLETED') {
        const path = await api.jobToolpath(sliceJobId)
        setToolpath(path)
        setMaxLayer(path.layer_count)
        setMode('toolpath')
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not load the slice result.')
    }
  }

  // --- upload + analyse --------------------------------------------------
  // Geometry analysis depends on the fitted nozzle (extrusion width, thin
  // walls). Track the last analysed pair so a printer/nozzle change re-runs it.
  const analysedKey = useRef<string | null>(null)

  const analyseNow = useCallback(
    async (versionId: string, nozzle: number): Promise<void> => {
      const key = `${versionId}:${nozzle}`
      if (analysedKey.current === key) return
      analysedKey.current = key
      try {
        setAnalysis(await api.analyze(versionId, nozzle))
      } catch {
        /* keep the previous analysis; the error is surfaced on upload */
      }
    },
    [],
  )

  useEffect(() => {
    if (!version) return
    void analyseNow(version.id, nozzleMm)
  }, [version, nozzleMm, analyseNow])

  const onPickFile = useCallback(
    async (picked: File) => {
      setBusy('Uploading and analysing…')
      setError(null)
      try {
        const upload = await api.upload(picked)
        const [modelFile, modelAnalysis] = await Promise.all([
          api.modelFile(upload.version.id),
          api.analyze(upload.version.id, nozzleMm),
        ])
        setFile(new File([modelFile.blob], modelFile.filename, { type: modelFile.mediaType }))
        setVersion(upload.version)
        setModelChanged(false)
        setJob(null)
        setValidation(null)
        setFiles([])
        setToolpath(null)
        setMode('model')
        analysedKey.current = `${upload.version.id}:${nozzleMm}`
        setAnalysis(modelAnalysis)
      } catch (caught) {
        setError(caught instanceof ApiError ? caught.message : 'Upload or analysis failed.')
        setFile(null)
        setVersion(null)
      } finally {
        setBusy(null)
      }
    },
    [nozzleMm],
  )

  // --- accept the assistant's suggestion ---------------------------------
  /**
   * Called when the user clicks "Use these settings" in the AI assistant.
   *
   * Three things happen:
   * 1. The objective radio is updated to match the dominant priority.
   * 2. The concrete engine parameters from /ai/apply are stored so they can be
   *    forwarded to POST /slice as `engine_settings_overrides`.
  * 3. The accepted settings become active for the next validated slice.
   *
   * Nothing is sliced until the user explicitly presses Slice.
   */
  const adoptSuggestedObjective = useCallback((parameters: Record<string, unknown>, plan: AiApply) => {
    const map: Record<AiApply['dominant_priority'], Objective> = {
      strength: 'strength',
      speed: 'speed',
      surface_quality: 'quality',
      material_efficiency: 'material',
      balanced: 'balanced',
    }
    setObjective(map[plan.dominant_priority])
    setAiParameters(parameters)
    setAiApplyResult(plan)
    setAiParamsAccepted(true)
  }, [])

  // --- run the pipeline --------------------------------------------------
  const runSlice = useCallback(async () => {
    if (!version || !printerId || !materialProfileId) {
      setError('Select a model, a printer and a material first.')
      return
    }
    setBusy('Slicing with a real engine…')
    setError(null)
    setValidation(null)
    setFiles([])
    setToolpath(null)
    setStages([])
    setEvents([])
    try {
      // If the user accepted AI-generated parameters, forward them as concrete
      // engine overrides. The backend planner still validates them against the
      // machine profile before passing them to CuraEngine.
      const engineOverrides = aiParamsAccepted && aiParameters ? aiParameters : undefined
      const lockedParams =
        aiParamsAccepted && aiParameters ? Object.keys(aiParameters) : undefined

      const created = await api.slice({
        model_version_id: version.id,
        printer_id: printerId,
        material_profile_id: materialProfileId,
        objective,
        ...(engineOverrides ? { engine_settings_overrides: engineOverrides } : {}),
        ...(lockedParams ? { locked_parameters: lockedParams } : {}),
      })
      setJob(created)
      const [s, e, v, f] = await Promise.all([
        api.jobStages(created.id),
        api.jobEvents(created.id),
        api.jobValidation(created.id),
        api.jobFiles(created.id),
      ])
      setStages(s)
      setEvents(e)
      setValidation(v)
      setFiles(f)
      if (created.status === 'COMPLETED') {
        const path = await api.jobToolpath(created.id)
        setToolpath(path)
        setMaxLayer(path.layer_count)
        setMode('toolpath')
      }
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? `${caught.message}${Object.keys(caught.details).length ? ` ${JSON.stringify(caught.details)}` : ''}`
          : 'Slicing failed.',
      )
    } finally {
      setBusy(null)
    }
  }, [version, printerId, materialProfileId, objective, aiParameters, aiParamsAccepted])

  // --- derived metrics ----------------------------------------------------
  const metrics = (analysis?.metrics ?? {}) as Record<string, number | string | boolean>
  const dimensions = (metrics.dimensions_mm ?? {}) as Record<string, number>
  const score = analysis?.printability_score ?? null
  const risk = analysis?.risk_level ?? 'unknown'
  const printable = job?.status === 'COMPLETED'
  const blocked = job?.status === 'BLOCKED'

  return (
    <div className="workbench-grid grid min-h-full grid-cols-1 gap-4 xl:h-full xl:min-h-0 xl:grid-cols-[minmax(260px,300px)_minmax(360px,1fr)_minmax(300px,350px)]">
      {/* ------------------------------ left ------------------------------ */}
      <div className="flex min-h-0 flex-col gap-4 overflow-auto">
        <section className="panel">
          <h2 className="panel-title">Model</h2>
          <div className="space-y-3 p-4">
            <label className="block">
              <span className="field-label">STL / OBJ / 3MF / PLY / GLB</span>
              <input
                type="file"
                accept=".stl,.obj,.3mf,.ply,.amf,.glb,.gltf"
                onChange={(e) => {
                  const picked = e.target.files?.[0]
                  if (picked) void onPickFile(picked)
                }}
                className="w-full text-xs text-slate-400 file:mr-3 file:rounded file:border-0
                           file:bg-slate-800 file:px-3 file:py-1.5 file:text-slate-200"
              />
            </label>
            {version && (
              <dl className="space-y-1 text-xs text-slate-400">
                <Row label="File" value={version.original_filename} />
                <Row label="Format" value={version.format} />
                <Row label="Size" value={`${(version.byte_size / 1024).toFixed(1)} kB`} />
                <Row label="Triangles" value={(version.triangle_count ?? 0).toLocaleString()} />
                <Row label="X dimension" value={metric(dimensions.x as number, 'mm')} />
                <Row label="Y dimension" value={metric(dimensions.y as number, 'mm')} />
                <Row label="Z dimension" value={metric(dimensions.z as number, 'mm')} />
              </dl>
            )}

            {isTinyModel && (
              <div className="mt-3 rounded-md border border-amber-500/40 bg-amber-950/30 p-2.5 text-xs text-amber-200">
                <div className="flex items-start gap-2">
                  <span className="text-base leading-none">🔍</span>
                  <div className="space-y-1.5 flex-1">
                    <div className="font-semibold text-amber-100">
                      Model is very small ({dimensions.x ? Number(dimensions.x).toFixed(2) : '0'} × {dimensions.y ? Number(dimensions.y).toFixed(2) : '0'} × {dimensions.z ? Number(dimensions.z).toFixed(2) : '0'} mm)
                    </div>
                    <div className="text-[11px] text-amber-300/90 leading-relaxed">
                      This model was likely exported in meters instead of millimeters. Real slicing software scales it to fit inside the printer.
                    </div>
                    <div className="flex flex-wrap gap-2 pt-1">
                      <button
                        type="button"
                        disabled={transformBusy}
                        onClick={() => void fitModelToPrinter(0.70)}
                        className="rounded bg-sky-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-sky-500 disabled:opacity-50"
                      >
                        ✨ Fit inside printer
                      </button>
                      <button
                        type="button"
                        disabled={transformBusy}
                        onClick={() => void scaleMetersToMillimeters()}
                        className="rounded border border-amber-400/50 bg-amber-950/60 px-2.5 py-1 text-xs font-medium text-amber-200 hover:bg-amber-900/50 disabled:opacity-50"
                      >
                        Scale 1000× (m → mm)
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {isOversizedModel && (
              <div className="mt-3 rounded-md border border-rose-500/40 bg-rose-950/30 p-2.5 text-xs text-rose-200">
                <div className="flex items-start gap-2">
                  <span className="text-base leading-none">⚠️</span>
                  <div className="space-y-1.5 flex-1">
                    <div className="font-semibold text-rose-100">
                      Model exceeds build volume
                    </div>
                    <div className="text-[11px] text-rose-300/90 leading-relaxed">
                      Scale the model down to fit within printer boundaries ({selectedProfile?.build_volume.x} × {selectedProfile?.build_volume.y} × {selectedProfile?.build_volume.z} mm).
                    </div>
                    <div className="pt-1">
                      <button
                        type="button"
                        disabled={transformBusy}
                        onClick={() => void fitModelToPrinter(0.95)}
                        className="rounded bg-rose-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-rose-500 disabled:opacity-50"
                      >
                        ✨ Fit inside printer
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {version && (
              <div className="border-t border-slate-800 pt-3">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-xs font-semibold text-slate-200">Model transform</h3>
                  <label className="flex items-center gap-2 text-[11px] text-slate-400">
                    <input
                      type="checkbox"
                      checked={aspectLocked}
                      onChange={(event) => {
                        const locked = event.target.checked
                        setAspectLocked(locked)
                        if (!locked) return
                        if (dimensionMode === 'percent') {
                          const uniform = scalePercent.x
                          setScalePercent({ x: uniform, y: uniform, z: uniform })
                          return
                        }
                        const dims = (analysis?.metrics.dimensions_mm ?? {}) as {
                          x: number
                          y: number
                          z: number
                        }
                        const ratio = Number(targetDimensions.x) / (dims.x || 1)
                        if (Number.isFinite(ratio) && ratio > 0) {
                          setTargetDimensions({
                            x: (dims.x * ratio).toFixed(6),
                            y: (dims.y * ratio).toFixed(6),
                            z: (dims.z * ratio).toFixed(6),
                          })
                        }
                      }}
                      className="accent-sky-500"
                    />
                    Lock aspect ratio
                  </label>
                </div>
                <div className="mb-3 flex gap-1" role="tablist" aria-label="Transformation input mode">
                  {(['percent', 'target'] as const).map((mode) => (
                    <button
                      key={mode}
                      type="button"
                      role="tab"
                      aria-selected={dimensionMode === mode}
                      onClick={() => setDimensionMode(mode)}
                      className={`rounded px-2 py-1 text-[10px] ${dimensionMode === mode ? 'bg-slate-800 text-sky-300' : 'text-slate-500'}`}
                    >
                      {mode === 'percent' ? 'Scale %' : 'Target mm'}
                    </button>
                  ))}
                </div>
                <div className="grid grid-cols-3 gap-2">
                  {(['x', 'y', 'z'] as const).map((axis) => (
                    <label key={axis} className="block">
                      <span className="field-label">{axis.toUpperCase()}</span>
                      <div className="relative">
                        <input
                          className="input pr-8"
                          type="number"
                          min="0.01"
                          // Matches the backend ceiling for the active unit: a
                          // percentage may reach 1e6 (a 10,000x recovery), a
                          // millimetre target only 1e4.
                          max={dimensionMode === 'percent' ? '1000000' : '10000'}
                          step="any"
                          value={dimensionMode === 'percent' ? scalePercent[axis] : targetDimensions[axis]}
                          onChange={(event) =>
                            dimensionMode === 'percent'
                              ? updatePercent(axis, event.target.value)
                              : updateTargetDimension(axis, event.target.value)
                          }
                          aria-label={dimensionMode === 'percent' ? `${axis.toUpperCase()} scale percent` : `${axis.toUpperCase()} target dimension in millimeters`}
                        />
                        <span className="pointer-events-none absolute right-2 top-2 text-[10px] text-slate-500">
                          {dimensionMode === 'percent' ? '%' : 'mm'}
                        </span>
                      </div>
                    </label>
                  ))}
                </div>
                <div className="mt-3 flex gap-2">
                  <button
                    type="button"
                    onClick={() => void fitModelToPrinter(0.70)}
                    disabled={transformBusy || !version || !analysis}
                    className="rounded border border-sky-700/60 bg-sky-950/40 px-2.5 py-2 text-xs font-semibold text-sky-300 hover:bg-sky-900/50 disabled:opacity-40"
                    title="Scale model proportionally to fit inside the printer"
                  >
                    ✨ Fit to printer
                  </button>
                  <button
                    type="button"
                    onClick={() => void applyModelTransform()}
                    disabled={transformBusy || !analysis || (dimensionMode === 'target' && Object.values(targetDimensions).some((value) => !Number(value)))}
                    className="btn-primary flex-1"
                  >
                    {transformBusy ? 'Applying…' : 'Apply'}
                  </button>
                  <button
                    type="button"
                    onClick={() => void applyModelTransform(true)}
                    disabled={transformBusy || !version.transformed_blob_id}
                    className="rounded border border-slate-700 px-3 py-2 text-xs text-slate-300 disabled:opacity-40"
                    title="Reset model transformation"
                    aria-label="Reset model transformation"
                  >
                    Reset
                  </button>
                </div>
              </div>
            )}
          </div>
        </section>

        <section className="panel">
          <h2 className="panel-title">Printer</h2>
          <div className="space-y-3 p-4">
            {printers.length === 0 ? (
              <div className="space-y-2">
                <p className="text-xs text-slate-500">
                  No printers registered.
                </p>
                <button
                  type="button"
                  onClick={() => navigate('/printers')}
                  className="w-full rounded border border-sky-800/50 bg-sky-950/30 px-3 py-2
                             text-xs font-medium text-sky-400 hover:bg-sky-950/60"
                >
                  + Add a printer
                </button>
              </div>
            ) : (
              <fieldset className="space-y-1.5">
                <legend className="field-label">Your printers · {printers.length} available</legend>
                {printers.map((printer) => {
                  const profile = profiles.find((item) => item.id === printer.machine_profile_id)
                  const machine = machines.find((item) => item.id === profile?.machine_id)
                  return (
                    <div
                      key={printer.id}
                      className={`flex items-start gap-2 rounded border px-2.5 py-2 text-xs ${
                        printerId === printer.id
                          ? 'border-slate-500 bg-slate-100'
                          : 'border-slate-200 hover:bg-slate-50'
                      }`}
                    >
                      <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-2">
                        <input
                          type="radio"
                          name="selected-printer"
                          value={printer.id}
                          checked={printerId === printer.id}
                          onChange={() => setPrinterId(printer.id)}
                          className="mt-0.5"
                        />
                        <span className="min-w-0">
                          <span className="block truncate font-medium text-slate-900">{printer.name}</span>
                          <span className="mt-0.5 block truncate text-[10px] text-slate-500">
                            {machine ? `${machine.vendor} ${machine.model}` : 'Machine profile'} · {printer.nozzle_diameter_mm} mm nozzle
                          </span>
                        </span>
                      </label>
                      <button
                        type="button"
                        onClick={() => void removePrinterFromWorkbench(printer)}
                        disabled={removingPrinterId === printer.id}
                        className="shrink-0 rounded border border-slate-300 px-2 py-1 text-[10px] font-medium text-slate-600 hover:bg-white disabled:opacity-50"
                        aria-label={`Remove ${printer.name}`}
                        title={`Remove ${printer.name}`}
                      >
                        {removingPrinterId === printer.id ? '…' : 'Remove'}
                      </button>
                    </div>
                  )
                })}
              </fieldset>
            )}
            {selectedProfile && machineForProfile && (
              <dl className="space-y-1 text-xs text-slate-400">
                <Row label="Machine" value={`${machineForProfile.vendor} ${machineForProfile.model}`} />
                <Row label="Technology" value={selectedProfile ? (machines.find((m) => m.id === selectedProfile.machine_id)?.technology ?? '') : ''} />
                <Row
                  label="Build volume"
                  value={`${selectedProfile.build_volume.x} × ${selectedProfile.build_volume.y} × ${selectedProfile.build_volume.z} mm`}
                />
                <Row
                  label="Supported nozzles"
                  value={selectedProfile.nozzle_diameters.map((d) => `${d} mm`).join(' · ') || '—'}
                />
                <Row
                  label="Default nozzle"
                  value={`${selectedProfile.default_nozzle_diameter_mm} mm`}
                />
                <Row
                  label="Nozzle limit"
                  value={`${selectedProfile.temperature_limits.nozzle?.min ?? 0}–${selectedProfile.temperature_limits.nozzle?.max ?? 0} °C`}
                />
                <Row
                  label="Bed limit"
                  value={`${selectedProfile.temperature_limits.bed?.min ?? 0}–${selectedProfile.temperature_limits.bed?.max ?? 0} °C`}
                />
                <Row label="Profile v" value={String(selectedProfile.profile_version)} />
                <Row
                  label="Validation"
                  value={
                    selectedProfile.validation_status === 'validated' ? (
                      <span className="flex items-center gap-1 font-medium text-emerald-400">
                        <span>✓</span> Verified
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 font-medium text-amber-400">
                        <span>⚠</span> Not validated
                      </span>
                    )
                  }
                />
              </dl>
            )}
            {selectedProfile && selectedProfile.validation_status !== 'validated' && (
              <div className="rounded border border-amber-900/60 bg-amber-950/40 p-2.5 text-[11px] text-amber-200">
                <p className="font-semibold text-amber-300">⚠ Profile Not Bench-Validated</p>
                <p className="mt-1 text-slate-300">
                  <span className="font-medium text-amber-400">Reason: </span>
                  {selectedProfile.validation_reason ||
                    'This machine profile has not been bench-validated on real hardware, so the platform will refuse to generate G-code for it.'}
                </p>
              </div>
            )}
          </div>
        </section>

        <section className="panel">
          <h2 className="panel-title">Material &amp; nozzle</h2>
          <div className="space-y-3 p-4">
            <label className="block">
              <span className="field-label">Material profile</span>
              <select
                className="input"
                value={materialProfileId}
                onChange={(e) => setMaterialProfileId(e.target.value)}
              >
                {materialProfiles.map((p) => {
                  const family = materials.find((m) => m.id === p.material_id)
                  return (
                    <option key={p.id} value={p.id}>
                      {family?.display_name ?? '?'} · {p.vendor}/{p.brand_sku}
                    </option>
                  )
                })}
              </select>
            </label>
            {selectedMaterialProfile && (
              <dl className="space-y-1 text-xs text-slate-400">
                <Row
                  label="Nozzle range (spec)"
                  value={`${selectedMaterialProfile.nozzle_temp.min}–${selectedMaterialProfile.nozzle_temp.max} °C`}
                />
                <Row
                  label="Bed range (spec)"
                  value={`${selectedMaterialProfile.bed_temp.min}–${selectedMaterialProfile.bed_temp.max} °C`}
                />
                <Row
                  label="Max flow"
                  value={`${selectedMaterialProfile.max_volumetric_flow_mm3s ?? '—'} mm³/s`}
                />
                <Row label="Abrasiveness" value={selectedMaterialProfile.abrasiveness} />
                <Row label="Enclosure" value={selectedMaterialProfile.requires_enclosure ? 'required' : 'not required'} />
                {materialFamily?.is_composite && (
                  <Row label="Composite" value="yes — hardened nozzle advised" />
                )}
              </dl>
            )}
            {/* Authoritative resolved setup: one block, backend-sourced. */}
            <div className="rounded border border-slate-800 bg-slate-950/40 p-3">
              <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                Resolved setup
              </p>
              <dl className="space-y-1 text-xs text-slate-400">
                <Row
                  label="Fitted nozzle"
                  value={
                    selectedPrinter
                      ? `${nozzleMm} mm · ${selectedPrinter.nozzle_material} (${selectedPrinter.name})`
                      : '— select a printer —'
                  }
                />
                <Row
                  label="Printer profile"
                  value={
                    selectedProfile && machineForProfile
                      ? `${machineForProfile.vendor} ${machineForProfile.model} v${selectedProfile.profile_version} (${selectedProfile.validation_status})`
                      : '—'
                  }
                />
                <Row
                  label="Material"
                  value={
                    selectedMaterialProfile && materialFamily
                      ? `${materialFamily.display_name} · ${selectedMaterialProfile.vendor}/${selectedMaterialProfile.brand_sku}`
                      : '— select a material —'
                  }
                />
                {tempLoading && <Row label="Validated temperatures" value="Resolving…" />}
                {!tempLoading && tempRange && tempRange.status === 'valid' && (
                  <>
                    <Row
                      label="Safe nozzle range"
                      value={`${tempRange.safe_min_c}–${tempRange.safe_max_c} °C`}
                    />
                    <Row
                      label="Validated nozzle temp"
                      value={`${tempRange.recommended_c} °C`}
                    />
                    <Row
                      label="First layer (nozzle)"
                      value={`${tempRange.first_layer_recommended_c} °C`}
                    />
                    <Row
                      label="Validated bed temp"
                      value={`${tempRange.bed_recommended_c} °C (${tempRange.bed_safe_min_c}–${tempRange.bed_safe_max_c} °C safe)`}
                    />
                    <Row
                      label="Source"
                      value={`${tempRange.provenance} — ${tempRange.reason}`}
                    />
                  </>
                )}
                {!tempLoading && !tempRange && printerId && materialProfileId && (
                  <Row label="Validated temperatures" value="Could not resolve." />
                )}
              </dl>
              {!tempLoading && tempRange?.status === 'conflict' && (
                <p className="mt-2 rounded bg-rose-950/50 px-2 py-1.5 text-[11px] text-rose-300">
                  {tempRange.conflict_message ??
                    'The selected printer and material have no safe overlapping temperature range.'}
                </p>
              )}
              {!tempLoading &&
                tempRange?.status === 'valid' &&
                tempRange.warnings.length > 0 && (
                  <ul className="mt-2 space-y-0.5">
                    {tempRange.warnings.map((w) => (
                      <li key={w} className="text-[10px] text-amber-400">
                        {w}
                      </li>
                    ))}
                  </ul>
                )}
            </div>
          </div>
        </section>

        <section className="panel">
          <h2 className="panel-title">Objective</h2>
          <div className="space-y-2 p-4">
            {OBJECTIVES.map((o) => (
              <label key={o.key} className="flex cursor-pointer items-start gap-2 text-xs">
                <input
                  type="radio"
                  className="mt-0.5"
                  checked={objective === o.key}
                  onChange={() => {
                    setObjective(o.key)
                    // Manual objective change clears AI parameters
                    setAiParameters(null)
                    setAiApplyResult(null)
                    setAiParamsAccepted(false)
                  }}
                />
                <span>
                  <span className="text-slate-200">{o.label}</span>
                  <span className="block text-[10px] text-slate-500">{o.hint}</span>
                </span>
              </label>
            ))}

            {/* AI proposed configuration review panel */}
            {aiApplyResult && !aiParamsAccepted && (
              <div className="mt-2 rounded border border-sky-800 bg-sky-950/50 p-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-[11px] font-semibold text-sky-300">AI proposed config</span>
                  <span className="rounded bg-sky-900/60 px-1.5 py-0.5 text-[9px] uppercase text-sky-400">
                    {aiApplyResult.changes.length} param{aiApplyResult.changes.length === 1 ? '' : 's'}
                  </span>
                </div>
                <ul className="space-y-1">
                  {aiApplyResult.changes.map((c) => (
                    <li key={c.field} className="text-[10px] text-slate-300">
                      <span className="font-medium text-slate-200">
                        {PARAM_LABELS[c.field] ?? c.field}
                      </span>
                      <span className="mx-1 text-slate-500">→</span>
                      <span className="font-mono text-sky-300">{JSON.stringify(c.value)}</span>
                      <span className="ml-1 text-slate-500">({c.reason})</span>
                    </li>
                  ))}
                </ul>
                {aiApplyResult.warnings.length > 0 && (
                  <ul className="mt-2 space-y-0.5">
                    {aiApplyResult.warnings.map((w) => (
                      <li key={w} className="text-[10px] text-amber-400">{w}</li>
                    ))}
                  </ul>
                )}
                <div className="mt-2 flex gap-2">
                  <button
                    type="button"
                    onClick={() => setAiParamsAccepted(true)}
                    className="flex-1 rounded bg-sky-700 px-2 py-1 text-[10px] font-semibold text-white hover:bg-sky-600"
                  >
                    Accept & use
                  </button>
                  <button
                    type="button"
                    onClick={() => { setAiParameters(null); setAiApplyResult(null) }}
                    className="rounded bg-slate-800 px-2 py-1 text-[10px] text-slate-400 hover:bg-slate-700"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            )}

            {aiParamsAccepted && aiApplyResult && (
              <div className="mt-1 flex items-center gap-2 rounded border border-emerald-800/50 bg-emerald-950/30 px-2 py-1.5">
                <span className="text-emerald-400">✓</span>
                <span className="text-[10px] text-emerald-300">
                  AI config active ({aiApplyResult.changes.length} params locked)
                </span>
                <button
                  type="button"
                  onClick={() => { setAiParameters(null); setAiApplyResult(null); setAiParamsAccepted(false) }}
                  className="ml-auto text-[9px] text-slate-500 hover:text-slate-300"
                >
                  clear
                </button>
              </div>
            )}

            <label className="block">
              <span className="field-label">Print request (optional)</span>
              <input
                className="input"
                value={userRequest}
                maxLength={2000}
                placeholder="e.g. minimise supports, keep the walls thick"
                onChange={(e) => setUserRequest(e.target.value)}
              />
            </label>

            <button
              type="button"
              onClick={() => void runSlice()}
              disabled={!version || busy !== null}
              className="btn-primary mt-2 w-full"
            >
              {busy ?? (aiParamsAccepted ? 'Slice with AI config' : 'Slice')}
            </button>
            {!version && <p className="text-[10px] text-slate-500">Upload a model to enable slicing.</p>}
          </div>
        </section>

        <AiAssistant
          key={`${version?.id ?? ''}|${printerId}|${materialProfileId}`}
          modelVersionId={version?.id ?? null}
          printerId={printerId || null}
          materialProfileId={materialProfileId || null}
          onApply={adoptSuggestedObjective}
        />
      </div>

      {/* ----------------------------- centre ------------------------------ */}
      <div className="flex min-h-0 flex-col gap-3">
        <div className="flex items-center gap-2">
          {(['model', 'toolpath'] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              disabled={m === 'toolpath' && !toolpath}
              className={[
                'rounded px-3 py-1.5 text-xs font-medium',
                mode === m ? 'bg-slate-800 text-sky-300' : 'text-slate-500 hover:bg-slate-800/60',
                m === 'toolpath' && !toolpath ? 'cursor-not-allowed opacity-40' : '',
              ].join(' ')}
            >
              {m === 'model' ? 'Model' : 'Slice Preview'}
            </button>
          ))}
          {mode === 'toolpath' && toolpath && (
            <div className="ml-2 flex flex-1 items-center gap-3">
              <input
                type="range"
                className="flex-1 accent-sky-500"
                min={0}
                max={toolpath.layer_count}
                value={maxLayer}
                onChange={(e) => setMaxLayer(Number(e.target.value))}
              />
              <span className="font-mono text-[11px] text-slate-500">
                {maxLayer}/{toolpath.layer_count}
              </span>
            </div>
          )}
        </div>

        <div className="h-[420px] shrink-0 md:h-[520px] xl:min-h-0 xl:flex-1">
          <Viewport
            file={file}
            toolpath={toolpath}
            mode={mode}
            printerLabel={machineForProfile ? `${machineForProfile.vendor} ${machineForProfile.model}` : null}
            motionSystem={machineForProfile?.motion_system ?? null}
            buildVolume={
              selectedProfile
                ? {
                    x: selectedProfile.build_volume.x,
                    y: selectedProfile.build_volume.y,
                    z: selectedProfile.build_volume.z,
                  }
                : null
            }
            maxLayer={maxLayer}
            onFitToPrinter={() => void fitModelToPrinter(0.70)}
            isTinyModel={isTinyModel}
            isOversizedModel={isOversizedModel}
          />
        </div>

        {events.length > 0 && (
          <div className="panel max-h-40 overflow-auto">
            <h2 className="panel-title">Pipeline log</h2>
            <ul className="space-y-0.5 p-3 font-mono text-[11px]">
              {events.map((event) => (
                <li key={event.id} className={levelClass(event.level)}>
                  {event.stage ? `[${event.stage}] ` : ''}
                  {event.message}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* ------------------------------ right ------------------------------ */}
      <div className="flex min-h-0 flex-col gap-4 overflow-auto">
        <PreparationPanel
          prep={prep}
          objective={objective}
          userRequest={userRequest}
          canPrepare={canPrepare}
          sliceBlockedReason={sliceBlockedReason}
          onPrepare={() => {
            void prep.prepare({ objective, userRequest })
          }}
          onSlice={() => void slicePrepared()}
        />
        {modelChanged && (
          <p role="status" className="border-l-2 border-amber-500 px-3 py-2 text-xs text-amber-300">
            Model changed — re-prepare required.
          </p>
        )}

        <section className="panel">
          <h2 className="panel-title">Printability</h2>
          <div className="p-4">
            {!analysis ? (
              <p className="text-xs text-slate-500">Upload a model to analyse its geometry.</p>
            ) : (
              <>
                <div className="flex items-end gap-3">
                  <span className="text-3xl font-semibold text-slate-100">{score?.toFixed(0)}</span>
                  <span className="pb-1 text-xs text-slate-500">/ 100</span>
                  <span
                    className={[
                      'mb-1 ml-auto rounded px-2 py-0.5 text-[10px] font-semibold uppercase',
                      risk === 'low'
                        ? 'bg-emerald-950 text-emerald-300'
                        : risk === 'medium'
                          ? 'bg-amber-950 text-amber-300'
                          : 'bg-rose-950 text-rose-300',
                    ].join(' ')}
                  >
                    {risk} risk
                  </span>
                </div>
                <div className="mt-2 h-1.5 w-full rounded bg-slate-800">
                  <div
                    className="h-full rounded bg-sky-500"
                    style={{ width: `${score ?? 0}%` }}
                  />
                </div>
                <dl className="mt-3 space-y-1 text-xs text-slate-400">
                  <Row label="Volume" value={metric(metrics.volume_mm3 as number, 'mm³', 1)} />
                  <Row label="Surface area" value={metric(metrics.surface_area_mm2 as number, 'mm²', 0)} />
                  <Row label="Watertight" value={String(metrics.is_watertight)} />
                  <Row label="Min wall" value={metric(metrics.estimated_min_wall_mm as number, 'mm')} />
                  <Row label="Overhang" value={`${(metrics.overhang_area_percent as number)?.toFixed(2) ?? '0'} %`} />
                  <Row label="Bodies" value={String(metrics.body_count ?? 1)} />
                </dl>

                {analysis.findings.length > 0 && (
                  <ul className="mt-3 space-y-1.5">
                    {analysis.findings.map((finding) => (
                      <li
                        key={`${finding.code}-${finding.message}`}
                        className={`rounded border px-2 py-1.5 text-[11px] ${severityClass(finding.severity)}`}
                      >
                        <div className="font-semibold">{finding.code}</div>
                        <div className="mt-0.5 opacity-90">{finding.message}</div>
                        {finding.mitigation && (
                          <div className="mt-1 text-[10px] opacity-70">{finding.mitigation}</div>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </div>
        </section>

        <section className="panel">
          <h2 className="panel-title">Result</h2>
          <div className="space-y-3 p-4">
            {!job && <p className="text-xs text-slate-500">No slice job yet.</p>}

            {job && (
              <>
                <div
                  className={[
                    'rounded px-3 py-2 text-xs font-semibold',
                    printable
                      ? 'bg-emerald-950/60 text-emerald-300'
                      : blocked
                        ? 'bg-rose-950/60 text-rose-300'
                        : 'bg-amber-950/50 text-amber-300',
                  ].join(' ')}
                >
                  {job.status}
                </div>

                {job.error_detail && (
                  <p className="rounded bg-slate-950/60 px-2 py-1.5 text-[11px] text-slate-400">
                    {job.error_detail}
                  </p>
                )}

                {stages.length > 0 && (
                  <ul className="space-y-1">
                    {stages.map((stage) => (
                      <li key={stage.stage} className="flex items-center gap-2 text-[11px]">
                        <StageMark status={stage.status} />
                        <span className="text-slate-300">{stage.stage}</span>
                        <span className="ml-auto font-mono text-slate-600">
                          {stage.duration_ms} ms
                        </span>
                      </li>
                    ))}
                  </ul>
                )}

                {validation && (
                  <div>
                    <div className="mb-1 text-[11px] font-semibold text-slate-300">
                      Toolpath validation
                    </div>
                    {validation.validated ? (
                      <>
                        <p className="text-[11px] text-slate-400">{validation.summary}</p>
                        {validation.findings.length > 0 && (
                          <ul className="mt-1 space-y-1">
                            {validation.findings.map((finding) => (
                              <li
                                key={`${finding.code}-${finding.line_number}`}
                                className={`rounded border px-2 py-1 text-[10px] ${severityClass(finding.severity)}`}
                              >
                                {finding.code}: {finding.message}
                              </li>
                            ))}
                          </ul>
                        )}
                      </>
                    ) : (
                      <p className="text-[11px] text-slate-500">{validation.reason}</p>
                    )}
                  </div>
                )}

                {toolpath && (
                  <dl className="space-y-1 text-xs text-slate-400">
                    <Row label="Layers" value={String(toolpath.layer_count)} />
                    <Row label="Extrusion moves" value={toolpath.stats.extrusion_moves.toLocaleString()} />
                    <Row label="Travel moves" value={toolpath.stats.travel_moves.toLocaleString()} />
                    <Row label="Filament" value={`${toolpath.stats.filament_extruded_mm.toFixed(1)} mm`} />
                  </dl>
                )}

                {files.length > 0 && (
                  <div className="space-y-1">
                    {files.map((file) => (
                      <div key={file.id} className="flex items-center gap-2 text-[11px]">
                        {file.is_blocked ? (
                          <span className="text-rose-400">blocked</span>
                        ) : (
                          <a
                            href={api.downloadUrl(job.id, file.id)}
                            className="text-sky-400 hover:text-sky-300"
                            download={file.filename}
                          >
                            Download {file.filename}
                          </a>
                        )}
                        <span className="ml-auto font-mono text-slate-600">
                          {(file.byte_size / 1024).toFixed(0)} kB
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        </section>

        <section className="panel">
          <h2 className="panel-title">Engine</h2>
          <div className="space-y-2 p-4 text-xs text-slate-400">
            {slicers.length === 0 ? (
              <p className="text-slate-500">No slicing engine registered.</p>
            ) : (
              slicers.map((slicer) => (
                <div key={slicer.engine} className="flex items-center gap-2">
                  <span className={slicer.available ? 'text-emerald-400' : 'text-rose-400'}>
                    ●
                  </span>
                  <span className="text-slate-300">{slicer.display_name}</span>
                  <span className="ml-auto font-mono text-[10px] text-slate-600">
                    {slicer.version ?? 'unknown'}
                  </span>
                  <span className="font-mono text-[10px] text-amber-500/80">{slicer.license}</span>
                </div>
              ))
            )}
            <p className="pt-1 text-[10px] text-slate-600">
              {slicers.find((s) => s.license.includes('AGPL')) &&
                'AGPL-3.0: run out of process, and legal review is required before commercial use.'}
            </p>
            <div className="border-t border-slate-800 pt-2">
              {technologies.map((tech) => (
                <div key={tech.key} className="flex items-center gap-2 py-0.5">
                  <span className={tech.supported ? 'text-emerald-400' : 'text-slate-600'}>●</span>
                  <span className={tech.supported ? 'text-slate-300' : 'text-slate-500'}>
                    {tech.display_name}
                  </span>
                  {!tech.supported && (
                    <span className="ml-auto text-[9px] uppercase text-slate-600">not implemented</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        </section>
      </div>

      {busy && (
        <div className="pointer-events-none fixed bottom-6 left-1/2 -translate-x-1/2 rounded bg-slate-900 px-4 py-2 text-xs text-sky-300 shadow-lg">
          {busy}
        </div>
      )}
      {error && (
        <div className="fixed bottom-6 left-1/2 max-w-xl -translate-x-1/2 rounded border border-rose-900 bg-rose-950/90 px-4 py-2 text-xs text-rose-200 shadow-lg">
          {error}
        </div>
      )}
    </div>
  )
}

function Row({ label, value }: { label: string; value: React.ReactNode }): JSX.Element {
  return (
    <div className="flex items-start gap-2">
      <dt className="w-28 shrink-0 text-slate-500">{label}</dt>
      <dd className="min-w-0 break-words font-mono text-slate-300">{value}</dd>
    </div>
  )
}

function StageMark({ status }: { status: string }): JSX.Element {
  const map: Record<string, string> = {
    succeeded: 'text-emerald-400',
    failed: 'text-rose-400',
    blocked: 'text-rose-400',
    skipped: 'text-amber-400',
    running: 'text-sky-400',
    pending: 'text-slate-600',
  }
  const glyph: Record<string, string> = {
    succeeded: '✓',
    failed: '✕',
    blocked: '⛔',
    skipped: '–',
    running: '●',
    pending: '○',
  }
  return <span className={map[status] ?? 'text-slate-600'}>{glyph[status] ?? '○'}</span>
}

function levelClass(level: string): string {
  if (level === 'error') return 'text-rose-400'
  if (level === 'warning') return 'text-amber-400'
  return 'text-slate-400'
}
