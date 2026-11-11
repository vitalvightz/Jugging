"use client";

import { useEffect, useRef, useState } from "react";

import { useToast } from "@/components/toast-provider";
import { trackProductEvent } from "@/lib/api";
import { preparePushOffer, subscribeToPushNotifications, type PreparedPush } from "@/lib/push";
import { pushPromptSeenKey } from "@/lib/push-prompt";

const SNOOZED_UNTIL_KEY = "unlxck:enable-reminders-row:snoozed-until";
const SNOOZE_MS = 14 * 24 * 60 * 60 * 1000;

/**
 * The way back for an athlete who passed on the one automatic notification
 * offer: a quiet Today row with a single Enable reminders button, so turning
 * reminders on later never needs a trip through Settings. Shown only while
 * this device can still be asked and has no subscription; hiding it snoozes
 * it for two weeks. Never a modal.
 */
export function EnableRemindersRow({ token, athleteId }: { token: string; athleteId: string }) {
  const { showToast } = useToast();
  const [setup, setSetup] = useState<PreparedPush | null>(null);
  const [busy, setBusy] = useState(false);
  const working = useRef(false);

  useEffect(() => {
    let cancelled = false;
    try {
      if (window.localStorage.getItem(pushPromptSeenKey(athleteId)) !== "seen") return;
      if (Date.now() < Number(window.localStorage.getItem(SNOOZED_UNTIL_KEY) ?? 0)) return;
    } catch { return; /* Without device storage the automatic offer repeats instead. */ }
    void preparePushOffer(token).then((prepared) => {
      if (cancelled || !prepared) return;
      setSetup(prepared);
      // Capped server-side at one per athlete per day.
      void trackProductEvent(token, "push_prompt_shown", "onboarding");
    }, () => { /* Optional: a failed read shows nothing rather than a broken row. */ });
    return () => { cancelled = true; };
  }, [token, athleteId]);

  if (!setup) return null;

  async function enable() {
    if (!setup || working.current) return;
    working.current = true;
    setBusy(true);
    try {
      // Prepared before the tap so the native request keeps user activation.
      await subscribeToPushNotifications(token, setup);
      showToast("Reminders on for this device.", { tone: "success" });
      setSetup(null);
    } catch (caught) {
      showToast(caught instanceof Error ? caught.message : "Couldn't enable reminders.", { tone: "error" });
      // A blocked permission cannot be asked again from here.
      if (Notification.permission !== "default") setSetup(null);
    } finally {
      working.current = false;
      setBusy(false);
    }
  }

  function snooze() {
    try { window.localStorage.setItem(SNOOZED_UNTIL_KEY, String(Date.now() + SNOOZE_MS)); } catch { /* Hidden for this visit. */ }
    setSetup(null);
  }

  return (
    <div className="today-training-time today-enable-reminders" role="group" aria-label="Reminders">
      <svg className="today-tool-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
        strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M6 16h12M8 16a4 4 0 118 0M12 8V6M10 19h4" />
      </svg>
      <span className="today-training-time-label">Reminders are off</span>
      <button type="button" className="action-utility today-training-time-save" disabled={busy}
        onClick={() => void enable()}>
        {busy ? "Enabling…" : "Enable reminders"}
      </button>
      <button type="button" className="today-training-time-dismiss" aria-label="Hide reminders offer"
        disabled={busy} onClick={snooze}>×</button>
    </div>
  );
}
