/**
 * The one-time "See all your work in History" hint. The first saved session
 * arms it; tapping it or opening History marks it seen for good.
 */
export const HISTORY_HINT_KEY = "unlxck.history-hint";
export const STORED_FLAG_EVENT = "unlxck:stored-flag";

export function readStoredFlag(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    // Blocked storage reads as already seen, so a hint never nags.
    return "blocked";
  }
}

export function writeStoredFlag(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    return;
  }
  window.dispatchEvent(new CustomEvent(STORED_FLAG_EVENT, { detail: key }));
}

/** Called after a session is saved: shows the hint unless it was ever shown. */
export function announceSessionLogged(): void {
  if (typeof window === "undefined") return;
  if (readStoredFlag(HISTORY_HINT_KEY) === null) writeStoredFlag(HISTORY_HINT_KEY, "armed");
}

export function markHistoryHintSeen(): void {
  if (typeof window === "undefined") return;
  if (readStoredFlag(HISTORY_HINT_KEY) !== "seen") writeStoredFlag(HISTORY_HINT_KEY, "seen");
}
