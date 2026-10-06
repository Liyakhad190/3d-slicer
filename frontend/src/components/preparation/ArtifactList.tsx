/**
 * Files the backend produced and validated for this attempt.
 *
 * The endpoint only lists artifacts that passed their own validation, and the
 * download control is gated on that again from the payload — a blocked or
 * unvalidated file is shown as such, never offered for download.
 */

import { useState } from 'react'
import type { OutputArtifact } from '../../lib/api'
import { formatBytes, formatName, shortHash } from '../../lib/preparation'

interface Props {
  artifacts: OutputArtifact[]
  onDownload: (artifact: OutputArtifact) => Promise<void>
  disabled?: boolean
}

function validated(artifact: OutputArtifact): boolean {
  return artifact.validation['status'] === 'passed'
}

export function ArtifactList({ artifacts, onDownload, disabled = false }: Props): JSX.Element {
  const [downloading, setDownloading] = useState<string | null>(null)

  if (artifacts.length === 0) {
    return (
      <p className="text-[11px] text-slate-500">
        No files yet. Slice the prepared plan to produce one.
      </p>
    )
  }

  return (
    <div className="space-y-2">
      <span className="field-label">Output files</span>
      {artifacts.map((artifact) => {
        const ok = validated(artifact)
        const extension = artifact.filename.includes('.')
          ? artifact.filename.slice(artifact.filename.lastIndexOf('.') + 1)
          : artifact.format
        return (
          <div key={artifact.id} className="rounded border border-slate-800 bg-slate-950/40 p-2.5">
            <div className="flex items-start gap-2">
              <span className="min-w-0 break-all font-mono text-[11px] text-slate-200">
                {artifact.filename}
              </span>
              <span className="ml-auto shrink-0 rounded bg-slate-800 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-slate-400">
                {formatName(artifact.format)}
              </span>
            </div>

            <dl className="mt-1.5 space-y-0.5 text-[10px] text-slate-500">
              <div className="flex gap-2">
                <dt className="w-24 shrink-0">Size</dt>
                <dd className="font-mono text-slate-400">{formatBytes(artifact.bytes)}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="w-24 shrink-0">SHA-256</dt>
                <dd className="truncate font-mono text-slate-400" title={artifact.checksum}>
                  {shortHash(artifact.checksum)}
                </dd>
              </div>
              {artifact.printer && (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0">Printer</dt>
                  <dd className="truncate text-slate-400">{artifact.printer}</dd>
                </div>
              )}
              {artifact.material && (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0">Material</dt>
                  <dd className="truncate text-slate-400">{artifact.material}</dd>
                </div>
              )}
              {artifact.slicer && (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0">Slicer</dt>
                  <dd className="truncate font-mono text-slate-400">
                    {artifact.slicer}
                    {artifact.slicer_version ? ` ${artifact.slicer_version}` : ''}
                  </dd>
                </div>
              )}
              {artifact.configuration_fingerprint && (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0">Config</dt>
                  <dd
                    className="truncate font-mono text-slate-400"
                    title={artifact.configuration_fingerprint}
                  >
                    {shortHash(artifact.configuration_fingerprint)}
                  </dd>
                </div>
              )}
              <div className="flex gap-2">
                <dt className="w-24 shrink-0">Validation</dt>
                <dd className={ok ? 'text-emerald-400' : 'text-rose-400'}>
                  {ok ? 'passed' : String(artifact.validation['status'] ?? 'not validated')}
                </dd>
              </div>
            </dl>

            {ok ? (
              <button
                type="button"
                disabled={disabled || downloading !== null}
                onClick={() => {
                  setDownloading(artifact.id)
                  void onDownload(artifact).finally(() => setDownloading(null))
                }}
                className="mt-2 w-full rounded bg-sky-700 px-2 py-1.5 text-[11px] font-semibold
                           text-white hover:bg-sky-600 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {downloading === artifact.id ? 'Downloading…' : `Download .${extension}`}
              </button>
            ) : (
              <p className="mt-2 rounded bg-rose-950/50 px-2 py-1.5 text-[10px] text-rose-300">
                Not validated — download is disabled.
              </p>
            )}
          </div>
        )
      })}
    </div>
  )
}
