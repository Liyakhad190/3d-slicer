import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  ApiError,
  type AiApply,
  type AiHealth,
  type AiPlan,
  type IntentPriorities,
  type Normalisation,
  type RecordedDecision,
  type TemperatureRange,
} from '../lib/api'

interface Props {
  modelVersionId: string | null
  printerId: string | null
  materialProfileId: string | null
  /** Called when the user accepts the suggested parameters. */
  onApply?: (parameters: Record<string, unknown>, plan: AiApply) => void
}

type Phase = 'idle' | 'planning' | 'ready' | 'failed'

const PRIORITY_LABELS: Record<keyof IntentPriorities, string> = {
  strength: 'Strength',
  speed: 'Speed',
  material_efficiency: 'Material saving',
  surface_quality: 'Surface quality',
}

const PRIORITY_COLORS: Record<keyof IntentPriorities, string> = {
  strength: 'bg-orange-500',
  speed: 'bg-sky-500',
  material_efficiency: 'bg-emerald-500',
  surface_quality: 'bg-violet-500',
}

const QUICK_CHIPS = [
  { label: '💪 Strong', text: 'Make it as strong as possible, print time does not matter' },
  { label: '⚡ Fast', text: 'Fastest possible print, quality is secondary' },
  { label: '✨ Smooth', text: 'I want the smoothest surface finish achievable' },
  { label: '🪶 Lightweight', text: 'Lightweight and save material, non-structural prototype' },
  { label: '⚖️ Balanced', text: 'Balanced trade-off between strength, speed and quality' },
  { label: '🏗️ Functional', text: 'Functional part, needs good layer adhesion and dimensional accuracy' },
]

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

function formatValue(v: unknown): string {
  if (typeof v === 'boolean') return v ? 'on' : 'off'
  if (typeof v === 'number') return String(v)
  return JSON.stringify(v)
}

/**
 * The AI print assistant.
 *
 * Design rules:
 * 1. Nothing is applied silently — the user always reviews and confirms.
 * 2. The model's miscalculations are visible (normalisations are shown).
 * 3. Temperature is resolved deterministically on the backend; the
 *    frontend shows the safe range as a guardrail, never inventing it.
 */
