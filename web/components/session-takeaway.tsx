"use client";

import { type FormEvent, type RefObject, useEffect, useId, useRef, useState } from "react";

import { ApiError, saveTrainingTakeaway } from "@/lib/api";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import { TAKEAWAY_MAX_CHARS, type TrainingHistorySession } from "@/lib/training-history";
import sheet from "./log-training.module.css";
import styles from "./session-takeaway.module.css";
import { useToast } from "./toast-provider";
import { useSheetExit } from "@/lib/use-sheet-exit";

const PLACEHOLDER = "e.g. Keep my right hand up after the jab";
const label = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);

export function sessionMeta(row: TrainingHistorySession): string {
  return [row.title, row.duration_minutes ? `${row.duration_minutes} minutes` : null, row.effort ? label(row.effort) : null]
    .filter(Boolean).join(" · ");
}

function saveError(reason: unknown): string {
  if (reason instanceof ApiError && reason.status === 401) return "Your session expired. Sign in again to save.";
  return "Your takeaway wasn't saved. Try again.";
}

/** Shared save state: one request at a time, trimmed text, a toast on success. */
function useTakeawaySave(token: string, row: TrainingHistorySession, onSaved: (row: TrainingHistorySession) => void) {
  const { showToast } = useToast();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const saving = useRef(false);
  async function save(text: string) {
    if (saving.current) return;
    saving.current = true; setBusy(true); setError(null);
    try {
      const updated = await saveTrainingTakeaway(token, row.id, text.trim());
      showToast(updated.takeaway ? "Takeaway saved." : "Takeaway removed.", { tone: "success" });
      onSaved(updated);
    } catch (reason) {
      setError(saveError(reason));
    } finally { saving.current = false; setBusy(false); }
  }
  return { busy, error, save };
}

/** Opens `dialog` as a modal sheet while mounted and enabled; disabling hands the top layer back. */
export function useModalSheet(dialog: RefObject<HTMLDialogElement | null>, enabled = true) {
  useEffect(() => {
    const element = dialog.current;
    if (!enabled || !element) return;
    const previous = document.activeElement as HTMLElement | null;
    const release = lockOverlayScroll();
    if (!element.open) element.showModal?.();
    element.querySelector<HTMLElement>("textarea, input")?.focus();
    return () => { if (element.open) element.close?.(); release(); previous?.focus(); };
  }, [dialog, enabled]);
}

/**
 * The optional "Anything worth remembering?" step right after a session is
 * saved. Inline under a full-width log action; as a bottom sheet (`sheet`)
 * where the action is a half-width tile with no room beneath it.
 */
export function SessionTakeawayPrompt({ token, row, onSaved, onDismiss, sheet: asSheet = false, onAddExercises, exerciseCount = 0, suspended = false }: {
  token: string; row: TrainingHistorySession; onSaved: (row: TrainingHistorySession) => void; onDismiss: () => void;
  sheet?: boolean;
  /** Offers the optional exercise logger; the takeaway stays where it was. */
  onAddExercises?: () => void;
  exerciseCount?: number;
  /** Another sheet is open on top; the takeaway sheet steps out of the top layer. */
  suspended?: boolean;
}) {
  const fieldId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const [text, setText] = useState("");
  const { busy, error, save } = useTakeawaySave(token, row, onSaved);
  useModalSheet(dialog, asSheet && !suspended);
  // As a sheet it slides away first; inline, its Collapse eases it out.
  const { closing, close: exitSheet } = useSheetExit(onDismiss);
  const dismiss = asSheet ? exitSheet : onDismiss;
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!text.trim()) { dismiss(); return; }
    void save(text);
  }
  const card = (
    <form className={asSheet ? `${styles.card} ${styles.inSheet}` : styles.card} onSubmit={submit} aria-label="Session saved">
      <div className={styles.saved} role="status">
        <div>
          <p className={styles.savedTitle}>Session saved</p>
          <span className="sr-only">{row.title} logged successfully. Saved to Training History.</span>
          <p className={styles.meta}>{sessionMeta(row)}</p>
        </div>
        <svg className={styles.check} viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="9" /><path d="m8 12.5 2.6 2.5L16 9.5" /></svg>
      </div>
      {onAddExercises ? <button type="button" className={styles.exercises} disabled={busy} aria-haspopup="dialog" onClick={onAddExercises}
        aria-describedby={`${fieldId}-exercises`}>
        <span><strong>{exerciseCount ? `${exerciseCount} ${exerciseCount === 1 ? "exercise" : "exercises"} added` : "Add exercises"}</strong>
          <span id={`${fieldId}-exercises`}>{exerciseCount ? "Edit or add more" : "Optional · sets, reps, load"}</span></span>
        <span aria-hidden="true">{exerciseCount ? "›" : "+"}</span>
      </button> : null}
      <label className={styles.question} htmlFor={`${fieldId}-takeaway`}>Anything worth remembering?</label>
      <input id={`${fieldId}-takeaway`} className={styles.input} type="text" value={text} maxLength={TAKEAWAY_MAX_CHARS}
        placeholder={PLACEHOLDER} disabled={busy} enterKeyHint="done" autoComplete="off" onChange={event => setText(event.target.value)} />
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
      <div className={styles.footer}>
        <button className={styles.skip} type="button" disabled={busy} onClick={() => dismiss()}>Optional — skip anytime</button>
        <button className={styles.add} type="submit" disabled={busy || !text.trim()}>
          {busy ? "Saving…" : <>Add takeaway <span aria-hidden="true">→</span></>}
        </button>
      </div>
    </form>
  );
  if (!asSheet) return card;
  return (
    <dialog ref={dialog} className={sheet.dialog} aria-label="Session saved" data-closing={closing || undefined}
      onCancel={event => { event.preventDefault(); if (!busy) dismiss(); }}>
      {card}
    </dialog>
  );
}

