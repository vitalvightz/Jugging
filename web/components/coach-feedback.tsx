"use client";
import { DiscoveryHint } from "@/components/once-hint";
import { useEffect, useState } from "react";
import { notifyFighterOfFeedback } from "@/lib/api";
import { coachRpc } from "@/lib/coach";
import { formatAppDate } from "@/lib/date-format";
import styles from "./coach-feedback.module.css";

type Note = { id: string; coach_name: string; note: string; exercise_ref: string; updated_at: string };

// One read per record, shared by every feedback box on it (the whole session
// and each exercise): a session with eight exercises made nine identical-shape
// requests. It is shared only while those boxes mount together (kept briefly
// after it lands), never as a long-lived cache; a save or a retry drops it.
const SHARE_MS = 1500;
const notesByRecord = new Map<string, Promise<Note[]>>();
function recordNotes(token: string, athleteId: string, source: string, targetId: string): Promise<Note[]> {
  const key = `${token}:${athleteId}:${source}:${targetId}`;
  let pending = notesByRecord.get(key);
  if (!pending) {
    const params = new URLSearchParams({ select: "id,coach_name,note,exercise_ref,updated_at", athlete_id: `eq.${athleteId}`,
      source: `eq.${source}`, target_id: `eq.${targetId}`, order: "created_at.asc" });
    pending = fetch(`${process.env.NEXT_PUBLIC_SUPABASE_URL}/rest/v1/coach_feedback?${params}`, {
      cache: "no-store", headers: { apikey: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "", Authorization: `Bearer ${token}` },
    }).then(async response => { if (!response.ok) throw Error(); return response.json(); })
      .then(rows => { if (!Array.isArray(rows)) throw Error(); return rows as Note[]; });
    const settled = pending;
    const drop = () => { if (notesByRecord.get(key) === settled) notesByRecord.delete(key); };
    settled.then(() => setTimeout(drop, SHARE_MS), drop);
    notesByRecord.set(key, settled);
  }
  return pending;
}
function forgetRecordNotes(token: string, athleteId: string, source: string, targetId: string) {
  notesByRecord.delete(`${token}:${athleteId}:${source}:${targetId}`);
}
export function CoachFeedback({ token, athleteId, source, targetId, exerciseRef = "", onSaved, editable = false }: {
  onSaved?: () => void; token: string; athleteId: string; source: string; targetId: string; exerciseRef?: string; editable?: boolean;
}) {
  const [notes, setNotes] = useState<Note[]>([]);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setNotes([]);
    recordNotes(token, athleteId, source, targetId)
      .then(rows => { if (active) { setNotes(rows.filter(row => (row.exercise_ref ?? "") === exerciseRef)); setError(false); } })
      .catch(() => { if (active) { setNotes([]); setError(true); } });
    return () => { active = false; };
  }, [token, athleteId, source, targetId, exerciseRef, attempt]);
  async function save() {
    setBusy(true);
    try {
      await coachRpc(token, "save_coach_feedback", { p_athlete_id: athleteId, p_source: source, p_target_id: targetId, p_exercise_ref: exerciseRef, p_note: draft.trim() });
      onSaved?.();
      forgetRecordNotes(token, athleteId, source, targetId);
      setEditing(false); setAttempt(value => value + 1);
      // Best-effort: the note is saved whether or not the fighter is pinged.
      notifyFighterOfFeedback(token, athleteId, { source, target_id: targetId, exercise_ref: exerciseRef }).catch(() => undefined);
    } catch { setError(true); }
    finally { setBusy(false); }
  }
  // data-open: notes or the form take the full width; a lone button sits in its row.
  return <div className={styles.feedback} data-open={notes.length || editing || error ? true : undefined}>
    {editable && source === "sparring" && !exerciseRef && !notes.length && !editing ? <DiscoveryHint id="coach-feedback-note" text="Leave a note on this round." /> : null}
    {notes.map(note => <div key={note.id}><small>Coach {note.coach_name} · {formatAppDate(note.updated_at.slice(0, 10))}</small><p>{note.note}</p></div>)}
    {editable && !editing ? <button type="button" className="ghost-button" onClick={() => { setDraft(notes[0]?.note ?? ""); setEditing(true); }}><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 20h4L19 9l-4-4L4 16z" /></svg>{notes.length ? "Edit feedback" : "Add feedback"}</button> : null}
    {editing ? <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <label>Feedback<textarea required maxLength={1500} value={draft} onChange={event => setDraft(event.target.value)} /></label>
      <button type="submit" className="cta" disabled={busy || !draft.trim()}>{busy ? "Saving…" : "Save feedback"}</button>
      <button type="button" className="ghost-button" disabled={busy} onClick={() => setEditing(false)}>Cancel</button>
    </form> : null}
    {error ? <p role="alert">Coach feedback unavailable. <button type="button" className="ghost-button" onClick={() => { forgetRecordNotes(token, athleteId, source, targetId); setAttempt(value => value + 1); }}>Retry</button></p> : null}
  </div>;
}

type FeedbackPreview = Pick<Note, "note" | "exercise_ref" | "updated_at"> & { source: string; target_id: string };
export type FeedbackTarget = Pick<FeedbackPreview, "source" | "target_id">;

export function LatestCoachFeedback({ token, athleteId, onReview }: { token: string; athleteId: string; onReview?: (target: FeedbackTarget) => void }) {
  const [result, setResult] = useState<FeedbackPreview | "failed" | null>();
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({
      select: "note,source,target_id,exercise_ref,updated_at", athlete_id: `eq.${athleteId}`,
      order: "updated_at.desc,id.desc", limit: "1",
    });
    fetch(`${process.env.NEXT_PUBLIC_SUPABASE_URL}/rest/v1/coach_feedback?${params}`, {
      signal: controller.signal, cache: "no-store",
      headers: { apikey: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "", Authorization: `Bearer ${token}` },
    }).then(async response => {
      if (!response.ok) throw Error();
      const rows = await response.json();
      if (!Array.isArray(rows)) throw Error();
      if (!controller.signal.aborted) setResult(rows[0] ?? null);
    }).catch(() => { if (!controller.signal.aborted) setResult("failed"); });
    return () => controller.abort();
  }, [token, athleteId, attempt]);

  if (result === undefined) return <p>Loading feedback…</p>;
  if (result === null) return <p>No feedback yet</p>;
  if (result === "failed") return <p role="alert">Feedback unavailable <button type="button" onClick={() => {
    setResult(undefined);
    setAttempt(value => value + 1);
  }}>Retry</button></p>;
  const source = result.exercise_ref ? "Exercise feedback" : result.source === "sparring" ? "Sparring report feedback" : "Session feedback";
  return <div className={styles.feedback}>
    <strong>{source}</strong>
    <div><small>Updated {formatAppDate(result.updated_at.slice(0, 10))}</small></div>
    <p>{result.note}</p>
    {onReview && result.target_id ? <button type="button" className="ghost-button" onClick={() => onReview({ source: result.source, target_id: result.target_id })}>Review record</button> : null}
  </div>;
}
