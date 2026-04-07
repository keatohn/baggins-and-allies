import { StrictMode, Component, type ErrorInfo, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route, useParams, useLocation, Navigate } from 'react-router-dom'
import './index.css'
import App from './App.tsx'
import MainMenu from './pages/MainMenu.tsx'
import Login from './pages/Login.tsx'
import Register from './pages/Register.tsx'
import CreateGame from './pages/CreateGame.tsx'
import GameList from './pages/GameList.tsx'
import JoinGame from './pages/JoinGame.tsx'
import Profile from './pages/Profile.tsx'
import Admin from './pages/Admin.tsx'
import {
  playUiClickSound,
  resumeMenuAmbienceIfPaused,
  resumeTurnMusicIfPaused,
  startMenuAmbience,
  stopMenuAmbience,
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
      resumeMenuAmbienceIfPaused()
      resumeTurnMusicIfPaused()

      const target = event.target
      const pathname = window.location.pathname || ''
      if (!(target instanceof Element)) return
      if (!target.closest(CLICKABLE_SELECTOR)) return
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
    const path = location.pathname
    const isMenuRoute =
      path === '/' ||
      path === '/login' ||
      path === '/register' ||
      path === '/games' ||
      path === '/join' ||
      path === '/profile' ||
      path === '/admin' ||
      path === '/game/new'

    if (isMenuRoute) startMenuAmbience('menu')
    else stopMenuAmbience()
  }, [location.pathname])

  return null
}

const rootEl = document.getElementById('root')
if (!rootEl) {
  document.body.innerHTML = '<div style="padding:2rem;font-family:system-ui;">Root element #root not found.</div>'
} else {
  createRoot(rootEl).render(
    <StrictMode>
      <AppErrorBoundary>
        <BrowserRouter>
          <MenuAmbienceController />
          <Routes>
            <Route path="/" element={<MainMenu />} />
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
            <Route path="/games" element={<GameList />} />
            <Route path="/join" element={<JoinGame />} />
            <Route path="/profile" element={<Profile />} />
            <Route path="/admin" element={<Admin />} />
            <Route path="/game/:gameId" element={<GameRoute />} />
          </Routes>
        </BrowserRouter>
      </AppErrorBoundary>
    </StrictMode>,
  )
}