/** Add or edit a saved manual session's takeaway from History. */
export function TakeawayEditor({ token, row, onSaved }: {
  token: string; row: TrainingHistorySession; onSaved: (row: TrainingHistorySession) => void;
}) {
  const [open, setOpen] = useState(false);
  const has = Boolean(row.takeaway?.trim());
  return <>
    <button className="history-session-delete" type="button" aria-haspopup="dialog"
      aria-label={`${has ? "Edit" : "Add"} takeaway for ${row.title}`} onClick={() => setOpen(true)}>
      {has ? "Edit takeaway" : "Add takeaway"}
    </button>
    {open ? <TakeawaySheet token={token} row={row} onClose={() => setOpen(false)}
      onSaved={updated => { setOpen(false); onSaved(updated); }} /> : null}
  </>;
}

function TakeawaySheet({ token, row, onClose, onSaved }: {
  token: string; row: TrainingHistorySession; onClose: () => void; onSaved: (row: TrainingHistorySession) => void;
}) {
  const fieldId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const [text, setText] = useState(row.takeaway ?? "");
  const { busy, error, save } = useTakeawaySave(token, row, onSaved);
  useModalSheet(dialog);
  const { closing, close } = useSheetExit(onClose);
  return (
    <dialog ref={dialog} className={sheet.dialog} aria-labelledby={`${fieldId}-title`} data-closing={closing || undefined}
      onCancel={event => { event.preventDefault(); if (!busy) close(); }}>
      <header className={sheet.header}>
        <div>
          <h2 id={`${fieldId}-title`}>Takeaway</h2>
          <p className={sheet.source}>{sessionMeta(row)}</p>
        </div>
        <button className={sheet.close} type="button" aria-label="Close takeaway" disabled={busy} onClick={() => close()}>×</button>
      </header>
      <form className={sheet.form} onSubmit={event => { event.preventDefault(); void save(text); }}>
        <div className={sheet.body}>
          <label className={styles.question} htmlFor={`${fieldId}-text`}>Anything worth remembering?</label>
          <textarea id={`${fieldId}-text`} className={styles.input} rows={3} value={text} maxLength={TAKEAWAY_MAX_CHARS}
            placeholder={PLACEHOLDER} disabled={busy} onChange={event => setText(event.target.value)} />
          <p className={styles.count} aria-live="polite">{text.length}/{TAKEAWAY_MAX_CHARS}</p>
        </div>
        <footer className={sheet.actions}>
          {error ? <p className={sheet.error} role="alert">{error}</p> : null}
          <button className="cta" type="submit" disabled={busy || text.trim() === (row.takeaway ?? "").trim()}>
            {busy ? "Saving…" : text.trim() || !row.takeaway ? "Save takeaway" : "Remove takeaway"}
          </button>
        </footer>
      </form>
    </dialog>
  );
}
