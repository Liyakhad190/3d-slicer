/**
 * The plan, in two clearly separated halves:
 *
 * 1. the configuration the deterministic validator accepted (what the slicer
 *    will actually receive), and
 * 2. the model's own proposal, still labelled as a proposal.
 *
 * The first comes from `validation.locked` / `validation.overrides`, the
 * second from `job.plan`. Nothing in between is inferred here.
 */

import type { PreparationJob, PreparationPlan } from '../../lib/api'
import { categoryLabel, formatPlanValue, parameterLabel } from '../../lib/preparation'

interface Props {
  job: PreparationJob
}

interface Decision {
  label: string
  value: string
  /** The plan category this came from, for the status tag. */
  status?: string
}

function num(value: number | null | undefined, unit: string): string {
  return value === null || value === undefined ? '' : `${value} ${unit}`
}

function join(parts: Array<string | undefined | null>): string {
  return parts.filter((part): part is string => Boolean(part)).join(' · ')
}

function decisions(plan: PreparationPlan): Decision[] {
  const rows: Decision[] = []
  const add = (label: string, value: string, status?: string): void => {
    if (value) rows.push({ label, value, ...(status ? { status } : {}) })
  }

  const orientation = plan.orientation
  if (orientation) {
    add('Orientation', join([orientation.strategy, orientation.candidate_id]), orientation.status)
  }
  if (plan.bed_placement) add('Bed placement', plan.bed_placement.strategy, plan.bed_placement.status)
  if (plan.layer_height) {
    add('Layer height', num(plan.layer_height.value_mm, 'mm'), plan.layer_height.status)
  }
  if (plan.walls) add('Walls', plan.walls.count === null ? '' : String(plan.walls.count), plan.walls.status)
  if (plan.top_bottom) {
    add(
      'Top / bottom',
      join([
        plan.top_bottom.top_layers === null ? null : `${plan.top_bottom.top_layers} top`,
        plan.top_bottom.bottom_layers === null ? null : `${plan.top_bottom.bottom_layers} bottom`,
      ]),
      plan.top_bottom.status,
    )
  }
  if (plan.infill) {
    add(
      'Infill',
      join([
        plan.infill.density_percent === null ? null : `${plan.infill.density_percent}%`,
        plan.infill.pattern,
      ]),
      plan.infill.status,
    )
  }
  if (plan.supports) {
    add(
      'Supports',
      plan.supports.enabled
        ? join(['on', plan.supports.placement, num(plan.supports.density_percent, '%')])
        : 'off',
      plan.supports.status,
    )
  }
  if (plan.support_interface) {
    add('Support interface', plan.support_interface.enabled ? 'on' : 'off', plan.support_interface.status)
  }
  if (plan.brim) add('Brim', plan.brim.enabled ? join(['on', num(plan.brim.width_mm, 'mm')]) : 'off', plan.brim.status)
  if (plan.raft) add('Raft', plan.raft.enabled ? 'on' : 'off', plan.raft.status)
  if (plan.bed_adhesion) add('Adhesion risk', plan.bed_adhesion.risk, plan.bed_adhesion.status)

  const temperature = plan.temperature
  if (temperature) {
    add(
      'Temperature',
      join([
        num(temperature.nozzle_c, '°C nozzle'),
        num(temperature.bed_c, '°C bed'),
        num(temperature.first_layer_nozzle_c, '°C first layer'),
      ]),
      temperature.status,
    )
  }
  if (plan.cooling) add('Cooling', num(plan.cooling.fan_percent, '%'), plan.cooling.status)
  if (plan.speed) {
    add(
      'Print speed',
      join([num(plan.speed.print_mm_s, 'mm/s'), num(plan.speed.first_layer_mm_s, 'mm/s first layer')]),
      plan.speed.status,
    )
  }
  if (plan.flow) add('Flow strategy', plan.flow.strategy, plan.flow.status)
  if (plan.retraction) {
    add(
      'Retraction',
      join([
        num(plan.retraction.distance_mm, 'mm'),
        num(plan.retraction.speed_mm_s, 'mm/s'),
        num(plan.retraction.z_hop_mm, 'mm z-hop'),
      ]),
      plan.retraction.status,
    )
  }
  if (plan.first_layer) {
    add(
      'First layer',
      join([num(plan.first_layer.height_mm, 'mm'), num(plan.first_layer.speed_mm_s, 'mm/s')]),
      plan.first_layer.status,
    )
  }
  if (plan.acceleration) {
    add('Acceleration', num(plan.acceleration.value_mm_s2, 'mm/s²'), plan.acceleration.status)
  }
  if (plan.min_layer_time) {
    add('Min layer time', num(plan.min_layer_time.seconds, 's'), plan.min_layer_time.status)
  }
  if (plan.material_specific && plan.material_specific.honoured.length > 0) {
    add('Material facts honoured', plan.material_specific.honoured.join(', '), plan.material_specific.status)
  }
  if (plan.slicer_specific && plan.slicer_specific.requested_features.length > 0) {
    add('Engine features', plan.slicer_specific.requested_features.join(', '), plan.slicer_specific.status)
  }
  return rows
}

