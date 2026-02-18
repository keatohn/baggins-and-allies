import { StrictMode } from 'react'
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

function GameRoute() {
  const { gameId } = useParams<{ gameId: string }>()
  if (!gameId) return <Navigate to="/" replace />
  if (gameId === 'new') return <CreateGame />
  return <App gameId={gameId} />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
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
  </StrictMode>,
)
