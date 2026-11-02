"use client";

import { useId, useRef, useState } from "react";

import { ApiError, editPlanCompletion } from "@/lib/api";
import { hasHealthDataConsent } from "@/lib/compliance";
import type { TrainingHistorySession } from "@/lib/training-history";
import { useSheetExit } from "@/lib/use-sheet-exit";
import { useAppSession } from "./auth-provider";
import sheet from "./log-training.module.css";
import styles from "./plan-session-editor.module.css";
import { useModalSheet } from "./session-takeaway";
import { useToast } from "./toast-provider";

const RPE = Array.from({ length: 10 }, (_, index) => index + 1);
const PAIN = Array.from({ length: 11 }, (_, index) => index);
const NOTES_MAX = 2000;

/** History's limited edit for a finished plan session: RPE, pain after and notes. */
export function PlanSessionEditor({ token, row, onSaved }: {
  token: string; row: TrainingHistorySession; onSaved: (row: TrainingHistorySession) => void;
}) {
  const [open, setOpen] = useState(false);
  return <>
    <button className="history-session-delete" type="button" aria-haspopup="dialog" aria-label={`Edit ${row.title}`}
      onClick={() => setOpen(true)}>Edit</button>
    {open ? <PlanSessionSheet token={token} row={row} onClose={() => setOpen(false)}
      onSaved={updated => { setOpen(false); onSaved(updated); }} /> : null}
  </>;
}

function PlanSessionSheet({ token, row, onClose, onSaved }: {
  token: string; row: TrainingHistorySession; onClose: () => void; onSaved: (row: TrainingHistorySession) => void;
}) {
  const fieldId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const { me } = useAppSession();
  const { showToast } = useToast();
  // Pain after is health data: only asked with consent; otherwise kept as stored.
  const askPain = hasHealthDataConsent(me);
  const [rpe, setRpe] = useState<number | null>(row.session_rpe ?? null);
  const [pain, setPain] = useState<number | null>(row.pain_after ?? null);
  const [notes, setNotes] = useState(row.notes ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const saving = useRef(false);
  useModalSheet(dialog);
  const { closing, close } = useSheetExit(onClose);

  async function save() {
    if (saving.current) return;
    saving.current = true; setBusy(true); setError(null);
    try {
      const updated = await editPlanCompletion(token, row.id, { session_rpe: rpe, pain_after: askPain ? pain : row.pain_after ?? null, notes: notes.trim() });
      showToast("Session updated.", { tone: "success" });
      onSaved({ ...row, ...updated });
    } catch (reason) {
      setError(reason instanceof ApiError && reason.status === 401 ? "Your session expired. Sign in again to save."
        : reason instanceof ApiError && reason.status === 409 ? "This session can't be changed from History."
          : "Your changes weren't saved. Try again.");
    } finally { saving.current = false; setBusy(false); }
  }

  return (
    <dialog ref={dialog} className={sheet.dialog} aria-labelledby={`${fieldId}-title`} data-closing={closing || undefined}
      onCancel={event => { event.preventDefault(); if (!busy) close(); }}>
      <header className={sheet.header}>
        <div>
          <h2 id={`${fieldId}-title`}>Edit session</h2>
          <p className={sheet.source}>{row.title}</p>
        </div>
        <button className={sheet.close} type="button" aria-label="Close edit session" disabled={busy} onClick={() => close()}>×</button>
      </header>
      <form className={sheet.form} onSubmit={event => { event.preventDefault(); void save(); }}>
        <div className={sheet.body}>
          <p className={styles.limit}>The plan, exercises and status stay as logged.</p>
          <fieldset className={sheet.fieldset} disabled={busy} aria-label="RPE">
            <legend>How hard was it? <span className={sheet.unit}>RPE 1–10</span></legend>
            <div className={styles.scale}>
              {RPE.map(value => <button key={value} type="button" aria-pressed={rpe === value}
                onClick={() => setRpe(current => current === value ? null : value)}>{value}</button>)}
            </div>
          </fieldset>
          {askPain ? <fieldset className={sheet.fieldset} disabled={busy} aria-label="Pain after">
            <legend>Pain after <span className={sheet.unit}>0–10</span></legend>
            <div className={styles.scale}>
              {PAIN.map(value => <button key={value} type="button" aria-pressed={pain === value}
                onClick={() => setPain(current => current === value ? null : value)}>{value}</button>)}
            </div>
          </fieldset> : null}
          <label className={styles.notesLabel} htmlFor={`${fieldId}-notes`}>Notes</label>
          <textarea id={`${fieldId}-notes`} className={styles.notes} rows={3} maxLength={NOTES_MAX} value={notes}
            disabled={busy} onChange={event => setNotes(event.target.value)} />
        </div>
        <footer className={sheet.actions}>
          {error ? <p className={sheet.error} role="alert">{error}</p> : null}
          <button className="cta" type="submit" disabled={busy}>{busy ? "Saving…" : "Save changes"}</button>
        </footer>
      </form>
    </dialog>
  );
}
