import { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api, type SliceJob } from '../lib/api'

function statusClass(status: string): string {
  if (status === 'COMPLETED') return 'bg-emerald-950 text-emerald-300'
  if (status === 'BLOCKED' || status === 'FAILED') return 'bg-rose-950 text-rose-300'
  return 'bg-amber-950 text-amber-300'
}

export default function Jobs(): JSX.Element {
  const { id } = useParams<{ id: string }>()
  const [jobs, setJobs] = useState<SliceJob[]>([])
  const [selected, setSelected] = useState<SliceJob | null>(null)
  const [stages, setStages] = useState<Array<{ stage: string; status: string; duration_ms: number; detail: Record<string, unknown> }>>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    void api
      .jobs()
      .then(setJobs)
      .catch((caught: unknown) =>
        setError(caught instanceof Error ? caught.message : 'Could not load jobs.'),
      )
  }, [])

  const select = useCallback(async (jobId: string) => {
    setError(null)
    try {
      const [job, jobStages] = await Promise.all([api.job(jobId), api.jobStages(jobId)])
      setSelected(job)
      setStages(jobStages)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not load the job.')
    }
  }, [])

  useEffect(() => {
    if (id) void select(id)
  }, [id, select])

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">Jobs</h1>
        <p className="mt-1 text-sm text-slate-500">
          Progress is derived from the stage runs a real pipeline execution recorded.
        </p>
      </div>

      {error && <p className="text-xs text-rose-400">{error}</p>}

      <div className="grid grid-cols-[1fr_380px] gap-4">
        <section className="panel">
          <h2 className="panel-title">All jobs</h2>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-[11px] uppercase tracking-wider text-slate-500">
                <th className="px-4 py-2">Job</th>
                <th className="px-4 py-2">Status</th>
                <th className="px-4 py-2">Fingerprint</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {jobs.length === 0 && (
                <tr>
                  <td colSpan={3} className="px-4 py-4 text-xs text-slate-500">
                    No jobs yet.
                  </td>
                </tr>
              )}
              {jobs.map((job) => (
                <tr
                  key={job.id}
                  onClick={() => void select(job.id)}
                  className="cursor-pointer hover:bg-slate-800/40"
                >
                  <td className="px-4 py-2 font-mono text-[11px] text-slate-400">
                    {job.id.slice(0, 8)}
                  </td>
                  <td className="px-4 py-2">
                    <span
                      className={`rounded px-2 py-0.5 text-[10px] font-semibold ${statusClass(job.status)}`}
                    >
                      {job.status}
                    </span>
                  </td>
                  <td className="px-4 py-2 font-mono text-[11px] text-slate-600">
                    {job.input_fingerprint ? job.input_fingerprint.slice(0, 12) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="panel">
          <h2 className="panel-title">Stage detail</h2>
          <div className="p-4">
            {!selected ? (
              <p className="text-xs text-slate-500">Select a job to see its stages.</p>
            ) : (
              <>
                <p className="mb-3 font-mono text-[11px] text-slate-500">{selected.id}</p>
                <ul className="space-y-1.5">
                  {stages.map((stage) => (
                    <li key={stage.stage} className="text-xs">
                      <div className="flex items-center gap-2">
                        <span className="text-slate-300">{stage.stage}</span>
                        <span
                          className={[
                            'rounded px-1.5 text-[10px]',
                            stage.status === 'succeeded'
                              ? 'bg-emerald-950 text-emerald-300'
                              : stage.status === 'skipped'
                                ? 'bg-amber-950 text-amber-300'
                                : 'bg-slate-800 text-slate-400',
                          ].join(' ')}
                        >
                          {stage.status}
                        </span>
                        <span className="ml-auto font-mono text-[10px] text-slate-600">
                          {stage.duration_ms} ms
                        </span>
                      </div>
                      {stage.detail.reason ? (
                        <p className="mt-0.5 pl-2 text-[10px] text-amber-400/80">
                          {String(stage.detail.reason)}
                        </p>
                      ) : null}
                    </li>
                  ))}
                </ul>
                {selected.error_detail && (
                  <p className="mt-3 rounded bg-rose-950/50 px-2 py-1.5 text-[11px] text-rose-300">
                    {selected.error_detail}
                  </p>
                )}
              </>
            )}
          </div>
        </section>
      </div>
    </div>
  )
}
