import { useState } from 'react'
import { ApiError, api, type TokenResponse } from '../lib/api'

export default function Login({
  onAuthenticated,
}: {
  onAuthenticated: (token: string, user: TokenResponse['user']) => void
}): JSX.Element {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent): Promise<void> {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const result =
        mode === 'login'
          ? await api.login(email, password)
          : await api.register(email, password, displayName || email.split('@')[0] || 'user')
      onAuthenticated(result.access_token, result.user)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Request failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="w-full max-w-sm">
        <div className="panel p-6">
          <h1 className="text-lg font-semibold text-slate-100">AI Slicing Platform</h1>
          <p className="mt-1 text-xs text-slate-500">
            Upload a model, pick a printer, get a validated toolpath.
          </p>

          <form onSubmit={submit} className="mt-6 space-y-3">
            {mode === 'register' && (
              <div>
                <label className="field-label" htmlFor="name">
                  Display name
                </label>
                <input
                  id="name"
                  className="input"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  autoComplete="name"
                />
              </div>
            )}

            <div>
              <label className="field-label" htmlFor="email">
                Email
              </label>
              <input
                id="email"
                className="input"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
              />
            </div>

            <div>
              <label className="field-label" htmlFor="password">
                Password
              </label>
              <input
                id="password"
                className="input"
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              />
              {mode === 'register' && (
                <p className="mt-1 text-[10px] text-slate-600">
                  At least 12 characters. Length beats complexity.
                </p>
              )}
            </div>

            {error && (
              <p className="rounded bg-rose-950/60 px-3 py-2 text-xs text-rose-300">{error}</p>
            )}

            <button type="submit" disabled={busy} className="btn-primary w-full">
              {busy ? 'Working…' : mode === 'login' ? 'Sign in' : 'Create account'}
            </button>
          </form>

          <button
            type="button"
            onClick={() => setMode(mode === 'login' ? 'register' : 'login')}
            className="mt-4 w-full text-center text-xs text-slate-500 hover:text-slate-300"
          >
            {mode === 'login' ? 'Need an account? Register' : 'Have an account? Sign in'}
          </button>
        </div>
      </div>
    </div>
  )
}
