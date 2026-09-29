import { useEffect, useMemo, useState } from 'react'
import { ApiError, api, type Machine, type MachineProfile, type Material, type MaterialProfile } from '../lib/api'

export default function Catalog({ section }: { section: 'printers' | 'materials' }): JSX.Element {
  const [machines, setMachines] = useState<Machine[]>([])
  const [profiles, setProfiles] = useState<MachineProfile[]>([])
  const [materials, setMaterials] = useState<Material[]>([])
  const [materialProfiles, setMaterialProfiles] = useState<MaterialProfile[]>([])
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // printer registration form
  const [name, setName] = useState('')
  const [profileId, setProfileId] = useState('')
  const [nozzle, setNozzle] = useState('0.4')

  const reload = (): void => {
    void (async () => {
      try {
        const [m, p, mat, mp] = await Promise.all([
          api.machines(),
          api.machineProfiles(),
          api.materials(),
          api.materialProfiles(),
        ])
        setMachines(m)
        setProfiles(p)
        setMaterials(mat)
        setMaterialProfiles(mp)
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
      setMessage(`Registered "${created.name}".`)
      setName('')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Registration failed.')
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

      <div className="grid grid-cols-[380px_1fr] gap-4">
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
                onChange={(e) => setProfileId(e.target.value)}
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
                {(selectedProfile?.nozzle_diameters ?? [0.4]).map((d) => (
                  <option key={d} value={d}>
                    {d} mm
                  </option>
                ))}
              </select>
            </label>

            {selectedProfile && (
              <p className="text-[11px] text-slate-500">
                Build volume {selectedProfile.build_volume.x} ×{' '}
                {selectedProfile.build_volume.y} × {selectedProfile.build_volume.z} mm ·{' '}
                {selectedProfile.validation_status === 'validated' ? (
                  <span className="text-emerald-400">bench-validated</span>
                ) : (
                  <span className="text-amber-400">unvalidated — slicing will be refused</span>
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
  )
}
