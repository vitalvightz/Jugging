/** A short-lived handoff for this tab only; copied URLs cannot offer a camp. */
const KEY = "unlxck:programme-arrival";
const MAX_AGE = 5 * 60 * 1000;

export function offerProgrammeArrival(planId: string): void {
  try { window.sessionStorage.setItem(KEY, JSON.stringify({ planId, at: Date.now(), document: performance.timeOrigin })); } catch { /* Storage is optional. */ }
}

export function hasProgrammeArrival(planId: string): boolean {
  try {
    // A refresh must remain an ordinary Today visit, even before presentation loaded.
    const value = JSON.parse(window.sessionStorage.getItem(KEY) ?? "null");
    return value?.planId === planId && value.document === performance.timeOrigin && Number.isFinite(value.at)
      && Date.now() >= value.at && Date.now() - value.at < MAX_AGE;
  } catch { return false; }
}

export function consumeProgrammeArrival(planId: string): boolean {
  if (!hasProgrammeArrival(planId)) return false;
  try { window.sessionStorage.removeItem(KEY); return true; } catch { return false; }
}
