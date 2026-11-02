"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { Fragment, useCallback, useEffect, useRef, useState, type TransitionEvent } from "react";

import { useAppSession } from "@/components/auth-provider";
import { LanguageSwitcher } from "@/components/language-switcher";
import { Skeleton } from "@/components/skeleton";
import { HistoryHint } from "@/components/once-hint";
import { shouldShowAdminPanelLink } from "@/lib/admin-nav-visibility";
import { getShellSurface, isAuthSurfaceRoute, shouldShowBrandTopbar } from "@/lib/app-surface";
import { isSafeAvatarImageUrl } from "@/lib/avatar-image-url";
import { showsMobileTabBar, SIDE_NAV_ITEMS } from "@/lib/beta-navigation";
import { isNavToggleCondensed } from "@/lib/nav-toggle-scroll";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import { useFadeNavigate } from "@/components/fade-through";

type MobileNavState = "closed" | "opening" | "open" | "closing";

const MOBILE_NAV_CLOSE_MS = 240;
const MOBILE_NAV_MEDIA_QUERY = "(max-width: 960px)";

function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

function getInitials(name: string): string {
  const result = name
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() ?? "")
    .join("");
  return result || "A";
}

function MenuIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" fill="none" aria-hidden="true" focusable="false">
      <path d="M4 6h12M4 10h12M4 14h12" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

type AccountIdentity = {
  displayName: string;
  displayEmail: string;
  initials: string;
  avatarUrl: string | null | undefined;
  role: string | null;
  isSessionResolving: boolean;
};

function AccountAvatar({ identity }: { identity: AccountIdentity }) {
  return identity.avatarUrl ? (
    <img src={identity.avatarUrl} alt="" className="sidebar-avatar-img" />
  ) : (
    <span className="sidebar-avatar-initials">{identity.initials}</span>
  );
}

function AccountIdentityRow({ identity }: { identity: AccountIdentity }) {
  const t = useTranslations("Navigation");
  const { role } = identity;
  return (
    <div className="sidebar-user-identity">
      <div className="sidebar-avatar" aria-hidden="true">
        <AccountAvatar identity={identity} />
      </div>
      <div className="sidebar-user-info">
        <p className="sidebar-user-name">{identity.displayName}</p>
        <p className="sidebar-user-email">{identity.displayEmail}</p>
        {role ? (
          <span
            className={`sidebar-role-badge sidebar-role-${role}`}
            aria-label={t("role", { role: role === "admin" ? t("administrator") : t("athlete") })}
          >
            {role === "admin" ? t("admin") : t("athlete")}
          </span>
        ) : identity.isSessionResolving ? (
          <Skeleton variant="block" width={74} height={24} style={{ borderRadius: 999 }} />
        ) : null}
      </div>
    </div>
  );
}

/**
 * Phone chrome while the bottom bar is showing: + Create top-left and the
 * avatar top-right, which opens Settings (profile, language, sign out, admin).
 */
function PhoneWorkspaceBar({ pathname, identity }: { pathname: string; identity: AccountIdentity }) {
  const t = useTranslations("Navigation");
  return (
    <>
      {/* Solid band behind both buttons so page content scrolls under it, not past them. */}
      <div className="phone-top-band" aria-hidden="true" />
      {pathname === "/create" ? null : (
        <Link href="/create" className="phone-create-button">
          <span aria-hidden="true">+</span>
          {t("create")}
        </Link>
      )}
      <Link href="/settings" className="phone-account-button" aria-label={t("account")}>
        <AccountAvatar identity={identity} />
      </Link>
    </>
  );
}

