import { useLocation } from 'react-router-dom'
import { isMenuScrollRoute, useMenuScroll } from './MenuScrollContext'
import './MenuScrollOverlay.css'

/**
 * Decorative parchment scroll ends framing menu pages (issue #44).
 * Idle visibility is route-driven; close/open phases come from MenuScrollProvider
 * during in-app menu→menu navigations.
 */
export default function MenuScrollOverlay() {
  const { pathname } = useLocation()
  const { phase } = useMenuScroll()

  let stateClass = ''
  if (phase === 'closing') {
    stateClass = ' menu-scroll-overlay--closing'
  } else if (phase === 'opening') {
    stateClass = ' menu-scroll-overlay--opening'
  } else if (!isMenuScrollRoute(pathname)) {
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
