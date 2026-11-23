"use client";
import { DiscoveryHint } from "@/components/once-hint";
import { useEffect, useState } from "react";
import { notifyFighterOfFeedback } from "@/lib/api";
import { coachRpc } from "@/lib/coach";
import { formatAppDate } from "@/lib/date-format";
import styles from "./coach-feedback.module.css";

type Note = { id: string; coach_name: string; note: string; exercise_ref: string; updated_at: string };
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
    const controller = new AbortController();
    setNotes([]);
    const params = new URLSearchParams({ select: "id,coach_name,note,exercise_ref,updated_at", athlete_id: `eq.${athleteId}`,
      source: `eq.${source}`, target_id: `eq.${targetId}`, exercise_ref: `eq.${exerciseRef}`, order: "created_at.asc" });
    fetch(`${process.env.NEXT_PUBLIC_SUPABASE_URL}/rest/v1/coach_feedback?${params}`, {
      signal: controller.signal, cache: "no-store", headers: { apikey: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "", Authorization: `Bearer ${token}` },
    }).then(async response => { if (!response.ok) throw Error(); return response.json(); })
      .then(rows => { if (!Array.isArray(rows)) throw Error(); if (!controller.signal.aborted) { setNotes(rows); setError(false); } })
      .catch(() => { if (!controller.signal.aborted) { setNotes([]); setError(true); } });
    return () => controller.abort();
  }, [token, athleteId, source, targetId, exerciseRef, attempt]);
  async function save() {
    setBusy(true);
    try {
      await coachRpc(token, "save_coach_feedback", { p_athlete_id: athleteId, p_source: source, p_target_id: targetId, p_exercise_ref: exerciseRef, p_note: draft.trim() });
      onSaved?.();
      setEditing(false); setAttempt(value => value + 1);
      // Best-effort: the note is saved whether or not the fighter is pinged.
      notifyFighterOfFeedback(token, athleteId, { source, target_id: targetId, exercise_ref: exerciseRef }).catch(() => undefined);
    } catch { setError(true); }
    finally { setBusy(false); }
  }
  return <div className={styles.feedback}>
    {editable && source === "sparring" && !exerciseRef && !notes.length && !editing ? <DiscoveryHint id="coach-feedback-note" text="Leave a note on this round." /> : null}
    {notes.map(note => <div key={note.id}><small>Coach {note.coach_name} · {formatAppDate(note.updated_at.slice(0, 10))}</small><p>{note.note}</p></div>)}
    {editable && !editing ? <button type="button" className="ghost-button" onClick={() => { setDraft(notes[0]?.note ?? ""); setEditing(true); }}>{notes.length ? "Edit feedback" : "Add feedback"}</button> : null}
    {editing ? <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <label>Feedback<textarea required maxLength={1500} value={draft} onChange={event => setDraft(event.target.value)} /></label>
      <button type="submit" className="cta" disabled={busy || !draft.trim()}>{busy ? "Saving…" : "Save feedback"}</button>
      <button type="button" className="ghost-button" disabled={busy} onClick={() => setEditing(false)}>Cancel</button>
    </form> : null}
    {error ? <p role="alert">Coach feedback unavailable. <button type="button" className="ghost-button" onClick={() => setAttempt(value => value + 1)}>Retry</button></p> : null}
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