export function AiAssistant({ modelVersionId, printerId, materialProfileId, onApply }: Props) {
  const [health, setHealth] = useState<AiHealth | null>(null)
  const [healthChecked, setHealthChecked] = useState(false)
  const [text, setText] = useState('')
  const [phase, setPhase] = useState<Phase>('idle')
  const [plan, setPlan] = useState<AiPlan | null>(null)
  const [applied, setApplied] = useState<AiApply | null>(null)
  const [error, setError] = useState<{ code: string; message: string; hint: string } | null>(null)
  const [instruction, setInstruction] = useState('')
  const [refining, setRefining] = useState(false)
  const [ledger, setLedger] = useState<RecordedDecision[]>([])
  const [declining, setDeclining] = useState(false)
  const [tempRange, setTempRange] = useState<TemperatureRange | null>(null)
  const [tempLoading, setTempLoading] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // ---------- health check -----------------------------------------------
  const checkHealth = useCallback(async () => {
    try {
      setHealth(await api.aiHealth())
    } catch (exc) {
      setHealth({
        provider: 'ollama',
        model: '',
        available: false,
        status: 'unavailable',
        code: 'HEALTH_CHECK_FAILED',
        message: exc instanceof Error ? exc.message : 'Could not reach the AI service.',
        latency_ms: null,
        models_available: [],
        hint: '',
      })
    } finally {
      setHealthChecked(true)
    }
  }, [])

  useEffect(() => { void checkHealth() }, [checkHealth])

  // ---------- temperature range ------------------------------------------
  useEffect(() => {
    if (!printerId || !materialProfileId) { setTempRange(null); return }
    setTempLoading(true)
    api.temperatureRange(printerId, materialProfileId)
      .then(setTempRange)
      .catch(() => setTempRange(null))
      .finally(() => setTempLoading(false))
  }, [printerId, materialProfileId])

  // ---------- ledger -------------------------------------------------------
  const refreshLedger = useCallback(async () => {
    try {
      setLedger((await api.aiDecisions(10)).decisions)
    } catch { /* best-effort */ }
  }, [])

  // ---------- plan ---------------------------------------------------------
  const planNow = useCallback(
    async (request: string) => {
      setPhase('planning')
      setError(null)
      setApplied(null)
      try {
        const result = await api.aiPlan({
          request,
          model_version_id: modelVersionId,
          printer_id: printerId,
          material_profile_id: materialProfileId,
          conversation: [],
        })
        setPlan(result)
        setPhase('ready')
        void refreshLedger()
      } catch (exc) {
        const apiError = exc as ApiError
        setError({
          code: apiError.code ?? 'UNKNOWN',
          message: apiError.message,
          hint: typeof apiError.details?.hint === 'string' ? apiError.details.hint : '',
        })
        setPhase('failed')
      }
    },
    [modelVersionId, printerId, materialProfileId, refreshLedger],
  )

  // ---------- refine -------------------------------------------------------
  const refine = useCallback(async () => {
    if (!plan || !instruction.trim()) return
    setRefining(true)
    setError(null)
    try {
      const result = await api.aiRefine({
        previous_plan: plan.validation.plan as unknown as Record<string, unknown>,
        instruction: instruction.trim(),
        previous_decision_id: plan.decision_id,
      })
      setPlan(result)
      setApplied(null)
      setInstruction('')
      void refreshLedger()
    } catch (exc) {
      const apiError = exc as ApiError
      setError({
        code: apiError.code ?? 'UNKNOWN',
        message: apiError.message,
        hint: typeof apiError.details?.hint === 'string' ? apiError.details.hint : '',
      })
    } finally {
      setRefining(false)
    }
  }, [plan, instruction, refreshLedger])

  // ---------- apply --------------------------------------------------------
  const apply = useCallback(async () => {
    if (!plan) return
    try {
      const result = await api.aiApply({
        plan: plan.validation.plan as unknown as Record<string, unknown>,
        printer_id: printerId,
        plan_decision_id: plan.decision_id,
      })
      setApplied(result)
      void refreshLedger()
    } catch (exc) {
      const apiError = exc as ApiError
      setError({
        code: apiError.code ?? 'UNKNOWN',
        message: apiError.message,
        hint: typeof apiError.details?.hint === 'string' ? apiError.details.hint : '',
      })
    }
  }, [plan, printerId, refreshLedger])

  // ---------- decline ------------------------------------------------------
  const decline = useCallback(async () => {
    if (!plan?.decision_id) return
    setDeclining(true)
    try {
      await api.aiOverride(plan.decision_id, 'Declined in the workbench.')
      void refreshLedger()
    } catch (exc) {
      const apiError = exc as ApiError
      setError({
        code: apiError.code ?? 'UNKNOWN',
        message: apiError.message,
        hint: typeof apiError.details?.hint === 'string' ? apiError.details.hint : '',
      })
    } finally {
      setDeclining(false)
    }
  }, [plan, refreshLedger])

  const normalisations: Normalisation[] = plan?.validation.normalisations ?? []
  const unavailable = healthChecked && health !== null && !health.available

  const priorities = useMemo(
    () => (plan ? Object.entries(plan.validation.plan.intent) : []),
    [plan],
  )

  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/60 backdrop-blur">
      {/* ── header ── */}
      <div className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
        <div className="flex items-center gap-2">
          <span className="text-base">🤖</span>
          <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-400">
            Print assistant
          </h2>
        </div>
        {health && (
          <span
            className={`rounded px-2 py-0.5 text-[10px] font-medium ${
              health.available
                ? 'bg-emerald-950 text-emerald-400'
                : 'bg-rose-950 text-rose-400'
            }`}
            title={health.message || undefined}
          >
            {health.available
              ? `${health.provider} · ${health.model}${health.latency_ms !== null ? ` · ${health.latency_ms}ms` : ''}`
              : health.code ?? 'offline'}
          </span>
        )}
      </div>

      <div className="space-y-4 p-4">

        {/* ── AI offline warning ── */}
        {unavailable && health && (
          <div className="rounded border border-amber-800/50 bg-amber-950/30 p-3 text-xs">
            <p className="font-semibold text-amber-300">{health.message}</p>
            {health.hint && <p className="mt-1 text-amber-400/70">{health.hint}</p>}
            <button
              type="button"
              onClick={() => void checkHealth()}
              className="mt-2 rounded bg-amber-900/50 px-2 py-1 text-[10px] text-amber-300 hover:bg-amber-900"
            >
              Check again
            </button>
          </div>
        )}

        {/* ── temperature guardrail ── */}
        {(tempRange || tempLoading) && printerId && materialProfileId && (
          <div className={`rounded border px-3 py-2 text-[10px] ${
            tempRange?.status === 'conflict'
              ? 'border-rose-800/50 bg-rose-950/30 text-rose-300'
              : 'border-slate-700/50 bg-slate-950/40 text-slate-400'
          }`}>
            {tempLoading ? (
              <span className="animate-pulse text-slate-500">Resolving safe temperatures…</span>
            ) : tempRange ? (
              <div className="space-y-1">
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-slate-300">Safe temperature range</span>
                  {tempRange.status === 'conflict' && (
                    <span className="rounded bg-rose-900/60 px-1.5 py-0.5 text-[9px] uppercase text-rose-400">
                      conflict
                    </span>
                  )}
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-0.5">
                  <span>
                    Nozzle:{' '}
                    <span className="font-mono text-slate-200">
                      {tempRange.safe_min_c}–{tempRange.safe_max_c} °C
                    </span>
                    <span className="ml-1 text-sky-400">
                      (target {tempRange.recommended_c} °C)
                    </span>
                  </span>
                  <span>
                      Bed:{' '}
                      <span className="font-mono text-slate-200">
                        {tempRange.bed_safe_min_c}–{tempRange.bed_safe_max_c} °C
                      </span>
                      <span className="ml-1 text-sky-400">
                        (target {tempRange.bed_recommended_c} °C)
                      </span>
                  </span>
                </div>
                <div className="text-[9px] text-slate-600">
                  via {tempRange.provenance} · {tempRange.reason}
                </div>
                {tempRange.conflict_message && (
                  <div className="text-rose-400">{tempRange.conflict_message}</div>
                )}
                {tempRange.warnings.length > 0 && (
                  <ul className="mt-1 space-y-0.5">
                    {tempRange.warnings.map((w) => (
                      <li key={w} className="text-amber-400">⚠ {w}</li>
                    ))}
                  </ul>
                )}
              </div>
            ) : null}
          </div>
        )}

        {/* ── request form ── */}
        <form
          onSubmit={(e) => {
            e.preventDefault()
            if (text.trim()) void planNow(text.trim())
          }}
          className="space-y-3"
        >
          <label className="block">
            <span className="mb-1 block text-[11px] font-medium text-slate-400">
              What do you want from this print?
            </span>
            <textarea
              id="ai-request"
              ref={inputRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Describe your goal in plain English — e.g. 'strong but lightweight, print time does not matter'"
              rows={4}
              maxLength={2000}
              disabled={phase === 'planning'}
              className="w-full resize-none rounded border border-slate-700 bg-slate-950 px-3 py-2.5 text-sm
                         text-slate-200 placeholder-slate-600 outline-none focus:border-sky-500
                         focus:ring-1 focus:ring-sky-500 disabled:opacity-40"
            />
          </label>

          {/* Quick objective chips */}
          <div className="flex flex-wrap gap-1.5">
            {QUICK_CHIPS.map((chip) => (
              <button
                key={chip.label}
                type="button"
                onClick={() => {
                  setText(chip.text)
                  inputRef.current?.focus()
                }}
                className={`rounded-full border px-2.5 py-1 text-[10px] font-medium transition-colors ${
                  text === chip.text
                    ? 'border-sky-600 bg-sky-900/50 text-sky-300'
                    : 'border-slate-700 bg-slate-900 text-slate-400 hover:border-slate-600 hover:text-slate-200'
                }`}
              >
                {chip.label}
              </button>
            ))}
          </div>

          <button
            type="submit"
            disabled={!text.trim() || phase === 'planning'}
            className="w-full rounded bg-sky-600 px-3 py-2 text-sm font-semibold text-white
                       transition-colors hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {phase === 'planning' ? (
              <span className="flex items-center justify-center gap-2">
                <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-sky-200/30 border-t-sky-200" />
                Thinking… (10–30 s on a small model)
              </span>
            ) : (
              'Suggest settings'
            )}
          </button>
        </form>

        {/* ── error ── */}
        {error && (
          <div className="rounded border border-rose-800/50 bg-rose-950/30 p-3 text-xs">
            <p className="font-semibold text-rose-300">{error.code}</p>
            <p className="mt-1 text-rose-400/80">{error.message}</p>
            {error.hint && <p className="mt-1 text-slate-500">{error.hint}</p>}
          </div>
        )}

        {/* ── plan result ── */}
        {plan && plan.validation.valid && (
          <div className="space-y-3">
            <div className="rounded border border-sky-800/40 bg-sky-950/20 p-3">
              {/* rationale */}
              {plan.validation.plan.rationale && (
                <p className="mb-3 text-[11px] italic leading-relaxed text-slate-300">
                  "{plan.validation.plan.rationale}"
                </p>
              )}

              {/* priority bars */}
              <div className="space-y-2">
                {priorities.map(([name, value]) => {
                  const numVal = value as number
                  const colorClass = PRIORITY_COLORS[name as keyof IntentPriorities] ?? 'bg-slate-500'
                  return (
                    <div key={name} className="flex items-center gap-2">
                      <span className="w-28 shrink-0 text-[10px] text-slate-400">
                        {PRIORITY_LABELS[name as keyof IntentPriorities] ?? name}
                      </span>
                      <div className="flex-1 overflow-hidden rounded-full bg-slate-800 h-1.5">
                        <div
                          className={`h-full rounded-full transition-all ${colorClass}`}
                          style={{ width: `${Math.round(numVal * 100)}%` }}
                        />
                      </div>
                      <span className="w-7 text-right font-mono text-[10px] text-slate-500">
                        {Math.round(numVal * 100)}%
                      </span>
                    </div>
                  )
                })}
              </div>

              {/* metadata row */}
              <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-slate-500">
                <span>Supports: <span className="text-slate-400">{plan.validation.plan.support_preference}</span></span>
                <span>Orientation: <span className="text-slate-400">{plan.validation.plan.orientation_preference}</span></span>
                {plan.attempts > 1 && <span>{plan.attempts} attempts</span>}
                {plan.duration_ms > 0 && <span>{(plan.duration_ms / 1000).toFixed(1)} s</span>}
              </div>
            </div>

            {/* normalisations */}
            {normalisations.length > 0 && (
              <details className="rounded border border-amber-800/30 bg-amber-950/20 p-2 text-[10px]">
                <summary className="cursor-pointer font-medium text-amber-400">
                  ⚠ {normalisations.length} value{normalisations.length === 1 ? '' : 's'} corrected by the server
                </summary>
                <ul className="mt-2 space-y-1.5">
                  {normalisations.map((item) => (
                    <li key={item.field} className="text-slate-400">
                      <code className="text-slate-300">{item.field}</code>:{' '}
                      <span className="line-through text-rose-400/70">{JSON.stringify(item.model_value)}</span>
                      {' → '}
                      <span className="text-emerald-400">{JSON.stringify(item.applied_value)}</span>
                      <span className="ml-1 text-slate-500">— {item.reason}</span>
                    </li>
                  ))}
                </ul>
                <p className="mt-2 text-slate-600">
                  The model returned out-of-range values. They were clamped before being forwarded.
                </p>
              </details>
            )}

            {/* refine input */}
            <div className="flex gap-2">
              <input
                id="ai-instruction"
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') void refine() }}
                placeholder="Refine — e.g. 'smoother surface'"
                maxLength={1000}
                className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-3 py-1.5
                           text-xs text-slate-200 placeholder-slate-600 outline-none
                           focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
              <button
                type="button"
                onClick={() => void refine()}
                disabled={refining || !instruction.trim()}
                className="rounded bg-slate-800 px-3 py-1.5 text-xs font-medium text-slate-300
                           hover:bg-slate-700 disabled:opacity-40"
              >
                {refining ? '…' : 'Adjust'}
              </button>
            </div>

            {/* CTA row */}
            {!applied && (
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => void apply()}
                  className="flex-1 rounded bg-sky-700 px-3 py-2 text-xs font-semibold text-white
                             hover:bg-sky-600"
                >
                  Show resulting parameters
                </button>
                {plan.decision_id && (
                  <button
                    type="button"
                    onClick={() => void decline()}
                    disabled={declining}
                    className="rounded border border-slate-700 px-3 py-2 text-xs text-slate-500
                               hover:border-slate-600 hover:text-slate-300 disabled:opacity-40"
                    title="Record that this suggestion didn't match your intent"
                  >
                    {declining ? '…' : '✕'}
                  </button>
                )}
              </div>
            )}

            {/* applied parameters */}
            {applied && (
              <div className="space-y-3 rounded border border-emerald-800/40 bg-emerald-950/20 p-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-emerald-300">
                    Proposed configuration ({applied.changes.length} params)
                  </span>
                  <button
                    type="button"
                    onClick={() => setApplied(null)}
                    className="text-[10px] text-slate-600 hover:text-slate-400"
                  >
                    back
                  </button>
                </div>

                <ul className="space-y-1.5">
                  {applied.changes.map((change) => (
                    <li key={change.field} className="text-[11px]">
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="font-medium text-slate-300">
                          {PARAM_LABELS[change.field] ?? change.field}
                        </span>
                        <span className="flex items-center gap-1 font-mono">
                          {change.previous !== undefined && change.previous !== null && (
                            <span className="text-slate-600 line-through">
                              {formatValue(change.previous)}
                            </span>
                          )}
                          <span className="text-emerald-400">{formatValue(change.value)}</span>
                        </span>
                      </div>
                      <p className="text-[10px] text-slate-500">{change.reason}</p>
                    </li>
                  ))}
                </ul>

                {applied.warnings.length > 0 && (
                  <ul className="space-y-1">
                    {applied.warnings.map((w) => (
                      <li key={w} className="text-[10px] text-amber-400">⚠ {w}</li>
                    ))}
                  </ul>
                )}

                {onApply && (
                  <button
                    type="button"
                    onClick={() => onApply(applied.parameters, applied)}
                    className="w-full rounded bg-emerald-700 px-3 py-2 text-xs font-semibold
                               text-white hover:bg-emerald-600"
                  >
                    ✓ Accept & use these settings
                  </button>
                )}
                <p className="text-[10px] text-slate-600">
                  Nothing sent to the printer yet. Slicing still goes through the validated pipeline.
                </p>
              </div>
            )}

            {/* decision ledger */}
            {ledger.length > 0 && (
              <details className="text-[10px]">
                <summary className="cursor-pointer text-slate-600 hover:text-slate-400">
                  Decision log ({ledger.length})
                </summary>
                <ul className="mt-2 space-y-2 border-l border-slate-800 pl-3">
                  {ledger.map((item) => (
                    <li key={item.id}>
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-slate-400">{item.decision_type}</span>
                        <span className="text-slate-600">
                          {item.source} · {item.calibration} · {item.evidence.length} citation{item.evidence.length === 1 ? '' : 's'}
                        </span>
                      </div>
                      <div className="text-slate-500">{item.reason}</div>
                      {item.override_reason && (
                        <div className="text-rose-400/70">Declined: {item.override_reason}</div>
                      )}
                      {item.evidence.length > 0 && (
                        <details>
                          <summary className="cursor-pointer text-slate-700 hover:text-slate-500">Evidence</summary>
                          <ul className="mt-1 space-y-0.5 pl-2 text-slate-600">
                            {item.evidence.map((cite, i) => (
                              <li key={`${item.id}-${i}`}>
                                <code>{cite.kind}</code>
                                {cite.ref_id ? ` ${cite.ref_id}` : ''}
                                {cite.excerpt ? ` — ${cite.excerpt}` : ''}
                              </li>
                            ))}
                          </ul>
                        </details>
                      )}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
      </div>
    </section>
  )
}