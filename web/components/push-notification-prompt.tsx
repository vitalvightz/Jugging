"use client";

import { useEffect, useRef, useState } from "react";
import Image from "next/image";
import { useAppSession } from "@/components/auth-provider";
import { getPushSettings } from "@/lib/api";
import { isPushSupported, subscribeToPushNotifications } from "@/lib/push";
import { installPromptClaimed, PUSH_PROMPT_EVENT } from "@/lib/push-prompt";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import { isInstagramBrowser } from "@/lib/pwa";
import styles from "./push-notification-prompt.module.css";
import { useSheetExit } from "@/lib/use-sheet-exit";

type Setup = { public_key: string; registration: ServiceWorkerRegistration };

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
  const storageKey = `unlxck:push-prompt:v1:${athleteId}`;

  useEffect(() => {
    let cancelled = false;
    offered.current = false;
    let checking = false;
    let timer: ReturnType<typeof setInterval> | undefined;
    try { offered.current = window.localStorage.getItem(storageKey) === "seen"; } catch { /* Device storage is optional. */ }

    async function onSaved(event: Event) {
      if ((event as CustomEvent<{ token: string }>).detail?.token !== token || offered.current || checking || timer) return;
      if (isInstagramBrowser(navigator.userAgent) || !isPushSupported() || Notification.permission !== "default") return;
      checking = true;
      try {
        const [settings, registration] = await Promise.all([
          getPushSettings(token), navigator.serviceWorker.getRegistration(),
        ]);
        if (cancelled || !settings.enabled || !settings.public_key || !registration?.active) return;
        if ((settings as typeof settings & { preferences?: { push_enabled?: boolean } }).preferences?.push_enabled === false) return;
        const subscription = await registration.pushManager.getSubscription();
        if (cancelled || offered.current || subscription) return;
        // Let completion review, sparring safety reports and other modals finish.
        timer = setInterval(() => {
          if (offered.current || installPromptClaimed()) { clearInterval(timer); timer = undefined; return; }
          if (document.visibilityState !== "visible" || document.querySelector('dialog[open], [aria-modal="true"]')) return;
          clearInterval(timer);
          timer = undefined;
          if (cancelled || Notification.permission !== "default") return;
          setSetup({ public_key: settings.public_key!, registration });
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
    }
    return () => {
      node.close();
      release();
      if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
    };
  }, [setup, storageKey]);

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
        {busy ? "Enabling…" : "Enable notifications"}<svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M4 12 12 4M4 4h8v8" stroke="currentColor" strokeWidth="1.5" /></svg>
      </button>
      <button type="button" className={styles.later} disabled={busy} onClick={() => dismiss()}>Not now</button>
      <p className={styles.footnote}>You control what comes through in Settings.</p>
    </dialog>
  );
}
