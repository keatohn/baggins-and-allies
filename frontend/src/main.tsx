import { StrictMode, Component, type ErrorInfo, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route, useParams, useLocation, Navigate } from 'react-router-dom'
import './index.css'
import App from './App.tsx'
import MainMenu from './pages/MainMenu.tsx'
import Login from './pages/Login.tsx'
import Register from './pages/Register.tsx'
import ForgotPassword from './pages/ForgotPassword.tsx'
import CreateGame from './pages/CreateGame.tsx'
import GameList from './pages/GameList.tsx'
import JoinGame from './pages/JoinGame.tsx'
import Profile from './pages/Profile.tsx'
import Admin from './pages/Admin.tsx'
import { MenuScrollProvider } from './components/MenuScrollContext'
import MenuScrollOverlay from './components/MenuScrollOverlay'
import api, { AUTH_CHANGED_EVENT, getAuthToken } from './services/api'
import {
  playUiClickSound,
  resumeGameAudioContext,
  resumeMenuAmbienceIfPaused,
  resumeTurnMusicIfPaused,
  setMenuRouteAmbienceAllowed,
} from './audio/gameAudio'
import { useEffect } from 'react'

const CLICKABLE_SELECTOR = [
  'button',
  'a[href]',
  '[role="button"]',
  '[role="tab"]',
  '[role="menuitem"]',
  // No checkbox/radio/range: situational toggles and repeated pointerdown while dragging sliders.
  '[data-sfx-click="true"]',
].join(', ')

function isActiveGameRoute(pathname: string): boolean {
  return pathname.startsWith('/game/') && pathname !== '/game/new'
}

function isLoggedInMenuRoute(pathname: string): boolean {
  return (
    pathname === '/' ||
    pathname === '/games' ||
    pathname === '/join' ||
    pathname === '/profile/stats' ||
    pathname === '/admin' ||
    pathname === '/game/new'
  )
}

function isAdminMajorClick(target: Element): boolean {
  return Boolean(target.closest('[data-admin-major-sfx]') || target.closest('.page-menu-btn'));
}

function isMajorInGameClick(target: Element): boolean {
  if (target.closest('.actions-panel')) {
    // Explicitly exclude "minor" actions in the Actions tab.
    if (
      target.closest(
        '.move-confirm .confirm-move-btn, .cancel-move-btn, .cancel-move-x, .confirm-no, .cancel-retreat, .charge-path-btn, .confirm-dialog',
      )
    ) {
      return false
    }
    return Boolean(
      target.closest(
        '.actions-panel #btn-purchase, .actions-panel .primary, .actions-panel .confirm-yes, .actions-panel .battle-btn, .actions-panel .retreat-option',
      ),
    )
  }
  return Boolean(
    target.closest('[data-sfx-click="true"]') ||
      target.closest('.header'),
  )
}

if (typeof document !== 'undefined') {
  document.addEventListener(
    'pointerdown',
    (event) => {
      resumeGameAudioContext()
      if (getAuthToken()) {
        resumeMenuAmbienceIfPaused()
        resumeTurnMusicIfPaused()
      }

      const target = event.target
      const pathname = window.location.pathname || ''
      if (!(target instanceof Element)) return
      if (!target.closest(CLICKABLE_SELECTOR)) return
      if (!getAuthToken()) return
      if (pathname === '/admin' && !isAdminMajorClick(target)) return
      if (isActiveGameRoute(pathname) && !isMajorInGameClick(target)) return
      if (target.closest('[data-no-ui-click-sfx]')) return
      playUiClickSound()
    },
    { capture: true },
  )
}

class AppErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('App error:', error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{
          padding: '2rem',
          fontFamily: 'system-ui, sans-serif',
          maxWidth: '600px',
          margin: '2rem auto',
          color: '#333',
          background: '#f8d7da',
          border: '1px solid #f5c2c7',
          borderRadius: '8px',
        }}>
          <h2 style={{ marginTop: 0 }}>Something went wrong</h2>
          <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: '0.9rem' }}>
            {this.state.error.message}
          </pre>
          <p style={{ fontSize: '0.85rem', color: '#666' }}>
            Open the browser Console (F12 → Console) for more details.
          </p>
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            style={{ marginTop: '1rem', padding: '0.5rem 1rem', cursor: 'pointer' }}
          >
            Try again
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