const STATUS_NOTE: Record<string, string> = {
  not_required: 'considered, not needed',
  unsupported: 'engine cannot do this',
  required: 'requested',
}

export function PlanReview({ job }: Props): JSX.Element | null {
  const plan = job.plan
  if (!plan || Object.keys(plan).length === 0) return null

  const locked = Object.entries(job.validation.locked ?? {})
  const overridden = Object.entries(job.validation.overrides ?? {})
  const proposal = decisions(plan)

  return (
    <div className="space-y-2">
      {plan.summary && (
        <p className="text-[11px] italic leading-relaxed text-slate-400">“{plan.summary}”</p>
      )}

      {/* ---------------- validated configuration ---------------- */}
      <div className="rounded border border-emerald-900/60 bg-emerald-950/25 p-2.5">
        <div className="flex items-center justify-between">
          <span className="text-[11px] font-semibold text-emerald-300">
            Validated configuration
          </span>
          <span className="rounded bg-emerald-900/70 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-emerald-300">
            accepted
          </span>
        </div>
        <p className="mt-1 text-[10px] text-slate-500">
          Set by the deterministic validator. These are the values the slicer receives.
        </p>
        {locked.length === 0 && overridden.length === 0 ? (
          <p className="mt-2 text-[10px] text-slate-500">
            The backend recorded no explicit parameters for this plan.
          </p>
        ) : (
          <dl className="mt-2 space-y-1">
            {locked.map(([key, value]) => (
              <div key={`locked-${key}`} className="flex items-baseline gap-2 text-[10px]">
                <dt className="w-32 shrink-0 text-slate-500">{parameterLabel(key)}</dt>
                <dd className="min-w-0 break-words font-mono text-emerald-300">
                  {formatPlanValue(value)}
                </dd>
              </div>
            ))}
            {overridden.map(([key, value]) => (
              <div key={`override-${key}`} className="flex items-baseline gap-2 text-[10px]">
                <dt className="w-32 shrink-0 text-slate-500">{parameterLabel(key)}</dt>
                <dd className="min-w-0 break-words font-mono text-emerald-300">
                  {formatPlanValue(value)}
                </dd>
                <span className="ml-auto shrink-0 rounded bg-slate-800 px-1 text-[8px] uppercase text-slate-500">
                  override
                </span>
              </div>
            ))}
          </dl>
        )}
      </div>

      {/* ---------------- AI recommendation ---------------- */}
      <div className="rounded border border-sky-900/50 bg-sky-950/20 p-2.5">
        <div className="flex items-center justify-between">
          <span className="text-[11px] font-semibold text-sky-300">AI recommendation</span>
          <span className="rounded bg-sky-900/70 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-sky-400">
            proposal
          </span>
        </div>
        <p className="mt-1 text-[10px] text-slate-500">
          What the model proposed. It only reaches the slicer where validation accepted it.
        </p>
        {proposal.length > 0 ? (
          <dl className="mt-2 space-y-1">
            {proposal.map((row) => (
              <div key={row.label} className="flex items-baseline gap-2 text-[10px]">
                <dt className="w-32 shrink-0 text-slate-500">{row.label}</dt>
                <dd className="min-w-0 break-words text-slate-300">{row.value}</dd>
                {row.status && row.status !== 'required' && (
                  <span
                    className="ml-auto shrink-0 rounded bg-slate-800 px-1 text-[8px] uppercase text-slate-500"
                    title={row.status}
                  >
                    {STATUS_NOTE[row.status] ?? categoryLabel(row.status)}
                  </span>
                )}
              </div>
            ))}
          </dl>
        ) : (
          <p className="mt-2 text-[10px] text-slate-500">The plan recorded no decisions.</p>
        )}
        {typeof plan.confidence === 'number' && (
          <p className="mt-2 text-[9px] text-slate-600">
            Model confidence {Math.round(plan.confidence * 100)}% · provider{' '}
            {job.provider ?? 'unknown'}
            {job.model_identifier ? ` · ${job.model_identifier}` : ''}
          </p>
        )}
      </div>
    </div>
  )
}
