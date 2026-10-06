/**
 * The 22 deterministic checks, exactly as the backend scored them.
 *
 * No rule is re-implemented here: statuses, reasons, evidence and the
 * requested-vs-validated corrections all come from the attempt's stored
 * verdict (and from POST /validate when the user asks for a fresh check).
 */

import { useState } from 'react'
import type {
  Normalisation,
  PreparationJob,
  PreparationValidationResponse,
  ValidationCheck,
  ValidationCheckStatus,
} from '../../lib/api'
import { categoryLabel, formatPlanValue, parameterLabel } from '../../lib/preparation'

interface Props {
  job: PreparationJob
  recheck: PreparationValidationResponse | null
  onValidate: () => void
  validating: boolean
}

const TONE: Record<ValidationCheckStatus, string> = {
  PASS: 'bg-emerald-950 text-emerald-400 border-emerald-900',
  WARNING: 'bg-amber-950 text-amber-400 border-amber-900',
  FAILED: 'bg-rose-950 text-rose-400 border-rose-900',
  NOT_REQUIRED: 'bg-slate-800 text-slate-500 border-slate-700',
  UNSUPPORTED: 'bg-slate-800 text-slate-500 border-slate-700',
}

const STATUS_TEXT: Record<ValidationCheckStatus, string> = {
  PASS: 'pass',
  WARNING: 'warning',
  FAILED: 'fail',
  NOT_REQUIRED: 'not required',
  UNSUPPORTED: 'unsupported',
}

function Chip({ status }: { status: ValidationCheckStatus }): JSX.Element {
  return (
    <span
      className={`shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-semibold uppercase ${TONE[status]}`}
    >
      {STATUS_TEXT[status]}
    </span>
  )
}

function Correction({ item }: { item: Normalisation }): JSX.Element {
  return (
    <li className="text-[10px]">
      <div className="flex flex-wrap items-baseline gap-x-1.5">
        <span className="text-slate-400">{parameterLabel(item.field)}</span>
        <span className="text-slate-500">requested</span>
        <span className="font-mono text-rose-400/90 line-through">
          {formatPlanValue(item.model_value)}
        </span>
        <span className="text-slate-600">→</span>
        <span className="text-slate-500">validated</span>
        <span className="font-mono text-emerald-400">{formatPlanValue(item.applied_value)}</span>
      </div>
      {item.reason && <p className="text-slate-600">{item.reason}</p>}
    </li>
  )
}

function CheckCard({ check }: { check: ValidationCheck }): JSX.Element {
  return (
    <li className={`rounded border px-2.5 py-2 text-[11px] ${TONE[check.status]}`}>
      <div className="flex items-center gap-2">
        <span className="font-semibold text-slate-200">{categoryLabel(check.category)}</span>
        <Chip status={check.status} />
        <span className="ml-auto truncate font-mono text-[9px] opacity-60" title={check.code}>
          {check.code}
        </span>
      </div>
      <p className="mt-1 text-slate-300/90">{check.reason}</p>
      {check.evidence.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-[10px] opacity-75">
          {check.evidence.map((line) => (
            <li key={line}>· {line}</li>
          ))}
        </ul>
      )}
      {check.normalisations.length > 0 && (
        <ul className="mt-1.5 space-y-1 border-t border-slate-700/60 pt-1.5">
          {check.normalisations.map((item) => (
            <Correction key={`${item.field}-${item.reason}`} item={item} />
          ))}
        </ul>
      )}
    </li>
  )
}

function CompactCheck({ check }: { check: ValidationCheck }): JSX.Element {
  return (
    <li className="flex items-start gap-2 py-0.5 text-[10px]">
      <Chip status={check.status} />
      <span className="text-slate-400">{categoryLabel(check.category)}</span>
      <span className="ml-auto truncate text-slate-600" title={check.reason}>
        {check.reason}
      </span>
    </li>
  )
}

