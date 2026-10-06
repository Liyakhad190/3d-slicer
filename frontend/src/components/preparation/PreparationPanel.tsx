/**
 * The print-preparation panel: status, plan review, the 22 checks, the
 * output-format choice, the slice action and the files that came back.
 *
 * Every value on screen is one the backend returned. The panel owns no print
 * settings and makes no decisions — it renders the attempt and forwards the
 * user's actions to the API.
 */

import { useState } from 'react'
import type { PrintObjective } from '../../lib/api'
import { PHASE_LABELS } from '../../lib/preparation'
import type { PreparationController } from '../../lib/usePreparation'
import { ArtifactList } from './ArtifactList'
import { OutputFormatPicker } from './OutputFormatPicker'
import { PlanReview } from './PlanReview'
import { ValidationChecks } from './ValidationChecks'

interface Props {
  prep: PreparationController
  objective: PrintObjective
  userRequest: string
  /** True when model + printer + material are all selected. */
  canPrepare: boolean
  /** Why slicing is unavailable, or null when it is available. */
  sliceBlockedReason: string | null
  onPrepare: () => void
  onSlice: () => void
}

const PHASE_TONE: Record<string, string> = {
  idle: 'bg-slate-800 text-slate-400',
  preparing: 'bg-sky-950 text-sky-300',
  analysing: 'bg-sky-950 text-sky-300',
  planning: 'bg-sky-950 text-sky-300',
  validating: 'bg-sky-950 text-sky-300',
  replanning: 'bg-sky-950 text-sky-300',
  ready: 'bg-emerald-950 text-emerald-300',
  rejected: 'bg-rose-950 text-rose-300',
  failed: 'bg-rose-950 text-rose-300',
}

function ReplanBox({
  prep,
  objective,
  userRequest,
}: {
  prep: PreparationController
  objective: PrintObjective
  userRequest: string
}): JSX.Element {
  const [feedback, setFeedback] = useState('')
  const job = prep.job
  if (!job) return <></>

  const attemptsLeft = job.max_attempts - job.attempt

  return (
    <div className="rounded border border-amber-900/50 bg-amber-950/20 p-2.5">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold text-amber-300">Re-plan</span>
        <span className="font-mono text-[10px] text-slate-500">
          attempt {job.attempt} / {job.max_attempts}
        </span>
      </div>
      <label className="mt-2 block">
        <span className="mb-1 block text-[10px] text-slate-500">
          What should change about the refused plan?
        </span>
        <textarea
          value={feedback}
          onChange={(event) => setFeedback(event.target.value)}
          rows={2}
          maxLength={4000}
          placeholder="e.g. use a brim, or drop the first-layer speed"
          className="w-full resize-none rounded border border-slate-700 bg-slate-950 px-2 py-1.5
                     text-[11px] text-slate-200 placeholder-slate-600 outline-none
                     focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
        />
      </label>
      {attemptsLeft > 0 ? (
        <button
          type="button"
          disabled={prep.busy !== null}
          onClick={() => {
            void prep.replan({ objective, userRequest, feedback })
          }}
          className="mt-2 w-full rounded bg-amber-700 px-2 py-1.5 text-[11px] font-semibold
                     text-white hover:bg-amber-600 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {prep.busy ? prep.busy : `Re-plan (${attemptsLeft} attempt${attemptsLeft === 1 ? '' : 's'} left)`}
        </button>
      ) : (
        <p className="mt-2 text-[10px] text-slate-500">
          This plan has used all {job.max_attempts} attempts. Prepare a new one after changing
          the model, printer or objective.
        </p>
      )}
    </div>
  )
}

