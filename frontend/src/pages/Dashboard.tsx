import { useEffect, useState } from 'react'
import { api, type ModelSummary, type SliceJob } from '../lib/api'

function ago(iso: string | null): string {
  if (!iso) return '—'
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

function statusClass(status: string): string {
  if (status === 'COMPLETED') return 'bg-emerald-950 text-emerald-300'
  if (status === 'BLOCKED') return 'bg-rose-950 text-rose-300'
  if (status === 'FAILED') return 'bg-rose-950/60 text-rose-400'
  return 'bg-amber-950 text-amber-300'
}

export default function Dashboard({ user }: { user: { display_name: string } }): JSX.Element {
  const [models, setModels] = useState<ModelSummary[]>([])
  const [jobs, setJobs] = useState<SliceJob[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    void (async () => {
      try {
        const [m, j] = await Promise.all([api.models(), api.jobs()])
        setModels(m)
        setJobs(j)
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : 'Could not load data.')
      }
    })()
  }, [])

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">Dashboard</h1>
        <p className="mt-1 text-sm text-slate-500">
          Welcome back, {user.display_name}. Every figure below is measured, not estimated.
        </p>
      </div>

      {error && <p className="text-xs text-rose-400">{error}</p>}

      <div className="grid grid-cols-3 gap-4">
        <Stat label="Models" value={models.length} />
        <Stat label="Slice jobs" value={jobs.length} />
        <Stat
          label="Validated"
          value={jobs.filter((j) => j.status === 'COMPLETED').length}
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <section className="panel">
          <h2 className="panel-title">Recent models</h2>
          <ul className="divide-y divide-slate-800">
            {models.length === 0 && (
              <li className="p-4 text-xs text-slate-500">
                Nothing uploaded yet. Go to <span className="text-sky-400">Slicer</span> to start.
              </li>
            )}
            {models.map((model) => (
              <li key={model.id} className="flex items-center gap-3 px-4 py-2.5 text-sm">
                <span className="truncate text-slate-200">{model.name}</span>
                <span className="ml-auto font-mono text-[11px] text-slate-600">
                  {ago(model.created_at)}
                </span>
              </li>
            ))}
          </ul>
        </section>

        <section className="panel">
          <h2 className="panel-title">Recent jobs</h2>
          <ul className="divide-y divide-slate-800">
            {jobs.length === 0 && (
              <li className="p-4 text-xs text-slate-500">No jobs yet.</li>
            )}
            {jobs.slice(0, 8).map((job) => (
              <li key={job.id} className="flex items-center gap-3 px-4 py-2.5 text-sm">
                <span className="font-mono text-[11px] text-slate-600">
                  {job.id.slice(0, 8)}
                </span>
                <span
                  className={`rounded px-2 py-0.5 text-[10px] font-semibold ${statusClass(job.status)}`}
                >
                  {job.status}
                </span>
                <span className="ml-auto font-mono text-[11px] text-slate-600">
                  {ago(job.created_at)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: number }): JSX.Element {
  return (
    <div className="panel p-4">
      <div className="text-xs uppercase tracking-wider text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-slate-100">{value}</div>
    </div>
  )
}