export function ValidationChecks({ job, recheck, onValidate, validating }: Props): JSX.Element {
  const [showCorrections, setShowCorrections] = useState(false)
  const validation = job.validation
  const checks = validation.checks ?? []
  const summary = validation.summary

  const important = checks.filter(
    (check) => check.status === 'FAILED' || check.status === 'WARNING',
  )
  const rest = checks.filter(
    (check) => check.status !== 'FAILED' && check.status !== 'WARNING',
  )

  const corrections = job.normalisations.length > 0 ? job.normalisations : (validation.normalisations ?? [])

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="block text-xs font-medium text-slate-400">
          Validation{summary ? ` · ${summary.total} checks` : ''}
        </span>
        <button
          type="button"
          onClick={onValidate}
          disabled={validating}
          className="rounded border border-slate-700 px-2 py-1 text-[10px] text-slate-400
                     hover:border-slate-600 hover:text-slate-200 disabled:opacity-40"
          title="Re-run the checklist against the current profiles. No model is called."
        >
          {validating ? 'Checking…' : 'Re-check now'}
        </button>
      </div>

      {summary && (
        <div className="flex flex-wrap gap-1">
          <span className="rounded border border-emerald-900 bg-emerald-950/60 px-1.5 py-0.5 text-[10px] text-emerald-400">
            {summary.pass} pass
          </span>
          <span className="rounded border border-amber-900 bg-amber-950/60 px-1.5 py-0.5 text-[10px] text-amber-400">
            {summary.warning} warning{summary.warning === 1 ? '' : 's'}
          </span>
          <span className="rounded border border-rose-900 bg-rose-950/60 px-1.5 py-0.5 text-[10px] text-rose-400">
            {summary.failed} failed
          </span>
          <span
            className="rounded border border-slate-700 bg-slate-800/60 px-1.5 py-0.5 text-[10px] text-slate-500"
            title={`${summary.not_required} not required · ${summary.unsupported} unsupported`}
          >
            {summary.not_required + summary.unsupported} other
          </span>
        </div>
      )}

      {important.length > 0 && (
        <ul className="space-y-1.5">
          {important.map((check) => (
            <CheckCard key={check.code} check={check} />
          ))}
        </ul>
      )}

      {rest.length > 0 && (
        <details className="rounded border border-slate-800 bg-slate-950/30 p-2">
          <summary className="cursor-pointer text-[10px] text-slate-500 hover:text-slate-300">
            {rest.length} check{rest.length === 1 ? '' : 's'} with nothing to flag
          </summary>
          <ul className="mt-1.5 space-y-0.5 border-t border-slate-800 pt-1.5">
            {rest.map((check) => (
              <CompactCheck key={check.code} check={check} />
            ))}
          </ul>
        </details>
      )}

      {checks.length === 0 && validation.error && (
        <div className="rounded border border-rose-900/60 bg-rose-950/40 p-2.5 text-[11px]">
          <p className="font-semibold text-rose-300">No checklist ran</p>
          <p className="mt-1 text-rose-400/80">
            {validation.error.message ?? 'This attempt failed before validation.'}
          </p>
          {validation.error.code && (
            <p className="mt-1 font-mono text-[10px] text-slate-500">{validation.error.code}</p>
          )}
        </div>
      )}

      {corrections.length > 0 && (
        <details
          className="rounded border border-amber-900/50 bg-amber-950/20 p-2"
          open={showCorrections}
          onToggle={(event) => setShowCorrections((event.target as HTMLDetailsElement).open)}
        >
          <summary className="cursor-pointer text-[10px] font-medium text-amber-400">
            {corrections.length} value{corrections.length === 1 ? '' : 's'} requested vs validated
          </summary>
          <ul className="mt-2 space-y-1.5 border-t border-amber-900/40 pt-1.5">
            {corrections.map((item, index) => (
              <Correction key={`${item.field}-${index}`} item={item} />
            ))}
          </ul>
        </details>
      )}

      {recheck && (
        <div className="rounded border border-sky-900/60 bg-sky-950/30 p-2.5 text-[10px]">
          <p className="font-semibold text-sky-300">Fresh check (just now)</p>
          <p className="mt-1 text-slate-400">
            Stored verdict:{' '}
            <span className={recheck.stored.passed ? 'text-emerald-400' : 'text-rose-400'}>
              {recheck.stored.passed ? 'passes' : 'fails'}
            </span>{' '}
            · re-measured:{' '}
            <span className={recheck.recheck.passed ? 'text-emerald-400' : 'text-rose-400'}>
              {recheck.recheck.passed ? 'passes' : 'fails'}
            </span>
          </p>
          {(recheck.recheck.checks ?? [])
            .filter((check) => check.status === 'FAILED')
            .map((check) => (
              <p key={check.code} className="mt-1 text-rose-400">
                {categoryLabel(check.category)}: {check.reason}
              </p>
            ))}
        </div>
      )}
    </div>
  )
}
