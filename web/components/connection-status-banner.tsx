"use client";

import Image from "next/image";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import { useAppSession } from "@/components/auth-provider";
import {
  RECONNECTED_EVENT,
  checkApiConnection,
  isBrowserOffline,
  getConnectivitySnapshot,
  getServerConnectivitySnapshot,
  subscribeConnectivity,
} from "@/lib/connectivity";

const BACK_ONLINE_DWELL_MS = 2500;

export function useConnectivity() {
  return useSyncExternalStore(
    subscribeConnectivity,
    getConnectivitySnapshot,
    getServerConnectivitySnapshot,
  );
}

/**
 * Runs `callback` after the API health check confirms recovery.
 * Screens pass `enabled` only while they
 * are sitting on a load error, so a healthy screen never refetches.
 */
export function useOnReconnect(callback: () => void, enabled: boolean) {
  const callbackRef = useRef(callback);
  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);
  useEffect(() => {
    if (!enabled) return;
    const handler = () => callbackRef.current();
    window.addEventListener(RECONNECTED_EVENT, handler);
    return () => window.removeEventListener(RECONNECTED_EVENT, handler);
  }, [enabled]);
}

/**
 * App-wide connection card. Mounted once in the root layout so every surface —
 * guarded or public — gets the same honest signal, and so reconnecting
 * re-hydrates the athlete profile without the user having to find a button.
 */
export function ConnectionStatusBanner() {
  const { offline, degraded } = useConnectivity();
  const { session, hasTransientMeError, refreshMe } = useAppSession();
  const [isRetrying, setIsRetrying] = useState(false);
  const [showBackOnline, setShowBackOnline] = useState(false);
  const refreshRef = useRef(refreshMe);
  const needsProfileRef = useRef(false);

  useEffect(() => {
    refreshRef.current = refreshMe;
    needsProfileRef.current = Boolean(session && hasTransientMeError);
  }, [hasTransientMeError, refreshMe, session]);

  useEffect(() => {
    let dwellTimer: ReturnType<typeof setTimeout> | null = null;
    const onReconnected = () => {
      setShowBackOnline(true);
      if (dwellTimer) clearTimeout(dwellTimer);
      dwellTimer = setTimeout(() => setShowBackOnline(false), BACK_ONLINE_DWELL_MS);
      if (needsProfileRef.current) {
        void refreshRef.current();
      }
    };
    window.addEventListener(RECONNECTED_EVENT, onReconnected);
    return () => {
      if (dwellTimer) clearTimeout(dwellTimer);
      window.removeEventListener(RECONNECTED_EVENT, onReconnected);
    };
  }, []);

  async function handleRetry() {
    if (isRetrying) return;
    setIsRetrying(true);
    try {
      const wasDown = getConnectivitySnapshot().offline || getConnectivitySnapshot().degraded;
      if (await checkApiConnection()) {
        // The recovery event refreshes a failed profile when transitioning up.
        if (!wasDown && needsProfileRef.current) await refreshMe();
      }
    } finally {
      setIsRetrying(false);
    }
  }

  const unreachable = degraded || Boolean(session && hasTransientMeError);

  if (!offline && !unreachable && !showBackOnline) return null;

  const restored = !offline && !unreachable;
  const title = offline ? "You’re offline." : unreachable ? "Can’t reach UNLXCK." : "Back online.";
  const description = offline
    ? "Keep what’s loaded. We’ll reconnect when your connection returns."
    : unreachable
      ? "Your progress is safe. We’re checking the connection."
      : "Connection confirmed. Pick up where you left off.";

  return (
    <div className={`connection-lost-banner${restored ? " connection-restored" : ""}`} role="status" aria-live="polite" aria-atomic="true">
      <div className="connection-card-header">
        <Image src="/brand/unlxck-one-angle-48.png" alt="" unoptimized width={24} height={24} />
        <span className="connection-card-label"><span className="connection-lost-dot" aria-hidden="true" />CONNECTION</span>
      </div>
      <p className="connection-card-title">{title}</p>
      <p className="connection-card-description">{description}</p>
      {!restored ? (
        <button type="button" className="connection-lost-retry" onClick={() => void handleRetry()} disabled={offline || isRetrying || isBrowserOffline()}>
          {isRetrying ? "Checking connection…" : "Retry connection"}<svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M4 12 12 4M4 4h8v8" stroke="currentColor" strokeWidth="1.5" /></svg>
        </button>
      ) : null}
    </div>
  );
}
