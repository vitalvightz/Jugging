/**
 * Answer a saved note with the cue the AI drew from it.
 *
 * Saving a note (a takeaway, sparring notes, a plan session's notes) starts a
 * background inference on the API. The save itself never waits for it, so the
 * athlete used to hear nothing back. `watchNoteFocusCue` asks the app-wide
 * announcer (components/focus-cue-announcer.tsx) to poll briefly for the result
 * and show it as a toast; nothing is shown when the note gives no cue.
 */
export type NoteFocusSourceKind = "sparring_log" | "manual_takeaway" | "manual_exercise" | "plan_completion";

export type NoteFocusCueWatch = { token: string; kind: NoteFocusSourceKind; sourceId: string };

export const NOTE_FOCUS_CUE_WATCH_EVENT = "unlxck:note-focus-cue-watch";

/** Poll timing: the model call usually finishes in a few seconds. */
export const CUE_POLL_DELAYS_MS = [1_500, 2_000, 2_500, 3_000, 4_000, 5_000, 6_000, 8_000] as const;

export function watchNoteFocusCue(watch: NoteFocusCueWatch): void {
  if (typeof window === "undefined" || !watch.token || !watch.sourceId) return;
  window.dispatchEvent(new CustomEvent<NoteFocusCueWatch>(NOTE_FOCUS_CUE_WATCH_EVENT, { detail: watch }));
}

/** The line shown for a ready cue. */
export function noteFocusCueMessage(focus: string): string {
  return `Next time: ${focus.trim()}`;
}
