import { useLocation } from 'react-router-dom'
import { isMenuScrollRoute } from './MenuScrollContext'
import './MenuMapBackdrop.css'

/** Middle-earth map behind every menu page. The home page keeps it stronger. */
export default function MenuMapBackdrop() {
  const { pathname } = useLocation()
  if (!isMenuScrollRoute(pathname)) return null
  const home = pathname === '/'
  return (
    <div
      className={`menu-map-backdrop${home ? ' menu-map-backdrop--home' : ''}`}
      aria-hidden="true"
    />
  )
}
