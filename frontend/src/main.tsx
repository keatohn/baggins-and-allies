import { StrictMode, Component, type ErrorInfo, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route, useParams, Navigate } from 'react-router-dom'
import './index.css'
import App from './App.tsx'
import MainMenu from './pages/MainMenu.tsx'
import Login from './pages/Login.tsx'
import Register from './pages/Register.tsx'
import CreateGame from './pages/CreateGame.tsx'
import GameList from './pages/GameList.tsx'
import JoinGame from './pages/JoinGame.tsx'
import Profile from './pages/Profile.tsx'

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
  if (!gameId) return <Navigate to="/" replace />
  if (gameId === 'new') return <CreateGame />
  return <App gameId={gameId} />
}

const rootEl = document.getElementById('root')
if (!rootEl) {
  document.body.innerHTML = '<div style="padding:2rem;font-family:system-ui;">Root element #root not found.</div>'
} else {
  createRoot(rootEl).render(
    <StrictMode>
      <AppErrorBoundary>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<MainMenu />} />
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
            <Route path="/games" element={<GameList />} />
            <Route path="/join" element={<JoinGame />} />
            <Route path="/profile" element={<Profile />} />
            <Route path="/game/:gameId" element={<GameRoute />} />
          </Routes>
        </BrowserRouter>
      </AppErrorBoundary>
    </StrictMode>,
  )
}
