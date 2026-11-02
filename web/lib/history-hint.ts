/**
 * The one-time "See all your work in History" hint, per athlete. A saved
 * session marks it pending for this tab; the shown hint then arms the
 * signed-in athlete's own flag. Tapping it or opening History marks it seen.
 */
export const STORED_FLAG_EVENT = "unlxck:stored-flag";
const PENDING_KEY = "unlxck.history-hint-pending";

export function historyHintKey(athleteId: string): string {
  return `unlxck.history-hint:${athleteId}`;
}

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

/** Called after a session is saved; whoever is signed in sees the hint once. */
export function announceSessionLogged(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(PENDING_KEY, "1");
  } catch {
    return;
  }
  window.dispatchEvent(new CustomEvent(STORED_FLAG_EVENT, { detail: PENDING_KEY }));
}

/** Arms this athlete's hint if a session was saved and it was never shown. */
export function armHistoryHint(athleteId: string): void {
  let pending = false;
  try {
    pending = window.sessionStorage.getItem(PENDING_KEY) !== null;
    if (pending) window.sessionStorage.removeItem(PENDING_KEY);
  } catch {
    return;
  }
  if (pending && readStoredFlag(historyHintKey(athleteId)) === null) writeStoredFlag(historyHintKey(athleteId), "armed");
}

export function markHistoryHintSeen(athleteId: string): void {
  if (typeof window === "undefined" || !athleteId) return;
  if (readStoredFlag(historyHintKey(athleteId)) !== "seen") writeStoredFlag(historyHintKey(athleteId), "seen");
}
