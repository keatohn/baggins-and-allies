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
    pathname === '/admin' ||
    pathname === '/game/new'
  )
}

/** Must match the `transform` transition duration in MenuScrollOverlay.css. */
const SCROLL_TRANSITION_MS = 300

/**
 * Grace period after navigate() before auto-revealing, giving the destination
 * page's mount effects time to call holdScroll() if it has async loading.
 */
const AUTO_REVEAL_MS = 150

/**
 * `closing`  — panels slide to center, covering the outgoing page.
 * `covering` — panels stay met in the center while the destination page loads.
 * `opening`  — panels reopen to the screen edges (menu destination).
 * `exiting`  — panels continue off-screen and stay hidden (menu → game).
 */
type ScrollTransitionPhase =
  | 'idle'
  | 'closing'
  | 'covering'
  | 'opening'
  | 'exiting'

interface MenuScrollContextValue {
  phase: ScrollTransitionPhase
  /** Animate the scroll ends around an in-app navigation. No-op while animating. */
  navigateWithScroll: (to: string, opts?: NavigateOptions) => void
  /**
   * Close the scroll ends, run `action` while covered (e.g. an in-page step
   * swap), then reopen. No-ops the animation if already animating or reduced
   * motion is preferred — `action` still runs.
   */
  withScrollTransition: (action: () => void) => void
  /**
   * Cancel the auto-reveal timer. Call from a destination page's mount effect
   * when it has async loading, so the scroll stays closed until revealScroll().
   */
  holdScroll: () => void
  /** Reveal after a hold: opens to edges (menu) or exits off-screen (game). */
  revealScroll: () => void
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
  const settleTimerRef = useRef<number | null>(null)
  const autoRevealTimerRef = useRef<number | null>(null)
  /** Whether the pending destination is a menu route (opening) or game (exiting). */
  const pendingToMenuRef = useRef<boolean | null>(null)
  /** True once a destination page has called holdScroll() for the current covering. */
  const holdClaimedRef = useRef(false)

  const clearTimer = useCallback((ref: React.MutableRefObject<number | null>) => {
    if (ref.current !== null) {
      window.clearTimeout(ref.current)
      ref.current = null
    }
  }, [])

  useEffect(
    () => () => {
      clearTimer(closeTimerRef)
      clearTimer(settleTimerRef)
      clearTimer(autoRevealTimerRef)
    },
    [clearTimer],
  )

  const applyPhase = useCallback((next: ScrollTransitionPhase) => {
    phaseRef.current = next
    setPhase(next)
  }, [])

  const scheduleSettle = useCallback(() => {
    settleTimerRef.current = window.setTimeout(() => {
      settleTimerRef.current = null
      phaseRef.current = 'idle'
      setPhase('idle')
    }, SCROLL_TRANSITION_MS)
  }, [])

  /** From covering: open to edges (menu) or exit off-screen (game), then settle. */
  const proceedToReveal = useCallback(() => {
    clearTimer(autoRevealTimerRef)
    const toMenu = pendingToMenuRef.current ?? true
    pendingToMenuRef.current = null
    holdClaimedRef.current = false
    applyPhase(toMenu ? 'opening' : 'exiting')
    scheduleSettle()
  }, [applyPhase, scheduleSettle, clearTimer])

  /**
   * Shared "close, swap content underneath, reopen" step for navigateWithScroll
   * and withScrollTransition. Destination pages may claim holdScroll() while
   * covered; otherwise auto-reveal runs after a short grace period.
   */
  const coverThenReveal = useCallback(
    (runUnderCover: () => void, toMenu: boolean) => {
      pendingToMenuRef.current = toMenu
      holdClaimedRef.current = false
      runUnderCover()
      applyPhase('covering')
      autoRevealTimerRef.current = window.setTimeout(() => {
        autoRevealTimerRef.current = null
        // Auto-reveal only if no page claimed the hold (no async loading).
        if (!holdClaimedRef.current && phaseRef.current === 'covering') {
          proceedToReveal()
        }
      }, AUTO_REVEAL_MS)
    },
    [applyPhase, proceedToReveal],
  )

  const holdScroll = useCallback(() => {
    if (phaseRef.current !== 'covering') return
    holdClaimedRef.current = true
    clearTimer(autoRevealTimerRef)
  }, [clearTimer])

  const revealScroll = useCallback(() => {
    if (phaseRef.current !== 'covering') return
    proceedToReveal()
  }, [proceedToReveal])

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

      // Game → menu: fly the panels in from off-screen to cover the game view
      // (same close phase as menu → menu; --closing forces them to center from
      // wherever they are), then open onto the freshly mounted menu page.
      applyPhase('closing')
      closeTimerRef.current = window.setTimeout(() => {
        closeTimerRef.current = null
        coverThenReveal(() => navigate(to, opts), toMenu)
      }, SCROLL_TRANSITION_MS)
    },
    [navigate, location.pathname, applyPhase, coverThenReveal],
  )

  const withScrollTransition = useCallback(
    (action: () => void) => {
      if (phaseRef.current !== 'idle' || prefersReducedMotion()) {
        action()
        return
      }
      applyPhase('closing')
      closeTimerRef.current = window.setTimeout(() => {
        closeTimerRef.current = null
        // No route change, so no destination page to hold — auto-reveal opens.
        coverThenReveal(action, true)
      }, SCROLL_TRANSITION_MS)
    },
    [applyPhase, coverThenReveal],
  )

  const contextValue: MenuScrollContextValue = {
    phase,
    navigateWithScroll,
    withScrollTransition,
    holdScroll,
    revealScroll,
  }

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
