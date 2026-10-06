import { useEffect, useMemo, useState } from 'react'
import { ApiError, api, type Machine, type MachineProfile, type Material, type MaterialProfile, type Printer } from '../lib/api'

export default function Catalog({ section }: { section: 'printers' | 'materials' }): JSX.Element {
  const [machines, setMachines] = useState<Machine[]>([])
  const [profiles, setProfiles] = useState<MachineProfile[]>([])
  const [materials, setMaterials] = useState<Material[]>([])
  const [materialProfiles, setMaterialProfiles] = useState<MaterialProfile[]>([])
  const [printers, setPrinters] = useState<Printer[]>([])
  const [removingPrinterId, setRemovingPrinterId] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // printer registration form
  const [name, setName] = useState('')
  const [profileId, setProfileId] = useState('')
  const [nozzle, setNozzle] = useState('')

  const reload = (): void => {
    void (async () => {
      try {
        const [m, p, mat, mp, registeredPrinters] = await Promise.all([
          api.machines(),
          api.machineProfiles(),
          api.materials(),
          api.materialProfiles(),
          api.printers(),
        ])
        setMachines(m)
        setProfiles(p)
        setMaterials(mat)
        setMaterialProfiles(mp)
        setPrinters(registeredPrinters)
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : 'Could not load the catalogue.')
      }
    })()
  }

  useEffect(reload, [])

  const selectedProfile = useMemo(
    () => profiles.find((p) => p.id === profileId) ?? null,
    [profiles, profileId],
  )

  async function register(event: React.FormEvent): Promise<void> {
    event.preventDefault()
    setError(null)
    setMessage(null)
    try {
      const created = await api.createPrinter({
        name,
        machine_profile_id: profileId,
        nozzle_diameter_mm: Number(nozzle),
        nozzle_material: 'brass',
      })
      setPrinters((current) => [...current, created])
      setMessage(`Registered "${created.name}".`)
      setName('')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Registration failed.')
    }
  }

  async function removePrinter(printer: Printer): Promise<void> {
    if (!window.confirm(`Remove "${printer.name}" from your active printer list? Existing job history will be kept.`)) {
      return
    }
    setError(null)
    setMessage(null)
    setRemovingPrinterId(printer.id)
    try {
      await api.deletePrinter(printer.id)
      setPrinters((current) => current.filter((item) => item.id !== printer.id))
      setMessage(`Removed "${printer.name}". Existing job history is preserved.`)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not remove this printer.')
    } finally {
      setRemovingPrinterId(null)
    }
  }

  if (section === 'materials') {
    return (
      <div className="space-y-5">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">Materials</h1>
          <p className="mt-1 text-sm text-slate-500">
            Manufacturer-specific profiles. Two &quot;PLA&quot; filaments are two profiles.
          </p>
        </div>
        {error && <p className="text-xs text-rose-400">{error}</p>}

        <section className="panel overflow-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-[11px] uppercase tracking-wider text-slate-500">
                <th className="px-4 py-2">Material</th>
                <th className="px-4 py-2">Profile</th>
                <th className="px-4 py-2">Nozzle °C</th>
                <th className="px-4 py-2">Bed °C</th>
                <th className="px-4 py-2">Max flow</th>
                <th className="px-4 py-2">Enclosure</th>
                <th className="px-4 py-2">Abrasion</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {materialProfiles.map((profile) => {
                const family = materials.find((m) => m.id === profile.material_id)
                return (
                  <tr key={profile.id} className="text-xs text-slate-300">
                    <td className="px-4 py-2">{family?.display_name ?? profile.material_id}</td>
                    <td className="px-4 py-2 font-mono text-slate-400">
                      {profile.vendor}/{profile.brand_sku}
                    </td>
                    <td className="px-4 py-2 font-mono">
                      {profile.nozzle_temp.min}–{profile.nozzle_temp.max}
                    </td>
                    <td className="px-4 py-2 font-mono">
                      {profile.bed_temp.min}–{profile.bed_temp.max}
                    </td>
                    <td className="px-4 py-2 font-mono">{profile.max_volumetric_flow_mm3s ?? '—'}</td>
                    <td className="px-4 py-2">
                      {profile.requires_enclosure ? (
                        <span className="text-amber-400">required</span>
                      ) : (
                        <span className="text-slate-600">no</span>
                      )}
                    </td>
                    <td className="px-4 py-2">{profile.abrasiveness}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </section>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">Printers</h1>
        <p className="mt-1 text-sm text-slate-500">
          Register a machine against a profile version. Unvalidated profiles are refused at slice
          time on purpose.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[360px_minmax(0,1fr)]">
        <section className="panel">
          <h2 className="panel-title">Register a printer</h2>
          <form onSubmit={register} className="space-y-3 p-4">
            <label className="block">
              <span className="field-label">Name</span>
              <input className="input" required value={name} onChange={(e) => setName(e.target.value)} />
            </label>

            <label className="block">
              <span className="field-label">Machine profile</span>
              <select
                className="input"
                required
                value={profileId}
                onChange={(e) => {
                  const next = e.target.value
                  setProfileId(next)
                  // The profile owns both the supported list and the default.
                  const profile = profiles.find((p) => p.id === next)
                  setNozzle(profile ? String(profile.default_nozzle_diameter_mm) : '')
                }}
              >
                <option value="">Select a profile…</option>
                {profiles.map((profile) => {
                  const machine = machines.find((m) => m.id === profile.machine_id)
                  return (
                    <option key={profile.id} value={profile.id}>
                      {machine?.vendor} {machine?.model} v{profile.profile_version} (
                      {profile.validation_status})
                    </option>
                  )
                })}
              </select>
            </label>

            <label className="block">
              <span className="field-label">Nozzle diameter</span>
              <select
                className="input"
                value={nozzle}
                onChange={(e) => setNozzle(e.target.value)}
                disabled={!selectedProfile}
              >
                {selectedProfile ? (
                  selectedProfile.nozzle_diameters.map((d) => (
                    <option key={d} value={d}>
                      {d} mm
                    </option>
                  ))
                ) : (
                  <option value="" disabled>
                    Select a profile first…
                  </option>
                )}
              </select>
            </label>

            {selectedProfile && (
              <p className="text-[11px] text-slate-500">
                Build volume {selectedProfile.build_volume.x} ×{' '}
                {selectedProfile.build_volume.y} × {selectedProfile.build_volume.z} mm ·{' '}
                {selectedProfile.validation_status === 'validated' ? (
                  <span className="text-emerald-400">✓ bench-validated</span>
                ) : (
                  <span className="text-amber-400">
                    ⚠ not validated ({selectedProfile.validation_reason || 'bench check pending'})
                  </span>
                )}
              </p>
            )}

            {message && <p className="text-xs text-emerald-400">{message}</p>}
            {error && <p className="text-xs text-rose-400">{error}</p>}

            <button type="submit" className="btn-primary w-full" disabled={!profileId}>
              Register
            </button>
          </form>
        </section>

        <div className="flex min-w-0 flex-col gap-4">
          <section className="panel overflow-auto">
            <h2 className="panel-title">Your printers</h2>
            {printers.length === 0 ? (
              <p className="p-4 text-xs text-slate-500">
                No active printers. Register one to select it in the slicer.
              </p>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-800 text-left text-[11px] uppercase tracking-wider text-slate-500">
                    <th className="px-4 py-2">Printer</th>
                    <th className="px-4 py-2">Machine profile</th>
                    <th className="px-4 py-2">Nozzle</th>
                    <th className="px-4 py-2">Status</th>
                    <th className="px-4 py-2" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {printers.map((printer) => {
                    const profile = profiles.find((item) => item.id === printer.machine_profile_id)
                    const machine = machines.find((item) => item.id === profile?.machine_id)
                    return (
                      <tr key={printer.id} className="text-xs text-slate-300">
                        <td className="px-4 py-2 font-medium">{printer.name}</td>
                        <td className="px-4 py-2 text-slate-500">
                          {machine ? `${machine.vendor} ${machine.model} v${profile?.profile_version}` : 'Profile unavailable'}
                        </td>
                        <td className="px-4 py-2 font-mono">{printer.nozzle_diameter_mm} mm</td>
                        <td className="px-4 py-2">
                          {profile?.validation_status === 'validated' ? 'validated' : 'unvalidated'}
                          {printer.is_default ? ' · default' : ''}
                        </td>
                        <td className="px-4 py-2 text-right">
                          <button
                            type="button"
                            onClick={() => void removePrinter(printer)}
                            disabled={removingPrinterId === printer.id}
                            className="rounded border border-slate-300 px-2 py-1 text-[10px] font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-50"
                            aria-label={`Remove ${printer.name}`}
                          >
                            {removingPrinterId === printer.id ? 'Removing…' : 'Remove'}
                          </button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
          </section>

          <section className="panel overflow-auto">
            <h2 className="panel-title">Machine knowledge base</h2>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-[11px] uppercase tracking-wider text-slate-500">
                <th className="px-4 py-2">Machine</th>
                <th className="px-4 py-2">Tech</th>
                <th className="px-4 py-2">Motion</th>
                <th className="px-4 py-2">Build volume</th>
                <th className="px-4 py-2">Validation</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {profiles.map((profile) => {
                const machine = machines.find((m) => m.id === profile.machine_id)
                return (
                  <tr key={profile.id} className="text-xs text-slate-300">
                    <td className="px-4 py-2">
                      {machine?.vendor} {machine?.model}
                      <span className="ml-1 text-slate-600">v{profile.profile_version}</span>
                    </td>
                    <td className="px-4 py-2">{machine?.technology}</td>
                    <td className="px-4 py-2">{machine?.motion_system}</td>
                    <td className="px-4 py-2 font-mono">
                      {profile.build_volume.x}×{profile.build_volume.y}×{profile.build_volume.z}
                    </td>
                    <td className="px-4 py-2">
                      {profile.validation_status === 'validated' ? (
                        <span className="text-emerald-400">validated</span>
                      ) : (
                        <span className="text-amber-400">unvalidated</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          </section>
        </div>
      </div>
    </div>
  )
}
