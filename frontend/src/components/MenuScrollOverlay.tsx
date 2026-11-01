import { useLocation } from 'react-router-dom'
import { isMenuScrollRoute, useMenuScroll } from './MenuScrollContext'
import './MenuScrollOverlay.css'

/**
 * Decorative parchment scroll ends framing menu pages (issue #44).
 * Idle visibility is route-driven; transition phases come from MenuScrollProvider:
 * closing/covering hide the outgoing page (game → menu flies the panels in from
 * off-screen to center), opening/exiting resolve a menu departure.
 */
export default function MenuScrollOverlay() {
  const { pathname } = useLocation()
  const { phase } = useMenuScroll()

  let stateClass = ''
  if (phase === 'closing' || phase === 'covering') {
    stateClass = ' menu-scroll-overlay--closing'
  } else if (phase === 'opening') {
    stateClass = ' menu-scroll-overlay--opening'
  } else if (phase === 'exiting' || !isMenuScrollRoute(pathname)) {
    stateClass = ' menu-scroll-overlay--hidden'
  }

  return (
    <div className={`menu-scroll-overlay${stateClass}`} aria-hidden="true">
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
