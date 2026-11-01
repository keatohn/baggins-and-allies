/* eslint-disable react-refresh/only-export-components -- state-machine module: route predicate + hook + provider belong together. */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { useLocation, useNavigate, type NavigateOptions } from 'react-router-dom'

/**
 * Routes framed by decorative scroll ends. Includes auth pages (part of the
 * menu flow per issue #44); the in-game map (`/game/:id`) is excluded so the
 * scrolls sit off-screen during play.
 */
export function isMenuScrollRoute(pathname: string): boolean {
  return (
    pathname === '/' ||
    pathname === '/login' ||
    pathname === '/register' ||
    pathname === '/forgot-password' ||
    pathname === '/games' ||
    pathname === '/join' ||
    pathname === '/profile' ||
    pathname === '/admin' ||
    pathname === '/game/new'
  )
}

/** Must match the `transform` transition duration in MenuScrollOverlay.css. */
const SCROLL_TRANSITION_MS = 300

/**
 * `closing` — panels slide to center, covering the outgoing menu page.
 * `opening` — panels reopen to the screen edges (menu → menu).
 * `exiting`  — panels continue off-screen and stay hidden (menu → game).
 * `entering` — panels fly in from off-screen to the edges (game → menu).
 */
type ScrollTransitionPhase = 'idle' | 'closing' | 'opening' | 'exiting' | 'entering'

interface MenuScrollContextValue {
  phase: ScrollTransitionPhase
  /** Animate the scroll ends around an in-app navigation. No-op while animating. */
  navigateWithScroll: (to: string, opts?: NavigateOptions) => void
}

const MenuScrollContext = createContext<MenuScrollContextValue | null>(null)

function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

export function MenuScrollProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  const location = useLocation()
  const [phase, setPhase] = useState<ScrollTransitionPhase>('idle')
  const phaseRef = useRef<ScrollTransitionPhase>('idle')
  const closeTimerRef = useRef<number | null>(null)
  const openTimerRef = useRef<number | null>(null)

  const clearTimers = useCallback(() => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current)
      closeTimerRef.current = null
    }
    if (openTimerRef.current !== null) {
      window.clearTimeout(openTimerRef.current)
      openTimerRef.current = null
    }
  }, [])

  useEffect(() => clearTimers, [clearTimers])

  const applyPhase = useCallback((next: ScrollTransitionPhase) => {
    phaseRef.current = next
    setPhase(next)
  }, [])

  const scheduleSettle = useCallback(() => {
    openTimerRef.current = window.setTimeout(() => {
      openTimerRef.current = null
      phaseRef.current = 'idle'
      setPhase('idle')
    }, SCROLL_TRANSITION_MS)
  }, [])

  const navigateWithScroll = useCallback(
    (to: string, opts?: NavigateOptions) => {
      if (phaseRef.current !== 'idle') return

      const toPath = to.split('?')[0].split('#')[0]
      const fromMenu = isMenuScrollRoute(location.pathname)
      const toMenu = isMenuScrollRoute(toPath)

      if (prefersReducedMotion() || (!fromMenu && !toMenu)) {
        navigate(to, opts)
        return
      }

      if (!fromMenu) {
        applyPhase('entering')
        navigate(to, opts)
        scheduleSettle()
        return
      }

      applyPhase('closing')
      closeTimerRef.current = window.setTimeout(() => {
        closeTimerRef.current = null
        navigate(to, opts)
        applyPhase(toMenu ? 'opening' : 'exiting')
        scheduleSettle()
      }, SCROLL_TRANSITION_MS)
    },
    [navigate, location.pathname, applyPhase, scheduleSettle],
  )

  const contextValue: MenuScrollContextValue = { phase, navigateWithScroll }

  return (
    <MenuScrollContext.Provider value={contextValue}>
      {children}
    </MenuScrollContext.Provider>
  )
}

export function useMenuScroll(): MenuScrollContextValue {
  const context = useContext(MenuScrollContext)
  if (!context) {
    throw new Error('useMenuScroll must be used within MenuScrollProvider')
  }
  return context
}
