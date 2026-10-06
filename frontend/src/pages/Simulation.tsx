import { useEffect, useState } from 'react'
import { api, type Printer, type SliceJob, type Toolpath } from '../lib/api'
import { Viewport } from '../components/Viewport'

export default function Simulation(): JSX.Element {
  const [jobs, setJobs] = useState<SliceJob[]>([])
  const [printers, setPrinters] = useState<Printer[]>([])
  const [selectedJobId, setSelectedJobId] = useState<string>('')
  const [toolpath, setToolpath] = useState<Toolpath | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [maxLayer, setMaxLayer] = useState(0)
  const [speed, setSpeed] = useState(1)
  const [playing, setPlaying] = useState(false)

  useEffect(() => {
    void (async () => {
      try {
        const [j, p] = await Promise.all([api.jobs(), api.printers()])
        const completed = j.filter((job) => job.status === 'COMPLETED')
        setJobs(completed)
        setPrinters(p)
        if (completed.length > 0 && completed[0]) {
          setSelectedJobId(completed[0].id)
        }
      } catch {
        setError('Could not load jobs.')
      }
    })()
  }, [])

  useEffect(() => {
    if (!selectedJobId) return
    setLoading(true)
    setToolpath(null)
    setPlaying(false)
    api
      .jobToolpath(selectedJobId)
      .then((tp) => {
        setToolpath(tp)
        setMaxLayer(tp.layer_count)
      })
      .catch(() => setError('Could not load toolpath for this job.'))
      .finally(() => setLoading(false))
  }, [selectedJobId])

  // Simple layer playback
  useEffect(() => {
    if (!playing || !toolpath) return
    if (maxLayer >= toolpath.layer_count) { setPlaying(false); return }
    const id = setTimeout(() => {
      setMaxLayer((l) => Math.min(l + speed, toolpath.layer_count))
    }, 50)
    return () => clearTimeout(id)
  }, [playing, maxLayer, toolpath, speed])

  const selectedJob = jobs.find((j) => j.id === selectedJobId) ?? null
  const printer = printers.find((p) => p.id === selectedJob?.printer_id) ?? null

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">Toolpath Simulation</h1>
          <p className="mt-0.5 text-sm text-slate-500">
            Replay completed slice jobs layer-by-layer in 3D.
          </p>
        </div>

        {/* Job selector */}
        {jobs.length > 0 && (
          <select
            className="input w-64"
            value={selectedJobId}
            onChange={(e) => setSelectedJobId(e.target.value)}
          >
            {jobs.map((job) => (
              <option key={job.id} value={job.id}>
                {job.id.slice(0, 8)} · {new Date(job.created_at).toLocaleDateString()}
              </option>
            ))}
          </select>
        )}
      </div>

      {error && (
        <div className="rounded border border-rose-800/50 bg-rose-950/30 px-4 py-2 text-xs text-rose-300">
          {error}
        </div>
      )}

      {jobs.length === 0 && !loading && (
        <div className="panel flex flex-1 items-center justify-center text-sm text-slate-500">
          No completed slice jobs yet. Run a slice from the Slicer page first.
        </div>
      )}

      {jobs.length > 0 && (
        <div className="flex min-h-0 flex-1 gap-4">
          {/* 3D viewport */}
          <div className="min-h-0 flex-1 overflow-hidden rounded-lg border border-slate-800">
            {loading ? (
              <div className="flex h-full items-center justify-center text-sm text-slate-500">
                Loading toolpath…
              </div>
            ) : toolpath ? (
              <Viewport
                file={null}
                toolpath={toolpath}
                mode="toolpath"
                buildVolume={toolpath.stats.build_volume}
                maxLayer={maxLayer}
              />
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-slate-500">
                Select a job to view its toolpath.
              </div>
            )}
          </div>

          {/* Controls sidebar */}
          <div className="w-64 shrink-0 space-y-4">
            {/* Playback */}
            {toolpath && (
              <section className="panel">
                <h2 className="panel-title">Playback</h2>
                <div className="space-y-3 p-4">
                  <div>
                    <div className="mb-1 flex justify-between text-[11px] text-slate-400">
                      <span>Layer</span>
                      <span className="font-mono">
                        {maxLayer} / {toolpath.layer_count}
                      </span>
                    </div>
                    <input
                      type="range"
                      className="w-full accent-sky-500"
                      min={0}
                      max={toolpath.layer_count}
                      value={maxLayer}
                      onChange={(e) => {
                        setPlaying(false)
                        setMaxLayer(Number(e.target.value))
                      }}
                    />
                  </div>

                  <div>
                    <div className="mb-1 text-[11px] text-slate-400">Speed</div>
                    <div className="flex gap-1">
                      {[1, 2, 5, 10].map((s) => (
                        <button
                          key={s}
                          type="button"
                          onClick={() => setSpeed(s)}
                          className={`flex-1 rounded py-1 text-[10px] font-medium ${
                            speed === s
                              ? 'bg-sky-700 text-white'
                              : 'bg-slate-800 text-slate-400 hover:bg-slate-700'
                          }`}
                        >
                          {s}×
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => { setMaxLayer(0); setPlaying(false) }}
                      className="flex-1 rounded bg-slate-800 py-1.5 text-[11px] text-slate-400 hover:bg-slate-700"
                    >
                      ⏮ Reset
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        if (maxLayer >= toolpath.layer_count) setMaxLayer(0)
                        setPlaying((p) => !p)
                      }}
                      className="flex-1 rounded bg-sky-700 py-1.5 text-[11px] font-semibold text-white hover:bg-sky-600"
                    >
                      {playing ? '⏸ Pause' : '▶ Play'}
                    </button>
                  </div>
                </div>
              </section>
            )}

            {/* Job info */}
            {toolpath && (
              <section className="panel">
                <h2 className="panel-title">Statistics</h2>
                <dl className="space-y-1.5 p-4 text-xs text-slate-400">
                  <Row label="Layers" value={String(toolpath.layer_count)} />
                  <Row label="Extrusion" value={toolpath.stats.extrusion_moves.toLocaleString() + ' moves'} />
                  <Row label="Travel" value={toolpath.stats.travel_moves.toLocaleString() + ' moves'} />
                  <Row label="Filament" value={toolpath.stats.filament_extruded_mm.toFixed(1) + ' mm'} />
                  <Row
                    label="Build vol"
                    value={`${toolpath.stats.build_volume.x}×${toolpath.stats.build_volume.y}×${toolpath.stats.build_volume.z} mm`}
                  />
                  {printer && <Row label="Printer" value={printer.name} />}
                </dl>
              </section>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="flex items-start gap-2">
      <dt className="w-20 shrink-0 text-slate-500">{label}</dt>
      <dd className="min-w-0 break-words font-mono text-slate-300">{value}</dd>
    </div>
  )
}
