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
    <div className="login-page flex min-h-full items-center justify-center p-5 sm:p-8">
      <div className="login-card panel w-full max-w-sm p-6 sm:p-8">
          <div className="mb-7 flex items-center gap-3">
            <span className="brand-mark" aria-hidden="true">S</span>
            <div>
              <p className="text-[10px] font-semibold uppercase text-slate-500">FDM workspace</p>
              <h1 className="text-lg font-semibold text-slate-100">AI Slicing Platform</h1>
            </div>
          </div>
          <p className="mb-1 text-xl font-semibold text-slate-100">
            {mode === 'login' ? 'Welcome back' : 'Create your account'}
          </p>
          <p className="text-xs text-slate-500">{mode === 'login' ? 'Sign in to continue.' : 'Register to create a workspace.'}</p>

          <form onSubmit={submit} className="mt-6 space-y-4">
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
              <p className="rounded border border-slate-800 bg-slate-100 px-3 py-2 text-xs text-slate-900">{error}</p>
            )}

            <button type="submit" disabled={busy} className="btn-primary w-full">
              {busy ? 'Working…' : mode === 'login' ? 'Sign in' : 'Create account'}
            </button>
          </form>

          <button
            type="button"
            onClick={() => setMode(mode === 'login' ? 'register' : 'login')}
            className="mt-5 w-full text-center text-xs text-slate-500 hover:text-slate-300"
          >
            {mode === 'login' ? 'Need an account? Register' : 'Have an account? Sign in'}
          </button>
        </div>
    </div>
  )
}
