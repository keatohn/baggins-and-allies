import { useLocation } from 'react-router-dom'
import './MenuScrollOverlay.css'

/**
 * Routes framed by decorative scroll ends. Includes auth pages (part of the
 * menu flow per issue #44); the in-game map (`/game/:id`) is excluded so the
 * scrolls sit off-screen during play.
 */
function isMenuScrollRoute(pathname: string): boolean {
  return (
    pathname === '/' ||
    pathname === '/login' ||
    pathname === '/register' ||
    pathname === '/games' ||
    pathname === '/join' ||
    pathname === '/profile' ||
    pathname === '/admin' ||
    pathname === '/game/new'
  )
}

/** Purely decorative; phase 1 of menu scroll transitions (issue #44). */
export default function MenuScrollOverlay() {
  const { pathname } = useLocation()
  const visible = isMenuScrollRoute(pathname)

  return (
    <div
      className={`menu-scroll-overlay${visible ? '' : ' menu-scroll-overlay--hidden'}`}
      aria-hidden="true"
    >
      <div className="menu-scroll-panel menu-scroll-panel--left">
        <div className="menu-scroll-panel__face" />
        <div className="menu-scroll-panel__roll" />
      </div>
      <div className="menu-scroll-panel menu-scroll-panel--right">
        <div className="menu-scroll-panel__roll" />
        <div className="menu-scroll-panel__face" />
      </div>
    </div>
  )
}
