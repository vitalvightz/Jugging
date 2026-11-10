/** A tab-local completion receipt, retained until the summary actually renders. */
const KEY = "unlxck:programme-arrival";
const SEEN_PREFIX = "unlxck:programme-arrival-seen:";
export const PROGRAMME_ARRIVAL_EVENT = "unlxck:programme-arrival-change";

export function offerProgrammeArrival(planId: string): void {
  try {
    if (window.localStorage.getItem(SEEN_PREFIX + planId)) return;
    window.sessionStorage.setItem(KEY, JSON.stringify({ planId }));
    window.dispatchEvent(new Event(PROGRAMME_ARRIVAL_EVENT));
  } catch { /* Storage is optional. */ }
}

export function hasProgrammeArrival(planId: string): boolean {
  try {
    const value = JSON.parse(window.sessionStorage.getItem(KEY) ?? "null");
    return value?.planId === planId && !window.localStorage.getItem(SEEN_PREFIX + planId);
  } catch { return false; }
}

export function consumeProgrammeArrival(planId: string): boolean {
  if (!hasProgrammeArrival(planId)) return false;
  try {
    window.localStorage.setItem(SEEN_PREFIX + planId, "1");
    window.sessionStorage.removeItem(KEY);
    return true;
  } catch { return false; }
}