export function PreparationPanel({
  prep,
  objective,
  userRequest,
  canPrepare,
  sliceBlockedReason,
  onPrepare,
  onSlice,
}: Props): JSX.Element {
  const { job, phase, busy, error, invalidated, outputs, formatKey, setFormatKey, recheck } = prep

  return (
    <section className="panel">
      <h2 className="panel-title">Print preparation</h2>
      <div className="space-y-3 p-4">
        {/* ---------------- status ---------------- */}
        <div className="flex items-center gap-2">
          <span
            className={`rounded px-2 py-0.5 text-[10px] font-semibold uppercase ${PHASE_TONE[phase] ?? PHASE_TONE['idle']}`}
          >
            {PHASE_LABELS[phase]}
          </span>
          {job && (
            <span className="ml-auto font-mono text-[10px] text-slate-600">
              attempt {job.attempt}/{job.max_attempts}
            </span>
          )}
        </div>

        {busy && (
          <div
            className="flex items-center gap-2 rounded border border-sky-900/50 bg-sky-950/30
                       px-2.5 py-2 text-[11px] text-sky-300"
            role="status"
          >
            <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-sky-200/30 border-t-sky-200" />
            <span>{busy}</span>
          </div>
        )}

        {error && (
          <div className="rounded border border-rose-900 bg-rose-950/40 p-2.5 text-[11px]">
            <div className="flex items-center gap-2">
              <span className="font-mono text-[9px] font-semibold text-rose-400">{error.code}</span>
              <button
                type="button"
                onClick={prep.clearError}
                className="ml-auto text-[9px] text-slate-500 hover:text-slate-300"
              >
                dismiss
              </button>
            </div>
            <p className="mt-1 text-rose-200">{error.message}</p>
            {error.hint && <p className="mt-1 text-slate-500">{error.hint}</p>}
            {!job && error.action === 'retry' && (
              <button
                type="button"
                onClick={onPrepare}
                disabled={!canPrepare}
                className="mt-2 w-full rounded bg-sky-700 px-2 py-1.5 text-[11px] font-semibold
                           text-white hover:bg-sky-600 disabled:opacity-40"
              >
                Try again
              </button>
            )}
          </div>
        )}

        {invalidated && !job && !busy && (
          <>
            <p className="rounded border border-amber-900/60 bg-amber-950/30 px-2.5 py-2 text-[11px] text-amber-300">
              Preparation required — the model, printer or material changed since the last attempt.
            </p>
            <button
              type="button"
              onClick={onPrepare}
              disabled={!canPrepare}
              className="btn-primary w-full"
            >
              Prepare print
            </button>
          </>
        )}

        {!job && !busy && !invalidated && !error && (
          <div className="space-y-2">
            <p className="text-[11px] text-slate-500">
              {canPrepare
                ? 'Not prepared yet. Add a print request, then prepare the print.'
                : 'Select a model, a printer and a material to prepare a print.'}
            </p>
            <button
              type="button"
              onClick={onPrepare}
              disabled={!canPrepare}
              className="btn-primary w-full"
            >
              Prepare print
            </button>
          </div>
        )}

        {/* ---------------- an attempt exists ---------------- */}
        {job && (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-x-3 text-[10px] text-slate-600">
              {job.duration_ms > 0 && <span>{(job.duration_ms / 1000).toFixed(1)} s</span>}
              {job.provider && <span>{job.provider}</span>}
              {job.model_identifier && <span className="font-mono">{job.model_identifier}</span>}
              {job.input_sha256 && (
                <span className="font-mono" title={job.input_sha256}>
                  input {job.input_sha256.slice(0, 8)}…
                </span>
              )}
            </div>

            {phase === 'rejected' && job.rejected_reason && (
              <p className="rounded border border-rose-900/60 bg-rose-950/40 px-2.5 py-2 text-[11px] text-rose-200">
                {job.rejected_reason}
              </p>
            )}

            {phase === 'failed' && (
              <div className="rounded border border-rose-900/60 bg-rose-950/40 p-2.5 text-[11px]">
                <p className="font-semibold text-rose-300">The attempt produced no plan</p>
                <p className="mt-1 text-rose-400/80">
                  {job.validation.error?.message ??
                    job.rejected_reason ??
                    'The provider failed before a plan could be read.'}
                </p>
                {job.validation.error?.code && (
                  <p className="mt-1 font-mono text-[10px] text-slate-500">
                    {job.validation.error.code}
                  </p>
                )}
                <button
                  type="button"
                  onClick={onPrepare}
                  disabled={!canPrepare || busy !== null}
                  className="mt-2 w-full rounded bg-sky-700 px-2 py-1.5 text-[11px] font-semibold
                             text-white hover:bg-sky-600 disabled:opacity-40"
                >
                  Prepare again
                </button>
              </div>
            )}

            {(phase === 'ready' || phase === 'rejected') && <PlanReview job={job} />}

            <ValidationChecks
              job={job}
              recheck={recheck}
              onValidate={() => void prep.validate()}
              validating={busy !== null}
            />

            {phase === 'ready' && (
              <div className="space-y-2 rounded border border-slate-800 bg-slate-950/40 p-2.5">
                <OutputFormatPicker
                  formats={job.output_formats}
                  value={formatKey}
                  onChange={setFormatKey}
                  disabled={busy !== null}
                />
                {sliceBlockedReason && (
                  <p className="text-[10px] text-amber-400">{sliceBlockedReason}</p>
                )}
                <button
                  type="button"
                  onClick={onSlice}
                  disabled={busy !== null || formatKey === null || sliceBlockedReason !== null}
                  className="btn-primary w-full"
                >
                  {busy ?? 'Accept & slice'}
                </button>
                <p className="text-[9px] text-slate-600">
                  Slicing re-validates this plan against the current profiles before any file is
                  written. Only a validated file can be downloaded.
                </p>
              </div>
            )}

            {phase === 'rejected' && (
              <ReplanBox prep={prep} objective={objective} userRequest={userRequest} />
            )}

            <ArtifactList
              artifacts={outputs}
              onDownload={async (artifact) => {
                await prep.download(artifact)
              }}
              disabled={busy !== null}
            />
          </div>
        )}
      </div>
    </section>
  )
}
