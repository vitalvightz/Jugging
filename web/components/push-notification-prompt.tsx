"use client";

import { useEffect, useRef, useState } from "react";
import Image from "next/image";
import { useAppSession } from "@/components/auth-provider";
import { trackProductEvent } from "@/lib/api";
import { canAskForPush, preparePushOffer, subscribeToPushNotifications, type PreparedPush } from "@/lib/push";
import { installPromptClaimed, offerInstallPrompt, PUSH_PROMPT_EVENT, pushPromptSeenKey } from "@/lib/push-prompt";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import styles from "./push-notification-prompt.module.css";
import { useSheetExit } from "@/lib/use-sheet-exit";

type Setup = PreparedPush;

export function PushNotificationPrompt() {
  const { session, me } = useAppSession();
  return session?.access_token && me?.profile.role === "athlete"
    ? <DevicePushPrompt key={`${me.profile.athlete_id}:${session.access_token}`} token={session.access_token} athleteId={me.profile.athlete_id} />
    : null;
}

export function DevicePushPrompt({ token, athleteId }: { token: string; athleteId: string }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [setup, setSetup] = useState<Setup | null>(null);
  const [busy, setBusy] = useState(false);
  const working = useRef(false);
  const offered = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const storageKey = pushPromptSeenKey(athleteId);

  useEffect(() => {
    let cancelled = false;
    offered.current = false;
    let checking = false;
    let timer: ReturnType<typeof setInterval> | undefined;
    try { offered.current = window.localStorage.getItem(storageKey) === "seen"; } catch { /* Device storage is optional. */ }

    async function onSaved(event: Event) {
      if ((event as CustomEvent<{ token: string }>).detail?.token !== token || offered.current || !canAskForPush()) return;
      // This device can be asked now, so notifications take the moment ahead
      // of install guidance. An offer already on its way keeps it.
      event.preventDefault();
      if (checking || timer) return;
      checking = true;
      try {
        const prepared = await preparePushOffer(token);
        if (cancelled || offered.current) return;
        // Nothing to offer after all (paused, already subscribed): hand the moment on.
        if (!prepared) { offerInstallPrompt(); return; }
        // Let completion review, sparring safety reports and other modals finish.
        timer = setInterval(() => {
          if (offered.current || installPromptClaimed()) { clearInterval(timer); timer = undefined; return; }
          if (document.visibilityState !== "visible" || document.querySelector('dialog[open], [aria-modal="true"]')) return;
          clearInterval(timer);
          timer = undefined;
          if (cancelled || Notification.permission === "denied") return;
          setSetup(prepared);
        }, 500);
      } catch { /* An optional prompt must never interrupt a saved workout. */ }
      finally { checking = false; }
    }
    window.addEventListener(PUSH_PROMPT_EVENT, onSaved);
    return () => {
      cancelled = true;
      clearInterval(timer);
      window.removeEventListener(PUSH_PROMPT_EVENT, onSaved);
    };
  }, [token, storageKey]);

  useEffect(() => {
    if (!setup || !dialog.current) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    const node = dialog.current;
    const release = lockOverlayScroll();
    try {
      node.showModal();
    } catch {
      // Navigation or another interruption must not consume this opportunity.
      release();
      return;
    }
    if (node.open) {
      offered.current = true;
      try { window.localStorage.setItem(storageKey, "seen"); } catch { /* Keep the in-memory guard. */ }
      void trackProductEvent(token, "push_prompt_shown", "onboarding");
    }
    return () => {
      node.close();
      release();
      if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
    };
  }, [setup, storageKey, token]);

  async function enable() {
    if (!setup || working.current) return;
    working.current = true;
    setBusy(true);
    setError(null);
    try {
      // Prepared before the click so the native request keeps user activation.
      await subscribeToPushNotifications(token, setup);
      setSetup(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Couldn't enable notifications. Try again in Settings.");
    } finally { working.current = false; setBusy(false); }
  }

  // Slides away before it is removed.
  const { closing, close: dismiss } = useSheetExit(() => setSetup(null));
  if (!setup) return null;
  return (
    <dialog ref={dialog} className={styles.dialog} aria-labelledby="push-prompt-title" aria-describedby="push-prompt-description" aria-busy={busy} data-closing={closing || undefined}
      onCancel={(event) => { event.preventDefault(); if (!working.current) dismiss(); }}>
      <div className={styles.header}>
        <Image src="/brand/unlxck-one-angle-48.png" alt="" width={28} height={28} className={styles.mark} />
        <p className={styles.eyebrow}>STAY READY</p>
      </div>
      <h2 id="push-prompt-title"><span className={styles.brand}>UNLXCK</span>{" "}<span className={styles.headline}>your next move.</span></h2>
      <p id="push-prompt-description" className={styles.description}>Training reminders, check-in nudges and camp updates.</p>
      {error ? <p role="alert" className={styles.error}>{error}</p> : null}
      <button type="button" className={styles.enable} onClick={() => void enable()} disabled={busy}>
        {busy ? "Enabling…" : "Enable reminders"}<svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M4 12 12 4M4 4h8v8" stroke="currentColor" strokeWidth="1.5" /></svg>
      </button>
      <button type="button" className={styles.later} disabled={busy} onClick={() => dismiss()}>Not now</button>
      <p className={styles.footnote}>Not now? Turn them on any time from Today or Settings.</p>
    </dialog>
  );
}
