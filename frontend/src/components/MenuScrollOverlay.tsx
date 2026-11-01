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
      <svg className="menu-scroll-overlay__filters" aria-hidden="true">
        <filter id="scroll-paper-ruffle" x="-8%" y="-8%" width="116%" height="116%">
          <feTurbulence type="fractalNoise" baseFrequency="0.012 0.02" numOctaves="3" seed="7" result="warp" />
          <feDisplacementMap in="SourceGraphic" in2="warp" scale="46" xChannelSelector="R" yChannelSelector="G" />
        </filter>
      </svg>
      <div className="menu-scroll-panel menu-scroll-panel--left">
        <div className="menu-scroll-panel__face">
          <div className="menu-scroll-panel__paper" />
        </div>
        <div className="menu-scroll-panel__roll">
          <div className="menu-scroll-panel__roll-body" />
        </div>
      </div>
      <div className="menu-scroll-panel menu-scroll-panel--right">
        <div className="menu-scroll-panel__roll">
          <div className="menu-scroll-panel__roll-body" />
        </div>
        <div className="menu-scroll-panel__face">
          <div className="menu-scroll-panel__paper" />
        </div>
      </div>
    </div>
  )
}
