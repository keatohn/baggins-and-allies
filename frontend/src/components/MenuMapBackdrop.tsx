import { useLocation } from 'react-router-dom'
import { isMenuScrollRoute } from './MenuScrollContext'
import './MenuMapBackdrop.css'

/** Middle-earth map behind menu pages and, more faintly, the game chrome. */
export default function MenuMapBackdrop() {
  const { pathname } = useLocation()
  const game = pathname.startsWith('/game/') && pathname !== '/game/new'
  if (!isMenuScrollRoute(pathname) && !game) return null
  const home = pathname === '/'
  const tone = home ? ' menu-map-backdrop--home' : game ? ' menu-map-backdrop--game' : ''
  return <div className={`menu-map-backdrop${tone}`} aria-hidden="true" />
}
