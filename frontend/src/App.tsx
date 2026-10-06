import { useEffect, useState } from 'react'
import { Routes, Route, Navigate, useNavigate, useLocation } from 'react-router-dom'
import { api, getToken, setToken, type User } from './lib/api'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Workbench from './pages/Workbench'
import Jobs from './pages/Jobs'
import Catalog from './pages/Catalog'
import Simulation from './pages/Simulation'
import Settings from './pages/Settings'

function Shell({
  user,
  onSignOut,
  children,
}: {
  user: User
  onSignOut: () => void
  children: React.ReactNode
}): JSX.Element {
  const navigate = useNavigate()
  const location = useLocation()

  const nav: Array<{ label: string; path: string; enabled: boolean }> = [
    { label: 'Dashboard', path: '/', enabled: true },
    { label: 'Slicer', path: '/slicer', enabled: true },
    { label: 'Jobs', path: '/jobs', enabled: true },
    { label: 'Printers', path: '/printers', enabled: true },
    { label: 'Materials', path: '/materials', enabled: true },
    { label: 'Simulation', path: '/simulation', enabled: true },
    { label: 'Settings', path: '/settings', enabled: true },
  ]

  return (
    <div className="app-shell flex h-full flex-col">
      <header className="app-header flex flex-wrap items-center justify-between gap-x-5 gap-y-3 border-b border-slate-800 bg-slate-900/70 px-5 py-3">
        <div className="flex min-w-0 flex-wrap items-center gap-x-6 gap-y-2">
          <span className="brand-name text-sm font-semibold tracking-tight text-slate-100">
            AI Slicing Platform
          </span>
          <nav className="app-nav flex max-w-full gap-1 overflow-x-auto">
            {nav.map((item) => (
              <button
                key={item.label}
                type="button"
                disabled={!item.enabled}
                title={item.enabled ? undefined : 'NOT IMPLEMENTED yet'}
                onClick={() => navigate(item.path)}
                className={[
                  'rounded px-2.5 py-1.5 text-xs font-medium transition-colors',
                  location.pathname === item.path
                    ? 'bg-slate-800 text-sky-300'
                    : 'text-slate-400 hover:bg-slate-800/60',
                  item.enabled ? '' : 'cursor-not-allowed opacity-40',
                ].join(' ')}
              >
                {item.label}
                {!item.enabled && <span className="ml-1 text-[9px]">soon</span>}
              </button>
            ))}
          </nav>
        </div>
        <div className="flex items-center gap-3 text-xs text-slate-400">
          <span className="max-w-36 truncate">{user.display_name}</span>
          <button type="button" onClick={onSignOut} className="text-slate-500 hover:text-slate-300">
            sign out
          </button>
        </div>
      </header>

      <main className="min-h-0 flex-1 overflow-auto p-3 md:p-5">{children}</main>

      <footer className="border-t border-slate-800 px-5 py-2 text-[10px] text-slate-600">
        Digital validation only. Physical print quality also depends on bed levelling, calibration,
        filament condition, ambient temperature, mechanical wear and the machine&apos;s current state.
      </footer>
    </div>
  )
}

export default function App(): JSX.Element {
  const [user, setUser] = useState<User | null>(null)
  const [checked, setChecked] = useState(false)

  useEffect(() => {
    if (!getToken()) {
      setChecked(true)
      return
    }
    api
      .me()
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setChecked(true))
  }, [])

  if (!checked) {
    return <div className="flex h-full items-center justify-center text-sm text-slate-500">loading…</div>
  }

  if (!user) {
    return (
      <Login
        onAuthenticated={(token, nextUser) => {
          setToken(token)
          setUser(nextUser)
        }}
      />
    )
  }

  return (
    <Shell
      user={user}
      onSignOut={() => {
        setToken(null)
        setUser(null)
      }}
    >
      <Routes>
        <Route path="/" element={<Dashboard user={user} />} />
        <Route path="/slicer" element={<Workbench />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/jobs/:id" element={<Jobs />} />
        <Route path="/printers" element={<Catalog section="printers" />} />
        <Route path="/materials" element={<Catalog section="materials" />} />
        <Route path="/simulation" element={<Simulation />} />
        <Route path="/settings" element={<Settings user={user} onSignOut={() => { setToken(null); setUser(null) }} />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  )
}
