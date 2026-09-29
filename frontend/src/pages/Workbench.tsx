/**
 * The slicing workbench: the product's main screen.
 *
 * Left: model, printer, material, nozzle, objective.
 * Centre: the 3D viewport, switchable between the model and the real toolpath.
 * Right: printability, validation verdict, stages, risks, download.
 *
 * Every number shown is one the backend measured. Nothing is estimated here.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Viewport } from '../components/Viewport'
import {
  ApiError,
  api,
  type Analysis,
  type GeneratedFile,
  type JobEvent,
  type Machine,
  type MachineProfile,
  type Material,
  type MaterialProfile,
  type ModelVersion,
  type Printer,
  type SlicerEngine,
  type SliceJob,
  type StageRun,
  type Technology,
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

  const [mode, setMode] = useState<'model' | 'toolpath'>('model')
  const [maxLayer, setMaxLayer] = useState(0)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

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
        if (pr.length > 0) setPrinterId(pr[0]?.id ?? '')
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

  // --- upload + analyse --------------------------------------------------
  const onPickFile = useCallback(
    async (picked: File) => {
      setBusy('Uploading and analysing…')
      setError(null)
      try {
        const upload = await api.upload(picked)
        setFile(picked)
        setVersion(upload.version)
        setJob(null)
        setValidation(null)
        setFiles([])
        setToolpath(null)
        setMode('model')
        setAnalysis(await api.analyze(upload.version.id, nozzleMm))
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
      const created = await api.slice({
        model_version_id: version.id,
        printer_id: printerId,
        material_profile_id: materialProfileId,
        objective,
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
  }, [version, printerId, materialProfileId, objective])

  // --- derived metrics ----------------------------------------------------
  const metrics = (analysis?.metrics ?? {}) as Record<string, number | string | boolean>
  const dimensions = (metrics.dimensions_mm ?? {}) as Record<string, number>
  const score = analysis?.printability_score ?? null
  const risk = analysis?.risk_level ?? 'unknown'
  const printable = job?.status === 'COMPLETED'
  const blocked = job?.status === 'BLOCKED'

  return (
    <div className="grid h-full grid-cols-[280px_1fr_320px] gap-4">
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
                <Row
                  label="Dimensions"
                  value={
                    dimensions.x !== undefined
                      ? `${dimensions.x.toFixed(1)} × ${dimensions.y?.toFixed(1)} × ${dimensions.z?.toFixed(1)} mm`
                      : '—'
                  }
                />
              </dl>
            )}
          </div>
        </section>

        <section className="panel">
          <h2 className="panel-title">Printer</h2>
          <div className="space-y-3 p-4">
            {printers.length === 0 ? (
              <p className="text-xs text-slate-500">
                No printers registered. Add one on the Printers page.
              </p>
            ) : (
              <label className="block">
                <span className="field-label">Your printer</span>
                <select
                  className="input"
                  value={printerId}
                  onChange={(e) => setPrinterId(e.target.value)}
                >
                  {printers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} · {p.nozzle_diameter_mm} mm
                    </option>
                  ))}
                </select>
              </label>
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
                  label="Nozzle temp"
                  value={`${selectedProfile.temperature_limits.nozzle?.min ?? 0}–${selectedProfile.temperature_limits.nozzle?.max ?? 0} °C`}
                />
                <Row label="Profile v" value={String(selectedProfile.profile_version)} />
                <Row
                  label="Validation"
                  value={
                    <span
                      className={
                        selectedProfile.validation_status === 'validated'
                          ? 'text-emerald-400'
                          : 'text-amber-400'
                      }
                    >
                      {selectedProfile.validation_status}
                    </span>
                  }
                />
              </dl>
            )}
            {selectedProfile && selectedProfile.validation_status !== 'validated' && (
              <p className="rounded bg-amber-950/50 px-2 py-1.5 text-[11px] text-amber-300">
                This machine profile has not been bench-validated, so the platform will refuse to
                generate G-code for it.
              </p>
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
                  label="Nozzle temp"
                  value={`${selectedMaterialProfile.nozzle_temp.min}–${selectedMaterialProfile.nozzle_temp.max} °C`}
                />
                <Row
                  label="Bed temp"
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
            <label className="block">
              <span className="field-label">Nozzle diameter</span>
              <input className="input" value={`${nozzleMm} mm`} readOnly disabled />
            </label>
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
                  onChange={() => setObjective(o.key)}
                />
                <span>
                  <span className="text-slate-200">{o.label}</span>
                  <span className="block text-[10px] text-slate-500">{o.hint}</span>
                </span>
              </label>
            ))}
            <button
              type="button"
              onClick={() => void runSlice()}
              disabled={!version || busy !== null}
              className="btn-primary mt-2 w-full"
            >
              {busy ?? 'Slice'}
            </button>
            {!version && <p className="text-[10px] text-slate-500">Upload a model to enable slicing.</p>}
          </div>
        </section>
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
              {m === 'model' ? 'Model' : 'Toolpath'}
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

        <div className="min-h-0 flex-1">
          <Viewport
            file={file}
            toolpath={toolpath}
            mode={mode}
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
