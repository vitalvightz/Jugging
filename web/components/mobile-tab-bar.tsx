"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { useAppSession } from "@/components/auth-provider";
import { useGenerationStatus } from "@/components/generation-status-provider";
import { HistoryHint } from "@/components/once-hint";
import { BOTTOM_NAV_ITEMS, showsMobileTabBar } from "@/lib/beta-navigation";

const TAB_ICONS: Record<string, ReactNode> = {
  "/": (
    <svg viewBox="0 0 20 20" width="20" height="20" fill="none" aria-hidden="true">
      <path d="M3 10.5L10 4l7 6.5V16a1 1 0 0 1-1 1h-3v-4H8v4H4a1 1 0 0 1-1-1v-5.5z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  ),
  "/today": (
    <svg viewBox="0 0 20 20" width="20" height="20" fill="none" aria-hidden="true">
      <rect x="3" y="4" width="14" height="13" rx="2" stroke="currentColor" strokeWidth="1.5" />
      <path d="M3 8h14M7 2v3M13 2v3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <path d="M7 12l2 2 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  "/plans": (
    <svg viewBox="0 0 20 20" width="20" height="20" fill="none" aria-hidden="true">
      <rect x="4" y="3" width="12" height="14" rx="2" stroke="currentColor" strokeWidth="1.5" />
      <path d="M7 7h6M7 10h6M7 13h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  ),
  "/history": (
    <svg viewBox="0 0 20 20" width="20" height="20" fill="none" aria-hidden="true">
      <path d="M3.5 10a6.5 6.5 0 1 0 1.9-4.6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <path d="M3 3v3h3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M10 6.5V10l2.5 1.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  "/timer": (
    <svg viewBox="0 0 20 20" width="20" height="20" fill="none" aria-hidden="true">
      <circle cx="10" cy="11" r="6" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8 2.5h4M10 2.5V5M10 11l2.5-2.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  ),
};

const TAB_LABEL_KEYS = {
  "/": "overview",
  "/today": "today",
  "/plans": "plan",
  "/history": "history",
  "/timer": "timerTab",
} as const;

/**
 * How far below the layout viewport's bottom edge the visible (visual)
 * viewport ends, as a negative CSS length, or 0.
 *
 * A fixed element is drawn against the layout viewport. iOS can leave the
 * visual viewport scrolled below it (after momentum scroll, the keyboard, or a
 * toolbar change), which floats a `bottom: 0` bar above the screen's edge with
 * content showing underneath. The offset moves it back onto the visible edge.
 * It never lifts the bar (a keyboard shrinking the visual viewport must not
 * push it up), and is ignored while pinch-zoomed.
 *
 * `fixedBottom` is where the bar's bottom edge lands with no offset, measured
 * from the bar itself: in an iOS home-screen app `window.innerHeight` can
 * report the full visual height while the fixed layer is still sized to the
 * keyboard-shrunk viewport, so the window alone reads "no gap".
 */
export function visualViewportBottomOffset(
  fixedBottom: number,
  viewport: { height: number; offsetTop: number; scale: number } | null | undefined,
): number {
  if (!viewport || Math.abs(viewport.scale - 1) > 0.01 || !(fixedBottom > 0)) {
    return 0;
  }
  const gap = Math.round(fixedBottom - (viewport.offsetTop + viewport.height));
  return gap < 0 ? gap : 0;
}

/**
 * Whether the page is mid-bounce past its top or bottom (iOS rubber-banding).
 * The visual viewport swings for a moment then; measuring it then and moving
 * the bar to match made the bar shake with every bounce.
 */
export function isOverscrolled(scrollY: number, innerHeight: number, scrollHeight: number): boolean {
  return scrollY < 0 || scrollY + innerHeight > scrollHeight + 1;
}

/** How long scrolling must be still before the bar's position is checked. */
export const SCROLL_SETTLE_MS = 150;

export function isTabActive(pathname: string, href: string): boolean {
  if (href === "/") {
    return pathname === "/";
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function MobileTabBar() {
  const t = useTranslations("Navigation");
  const pathname = usePathname();
  const { isReady, session } = useAppSession();
  const { isActive: generationActive } = useGenerationStatus();
  const barRef = useRef<HTMLElement>(null);
  // The tapped tab lights at once. The route commits a moment later, and
  // waiting for it left the old tab lit over the new page for a frame or two.
  const [pendingHref, setPendingHref] = useState<string | null>(null);
  const [seenPath, setSeenPath] = useState(pathname);
  if (pathname !== seenPath) {
    setSeenPath(pathname);
    setPendingHref(null);
  }

  const isHidden = !isReady || !showsMobileTabBar(pathname, Boolean(session));

  useEffect(() => {
    const { documentElement } = document;
    if (isHidden) {
      delete documentElement.dataset.mobileTabBar;
      return;
    }
    documentElement.dataset.mobileTabBar = generationActive ? "stacked" : "active";
    return () => {
      delete documentElement.dataset.mobileTabBar;
    };
  }, [isHidden, generationActive]);

  useEffect(() => {
    const viewport = window.visualViewport;
    if (isHidden || !viewport) {
      return;
    }
    const root = document.documentElement;
    let frame = 0;
    let applied = 0;
    const timers = new Set<number>();
    const sync = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        // While the drawer slides the bar off-screen its rect is not where
        // `bottom: 0` lands; keep the current offset until it comes back.
        if (root.dataset.mobileNavOpen === "true") {
          return;
        }
        if (isOverscrolled(window.scrollY, window.innerHeight, root.scrollHeight)) {
          return;
        }
        const bar = barRef.current;
        // Undo the offset already applied to recover where `bottom: 0` lands.
        const fixedBottom = bar ? bar.getBoundingClientRect().bottom + applied : window.innerHeight;
        const offset = visualViewportBottomOffset(fixedBottom, viewport);
        // A pixel either way is rounding, not a floating bar.
        if (Math.abs(offset - applied) <= 1) {
          return;
        }
        applied = offset;
        if (offset === 0) {
          root.style.removeProperty("--visual-viewport-bottom-offset");
        } else {
          root.style.setProperty("--visual-viewport-bottom-offset", `${offset}px`);
        }
      });
    };
    // The keyboard and the app-switcher settle after their events fire, and
    // iOS does not always send a final viewport event; check again once the
    // animation has finished.
    const settle = () => {
      sync();
      for (const delay of [120, 400, 800]) {
        const id = window.setTimeout(() => {
          timers.delete(id);
          sync();
        }, delay);
        timers.add(id);
      }
    };
    // Scrolling moves the visual viewport every frame. Chasing it moved the bar
    // a frame late each time, so it shook; the floating this corrects is what
    // remains once scrolling stops, so check then.
    let scrollTimer: number | null = null;
    const onScroll = () => {
      // A correction already applied is released at once, not left in place
      // for the rest of the scroll.
      if (applied !== 0) sync();
      if (scrollTimer !== null) window.clearTimeout(scrollTimer);
      scrollTimer = window.setTimeout(() => {
        scrollTimer = null;
        sync();
      }, SCROLL_SETTLE_MS);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        settle();
      }
    };
    settle();
    viewport.addEventListener("resize", sync);
    viewport.addEventListener("scroll", onScroll);
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", settle);
    window.addEventListener("orientationchange", settle);
    window.addEventListener("pageshow", settle);
    document.addEventListener("focusout", settle);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelAnimationFrame(frame);
      timers.forEach((id) => window.clearTimeout(id));
      if (scrollTimer !== null) window.clearTimeout(scrollTimer);
      viewport.removeEventListener("resize", sync);
      viewport.removeEventListener("scroll", onScroll);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", settle);
      window.removeEventListener("orientationchange", settle);
      window.removeEventListener("pageshow", settle);
      document.removeEventListener("focusout", settle);
      document.removeEventListener("visibilitychange", onVisibility);
      root.style.removeProperty("--visual-viewport-bottom-offset");
    };
  }, [isHidden]);

  if (isHidden) {
    return null;
  }

  return (
    <nav ref={barRef} className="mobile-tab-bar" aria-label={t("primary")}>
      {BOTTOM_NAV_ITEMS.map((tab) => {
        const active = pendingHref ? pendingHref === tab.href : isTabActive(pathname, tab.href);
        return (
          <Link
            key={tab.href}
            href={tab.href}
            className={`mobile-tab-bar-item${active ? " mobile-tab-bar-item-active" : ""}`}
            aria-current={active ? "page" : undefined}
            onClick={(event) => {
              const plainTap = !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && event.button === 0;
              if (plainTap && !isTabActive(pathname, tab.href)) setPendingHref(tab.href);
            }}
          >
            <span className="mobile-tab-bar-icon" aria-hidden="true">
              {TAB_ICONS[tab.href]}
            </span>
            <span className="mobile-tab-bar-label">
              {t(TAB_LABEL_KEYS[tab.href as keyof typeof TAB_LABEL_KEYS])}
            </span>
          </Link>
        );
      })}
      {/* Anchored over the History tab; a sibling, since a button can't sit in a link. */}
      <span className="mobile-tab-bar-hint-anchor" style={{ left: `${((BOTTOM_NAV_ITEMS.findIndex((tab) => tab.href === "/history") + 0.5) / BOTTOM_NAV_ITEMS.length) * 100}%` }}>
        <HistoryHint className="once-hint once-hint-above" />
      </span>
    </nav>
  );
}
