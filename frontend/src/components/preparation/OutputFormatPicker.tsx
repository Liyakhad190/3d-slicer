/**
 * Output format chooser, built entirely from what the backend reported for
 * this printer's profile.
 *
 * A format the profile never declared is disabled and labelled as such; a
 * format it declares but this deployment cannot produce is disabled with the
 * server's own reason and dependency note. Nothing is inferred — in
 * particular nothing assumes "FlashForge means GX".
 */

import type { PreparationOutputFormat } from '../../lib/api'
import { formatName } from '../../lib/preparation'

interface Props {
  formats: PreparationOutputFormat[]
  value: string | null
  onChange: (key: string) => void
  disabled?: boolean
}

export function OutputFormatPicker({ formats, value, onChange, disabled = false }: Props): JSX.Element {
  if (formats.length === 0) {
    return (
      <div>
        <span className="field-label">Output format</span>
        <p className="text-[11px] text-slate-500">
          The backend has not reported any output format for this printer yet.
        </p>
      </div>
    )
  }

  return (
    <fieldset className="space-y-1.5" aria-label="Output format">
      <legend className="field-label">Output format</legend>
      {formats.map((format) => {
        const declared = format.declared_by_profile
        const usable = declared && format.available
        const selected = value === format.key
        return (
          <label
            key={format.key}
            className={[
              'flex items-start gap-2 rounded border px-2.5 py-2 transition-colors',
              selected && usable ? 'border-sky-700 bg-sky-950/40' : 'border-slate-800 bg-slate-950/40',
              usable ? 'cursor-pointer' : 'cursor-not-allowed opacity-70',
            ].join(' ')}
          >
            <input
              type="radio"
              name="output-format"
              className="mt-0.5"
              value={format.key}
              checked={selected}
              disabled={!usable || disabled}
              onChange={() => onChange(format.key)}
            />
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-2">
                <span className="text-xs font-semibold text-slate-200">
                  {formatName(format.format)}
                </span>
                <span className="font-mono text-[10px] text-slate-500">.{format.extension}</span>
                <span
                  className={[
                    'ml-auto rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase',
                    usable
                      ? 'bg-emerald-950 text-emerald-400'
                      : declared
                        ? 'bg-amber-950 text-amber-400'
                        : 'bg-slate-800 text-slate-500',
                  ].join(' ')}
                >
                  {!declared ? 'unsupported' : usable ? 'available' : 'unavailable'}
                </span>
              </span>
              {!declared && (
                <span className="mt-0.5 block text-[10px] text-slate-500">
                  This printer's profile does not declare {formatName(format.format)} output.
                </span>
              )}
              {declared && !format.available && (
                <span className="mt-0.5 block text-[10px] text-amber-400/90">
                  {format.reason || 'Unavailable on this server.'}
                  {format.remedy && (
                    <span className="mt-0.5 block break-words font-mono text-[9px] text-slate-500">
                      {format.remedy}
                    </span>
                  )}
                </span>
              )}
            </span>
          </label>
        )
      })}
    </fieldset>
  )
}