export function AppNav() {
  const t = useTranslations("Navigation");
  const pathname = usePathname();
  const router = useRouter();
  const fadeNavigate = useFadeNavigate();
  const { isReady, isMeHydrated, session, me, signOut } = useAppSession();
  const [mobileNavState, setMobileNavState] = useState<MobileNavState>("closed");
  const [navToggleCondensed, setNavToggleCondensed] = useState(false);
  const [desktopNavCollapsed, setDesktopNavCollapsed] = useState(false);
  const [pendingNavHref, setPendingNavHref] = useState<string | null>(null);
  const closeTimeoutRef = useRef<number | null>(null);
  const desktopNavToggleRef = useRef<HTMLButtonElement | null>(null);
  const hasSession = Boolean(session);
  const sessionIdentity = session?.user_id ?? session?.email ?? (session ? "signed-in" : null);
  const isSessionResolving = Boolean(session && !isMeHydrated);
  const shellSurface = getShellSurface(pathname, hasSession);
  const showBrandTopbar = isReady && shouldShowBrandTopbar(pathname, hasSession);
  // Public auth routes (login / signup / password reset) render only the brand
  // shell. Suppress every piece of workspace navigation there (the floating
  // Menu control, the sidebar, and its "checking your session" loading card)
  // so these pages never look like a half-loaded workspace. Derived from the
  // pathname alone, so it is correct from the very first render (no flash), and
  // signed-in visitors are redirected away by the auth pages themselves.
  const isAuthRoute = isAuthSurfaceRoute(pathname);

  // On the public brand surface (logged-out landing) the slim brand top bar is
  // the only navigation. Suppress the floating workspace Menu control and the
  // sidebar drawer there so they can't overlap page content on scroll — those
  // belong to the signed-in workspace shell. Auth routes suppress them too.
  const isPublicBrandSurface = shellSurface === "brand" && !session;
  const suppressWorkspaceNav = isAuthRoute || isPublicBrandSurface || shellSurface === "entry";

  const isMobileDrawerVisible = mobileNavState !== "closed";

  const clearCloseTimeout = useCallback(() => {
    if (closeTimeoutRef.current === null) {
      return;
    }
    window.clearTimeout(closeTimeoutRef.current);
    closeTimeoutRef.current = null;
  }, []);

  function openMobileDrawer() {
    clearCloseTimeout();
    setMobileNavState((current) => (current === "open" || current === "opening" ? current : "opening"));
  }

  const closeMobileDrawer = useCallback(() => {
    clearCloseTimeout();
    setMobileNavState((current) => (current === "closed" || current === "closing" ? current : "closing"));
  }, [clearCloseTimeout]);

  function handleSidebarClose() {
    if (window.matchMedia(MOBILE_NAV_MEDIA_QUERY).matches) {
      closeMobileDrawer();
      return;
    }

    setDesktopNavCollapsed(true);
  }

  useEffect(() => {
    if (mobileNavState !== "opening") {
      return;
    }

    const frameId = window.requestAnimationFrame(() => {
      setMobileNavState((current) => (current === "opening" ? "open" : current));
    });

    return () => {
      window.cancelAnimationFrame(frameId);
    };
  }, [clearCloseTimeout, mobileNavState]);

  useEffect(() => {
    if (mobileNavState !== "closing") {
      return;
    }

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    closeTimeoutRef.current = window.setTimeout(() => {
      setMobileNavState((current) => (current === "closing" ? "closed" : current));
      closeTimeoutRef.current = null;
    }, reducedMotion ? 0 : MOBILE_NAV_CLOSE_MS);

    return () => {
      clearCloseTimeout();
    };
  }, [clearCloseTimeout, mobileNavState]);

  useEffect(() => {
    return () => {
      clearCloseTimeout();
    };
  }, [clearCloseTimeout]);

  // The floating Menu pill is position:fixed, so once the page scrolls it sits
  // on top of mid-page content. It condenses to an icon-only square on scroll
  // (via the pure, unit-tested `isNavToggleCondensed`) to shrink that footprint,
  // but is never hidden or disabled — it stays tappable at any scroll position.
  // Skipped while the drawer is open (the button is unmounted then anyway).
  useEffect(() => {
    if (isMobileDrawerVisible) {
      return;
    }
    let frame: number | null = null;
    const update = () => {
      frame = null;
      setNavToggleCondensed(isNavToggleCondensed(window.scrollY));
    };
    const onScroll = () => {
      if (frame === null) {
        frame = window.requestAnimationFrame(update);
      }
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    update(); // sync initial state in case the page loads already scrolled
    return () => {
      window.removeEventListener("scroll", onScroll);
      if (frame !== null) {
        window.cancelAnimationFrame(frame);
      }
    };
  }, [isMobileDrawerVisible]);

  useEffect(() => {
    const { documentElement } = document;

    if (desktopNavCollapsed) {
      documentElement.dataset.desktopNavCollapsed = "true";
      desktopNavToggleRef.current?.focus();
      return () => {
        delete documentElement.dataset.desktopNavCollapsed;
      };
    }

    delete documentElement.dataset.desktopNavCollapsed;
    return () => {
      delete documentElement.dataset.desktopNavCollapsed;
    };
  }, [desktopNavCollapsed]);

  const isMobileNavExpanding = mobileNavState === "opening" || mobileNavState === "open";

  useEffect(() => {
    if (!isMobileNavExpanding) {
      return;
    }
    document.documentElement.dataset.mobileNavOpen = "true";
    return () => {
      delete document.documentElement.dataset.mobileNavOpen;
    };
  }, [isMobileNavExpanding]);

  useEffect(() => {
    if (!isMobileDrawerVisible) {
      return;
    }

    const mediaQuery = window.matchMedia(MOBILE_NAV_MEDIA_QUERY);
    let releaseScroll: (() => void) | undefined;
    const syncScrollLock = () => {
      const shouldLock = mediaQuery.matches && isMobileDrawerVisible;
      if (shouldLock) {
        releaseScroll ??= lockOverlayScroll();
        document.documentElement.dataset.mobileNavLock = "true";
        document.body.dataset.mobileNavLock = "true";
        return;
      }

      releaseScroll?.();
      releaseScroll = undefined;
      delete document.documentElement.dataset.mobileNavLock;
      delete document.body.dataset.mobileNavLock;
    };

    syncScrollLock();
    mediaQuery.addEventListener("change", syncScrollLock);

    return () => {
      mediaQuery.removeEventListener("change", syncScrollLock);
      releaseScroll?.();
      delete document.documentElement.dataset.mobileNavLock;
      delete document.body.dataset.mobileNavLock;
    };
  }, [isMobileDrawerVisible]);

  useEffect(() => {
    if (!isMobileDrawerVisible) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        closeMobileDrawer();
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [closeMobileDrawer, isMobileDrawerVisible]);

  // Close for route/account changes and sign-out. Same-account token refreshes
  // must preserve the menu the athlete just opened. This must NOT depend on
  // isMobileDrawerVisible: including it re-ran the
  // effect the instant the drawer opened and closed it again in the same frame,
  // so the Menu button appeared to do nothing. closeMobileDrawer is a no-op when
  // the drawer is already closed, so no open-state guard is needed here.
  useEffect(() => {
    closeMobileDrawer();
  }, [closeMobileDrawer, pathname, sessionIdentity]);

  useEffect(() => {
    if (pendingNavHref && isActive(pathname, pendingNavHref)) {
      setPendingNavHref(null);
    }
  }, [pathname, pendingNavHref]);

  function handleSidebarLinkSelect(href: string) {
    setPendingNavHref(href);
    closeMobileDrawer();
  }

  function isLinkActive(href: string): boolean {
    if (pendingNavHref === href) {
      return true;
    }
    return isActive(pathname, href);
  }

  useEffect(() => {
    document.documentElement.dataset.appSurface = shellSurface;

    return () => {
      delete document.documentElement.dataset.appSurface;
    };
  }, [shellSurface]);

  async function handleSignOut() {
    closeMobileDrawer();
    await signOut();
    fadeNavigate(() => router.push("/"));
  }

  function handleSidebarTransitionEnd(event: TransitionEvent<HTMLElement>) {
    if (event.target !== event.currentTarget || mobileNavState !== "closing") {
      return;
    }

    clearCloseTimeout();
    setMobileNavState("closed");
  }

  const navCopy: Record<string, { label: string; meta: string }> = {
    "/": { label: t("overview"), meta: t("overviewMeta") },
    "/today": { label: t("today"), meta: t("todayMeta") },
    "/plans": { label: t("plan"), meta: t("planMeta") },
    "/history": { label: t("history"), meta: t("historyMeta") },
    "/timer": { label: t("timer"), meta: t("timerMeta") },
    "/nutrition": { label: t("weight"), meta: t("weightMeta") },
    "/onboarding": { label: t("campSetup"), meta: t("campSetupMeta") },
    "/settings": { label: t("settings"), meta: t("settingsMeta") },
  };
  const signedInLinks = SIDE_NAV_ITEMS.map((item) => ({ ...item, ...navCopy[item.href] }));

  const profile = me?.profile;
  const displayName = profile?.full_name || t("athlete");
  const displayEmail = profile?.email || session?.email || t("sessionActive");
  const initials = getInitials(displayName);
  const avatarUrl = profile && isSafeAvatarImageUrl(profile.avatar_url) ? profile.avatar_url : null;
  const role = profile?.role ?? null;
  const isAdminWorkspace = shouldShowAdminPanelLink(role, isActive(pathname, "/admin"));
  // Phones with the bottom bar get + Create and the account button instead of
  // the sidebar; CSS limits both to phone widths, so tablets keep the drawer.
  const hasPhoneBar = isReady && showsMobileTabBar(pathname, hasSession);
  const identity: AccountIdentity = { displayName, displayEmail, initials, avatarUrl, role, isSessionResolving };

  return (
    <>
      {showBrandTopbar ? (
        <header className="brand-topbar" aria-label={t("entryNavigation")}>
          <Link href="/" className="brand-topbar-mark" aria-label="UNLXCK">
            <img
              className="brand-topbar-logo"
              src="/brand/unlxck-one-angle-48.png"
              alt=""
              width={48}
              height={48}
            />
            <span className="brand-topbar-wordmark" aria-hidden="true">UNLXCK</span>
          </Link>
          <nav className="brand-topbar-actions" aria-label={t("accountAccess")}>
            <LanguageSwitcher placement="header" />
            {/* The page's one primary action is "Get started" in the hero and
                closing CTA; returning athletes get a quiet Log in link here. */}
            <Link href="/login" className="brand-topbar-login">
              {t("login")}
            </Link>
          </nav>
        </header>
      ) : null}
      {suppressWorkspaceNav ? null : (
        <>
      {!isMobileDrawerVisible ? (
        <button
          type="button"
          className="mobile-nav-toggle"
          data-phone-hidden={hasPhoneBar ? "true" : undefined}
          data-condensed={navToggleCondensed ? "true" : undefined}
          aria-label={t("openNavigation")}
          aria-expanded={false}
          aria-controls="app-sidebar"
          onClick={openMobileDrawer}
        >
          <span className="nav-toggle-icon" aria-hidden="true">
            <MenuIcon />
          </span>
          <span className="nav-toggle-label">{t("menu")}</span>
          {!session && isReady ? <span className="badge status-badge-neutral">{t("entry")}</span> : null}
        </button>
      ) : null}
      {desktopNavCollapsed ? (
        <button
          ref={desktopNavToggleRef}
          type="button"
          className="desktop-nav-toggle"
          aria-label={t("openNavigation")}
          aria-expanded={false}
          aria-controls="app-sidebar"
          onClick={() => setDesktopNavCollapsed(false)}
        >
          <span className="nav-toggle-icon" aria-hidden="true">
            <MenuIcon />
          </span>
          <span className="nav-toggle-label">{t("menu")}</span>
        </button>
      ) : null}
      {hasPhoneBar ? (
        <PhoneWorkspaceBar pathname={pathname} identity={identity} />
      ) : null}
      {isMobileDrawerVisible ? (
        <button
          type="button"
          className="nav-scrim"
          data-mobile-nav-state={mobileNavState}
          aria-label={t("closeNavigation")}
          onClick={closeMobileDrawer}
        />
      ) : null}
      <aside
        id="app-sidebar"
        className="app-sidebar"
        data-phone-hidden={hasPhoneBar ? "true" : undefined}
        data-mobile-nav-state={mobileNavState}
        data-mobile-nav-visible={isMobileDrawerVisible}
        onTransitionEnd={handleSidebarTransitionEnd}
      >
        <div className="sidebar-shell">
          <div className="sidebar-brand">
            <div className="sidebar-brand-header">
              <img
                className="sidebar-brand-logo"
                src="/brand/unlxck-one-angle-48.png"
                alt="UNLXCK"
                width={48}
                height={48}
              />
              <button
                type="button"
                className="sidebar-drawer-close"
                aria-label={t("closeMenu")}
                onClick={handleSidebarClose}
              >
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
                </svg>
              </button>
            </div>
            <Link href="/" className="brand" onClick={closeMobileDrawer}>
              {t("fightCamp")}
            </Link>
            <p className="sidebar-tagline">{t("tagline")}</p>
          </div>

          {!isReady ? (
            <div className="sidebar-nav">
              <p className="sidebar-section-label">{t("session")}</p>
              <div className="sidebar-user-card">
                <p className="sidebar-user-name">{t("loadingWorkspace")}</p>
                <p className="sidebar-user-email">{t("checkingSession")}</p>
              </div>
            </div>
          ) : null}

          {isReady && !session ? (
            <>
              <div className="sidebar-auth">
                <p className="sidebar-section-label">{t("access")}</p>
                <Link
                  href="/signup"
                  className={isLinkActive("/signup") ? "sidebar-link sidebar-link-active" : "sidebar-link"}
                  onClick={() => handleSidebarLinkSelect("/signup")}
                >
                  <div className="sidebar-link-copy">
                    <span className="sidebar-link-title">{t("createAccount")}</span>
                    <span className="sidebar-link-meta">{t("startAthleteSetup")}</span>
                  </div>
                </Link>
                <Link
                  href="/login"
                  className={isLinkActive("/login") ? "sidebar-link sidebar-link-active" : "sidebar-link"}
                  onClick={() => handleSidebarLinkSelect("/login")}
                >
                  <div className="sidebar-link-copy">
                    <span className="sidebar-link-title">{t("login")}</span>
                    <span className="sidebar-link-meta">{t("resumeCamp")}</span>
                  </div>
                </Link>
              </div>
              <div className="sidebar-user-card">
                <p className="sidebar-user-name">{t("eliteEntry")}</p>
                <p className="sidebar-user-email">{t("entryDescription")}</p>
              </div>
            </>
          ) : null}

          {isReady && session ? (
            <>
              <nav className="sidebar-nav">
                <p className="sidebar-section-label">{t("workspace")}</p>
                {signedInLinks.map((link) => (
                  <Fragment key={link.href}>
                    <Link
                      className={isLinkActive(link.href) ? "sidebar-link sidebar-link-active" : "sidebar-link"}
                      href={link.href}
                      onClick={() => handleSidebarLinkSelect(link.href)}
                    >
                      <div className="sidebar-link-copy">
                        <span className="sidebar-link-title">{link.label}</span>
                        <span className="sidebar-link-meta">{link.meta}</span>
                      </div>
                    </Link>
                    {link.href === "/history" ? <HistoryHint className="once-hint once-hint-below" /> : null}
                  </Fragment>
                ))}
                {isAdminWorkspace ? (
                  <>
                    <div className="sidebar-admin-divider" aria-hidden="true" />
                    <p className="sidebar-section-label">{t("control")}</p>
                    <Link
                      className={isLinkActive("/admin") ? "sidebar-link sidebar-link-active" : "sidebar-link"}
                      href="/admin"
                      onClick={() => handleSidebarLinkSelect("/admin")}
                    >
                      <div className="sidebar-link-copy">
                        <span className="sidebar-link-title">{t("adminPanel")}</span>
                        <span className="sidebar-link-meta">{t("adminMeta")}</span>
                      </div>
                    </Link>
                  </>
                ) : null}
              </nav>
              <div className="sidebar-footer">
                <div className="sidebar-user-card">
                  <AccountIdentityRow identity={identity} />
                  <div className="sidebar-user-actions">
                    <button type="button" className="ghost-button" onClick={handleSignOut}>
                      {t("signOut")}
                    </button>
                    <LanguageSwitcher placement="inline" />
                  </div>
                </div>
              </div>
            </>
          ) : null}
        </div>
      </aside>
        </>
      )}
    </>
  );
}