function GameRoute() {
  const { gameId } = useParams<{ gameId: string }>()
  const location = useLocation()
  if (!gameId) return <Navigate to="/" replace />
  if (gameId === 'new') return <CreateGame />
  const navState = location.state as { initialState?: import('./services/api').ApiGameState; gameId?: string } | undefined
  const initialState = navState?.gameId === gameId ? navState.initialState : undefined
  return <App gameId={gameId} initialState={initialState} />
}

function MenuAmbienceController() {
  const location = useLocation()

  useEffect(() => {
    const sync = () => {
      setMenuRouteAmbienceAllowed(Boolean(getAuthToken()) && isLoggedInMenuRoute(location.pathname))
    }
    sync()
    window.addEventListener(AUTH_CHANGED_EVENT, sync)
    return () => window.removeEventListener(AUTH_CHANGED_EVENT, sync)
  }, [location.pathname])

  return null
}

const GAINS_RETRY_MS = 15000
const GAINS_REFRESH_MIN_MS = 30000
let gainsFetching = false
let gainsLastAttemptAt = 0
let gainsRetryTimer: number | null = null

/**
 * Admin per-file gains and the menu playlist are fetched at boot and re-fetched
 * whenever the tab becomes active again (or comes back online), so long-lived
 * mobile tabs pick up admin saves instead of keeping the mix they loaded with.
 * A failed fetch retries on a timer.
 */
async function refreshAudioGains(minIntervalMs: number): Promise<void> {
  if (gainsFetching) return
  if (minIntervalMs > 0 && Date.now() - gainsLastAttemptAt < minIntervalMs) return
  gainsLastAttemptAt = Date.now()
  gainsFetching = true
  try {
    await api.getAudioGains()
    if (gainsRetryTimer !== null) {
      window.clearTimeout(gainsRetryTimer)
      gainsRetryTimer = null
    }
  } catch {
    if (gainsRetryTimer === null) {
      gainsRetryTimer = window.setTimeout(() => {
        gainsRetryTimer = null
        void refreshAudioGains(0)
      }, GAINS_RETRY_MS)
    }
  } finally {
    gainsFetching = false
  }
}

void refreshAudioGains(0)

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return
    void refreshAudioGains(GAINS_REFRESH_MIN_MS)
  })
}
window.addEventListener('online', () => void refreshAudioGains(0))
window.addEventListener('focus', () => void refreshAudioGains(GAINS_REFRESH_MIN_MS))

const rootEl = document.getElementById('root')
if (!rootEl) {
  document.body.innerHTML = '<div style="padding:2rem;font-family:system-ui;">Root element #root not found.</div>'
} else {
  createRoot(rootEl).render(
    <StrictMode>
      <AppErrorBoundary>
        <BrowserRouter>
          <MenuAmbienceController />
          <MenuScrollProvider>
            <MenuScrollOverlay />
            <Routes>
              <Route path="/" element={<MainMenu />} />
              <Route path="/login" element={<Login />} />
              <Route path="/register" element={<Register />} />
              <Route path="/forgot-password" element={<ForgotPassword />} />
              <Route path="/games" element={<GameList />} />
              <Route path="/join" element={<JoinGame />} />
              <Route path="/profile/stats" element={<Profile />} />
              {/* Old profile page → menu with the Profile modal open. */}
              <Route path="/profile" element={<Navigate to="/?modal=profile" replace />} />
              <Route path="/admin" element={<Admin />} />
              <Route path="/game/:gameId" element={<GameRoute />} />
            </Routes>
          </MenuScrollProvider>
        </BrowserRouter>
      </AppErrorBoundary>
    </StrictMode>,
  )
}
