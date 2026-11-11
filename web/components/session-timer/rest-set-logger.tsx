"use client";

import { useState } from "react";
import { WeightRecommendation } from "../weight-recommendation";
import { SetRows } from "@/components/set-rows";
import { readSets } from "@/lib/set-entries";
import { leaveRemainingUnreported, leaveSetUnreported, confirmAllSetLogs, confirmSetLog, timerSetDrafts, pendingSetLogs } from "@/lib/session-timer/sets";
import type { SessionTimerController } from "./use-session-timer";

/** A contextual non-modal sheet: typing and confirming never pause or restart the clock. */
export function RestSetLogger({ timer, target }: { timer: SessionTimerController; target: { itemId: string; set: number } }) {
  const { state } = timer;
  const item = state.items.find(entry => entry.id === target.itemId);
  const rows = timerSetDrafts(state, target.itemId);
  if (item?.kind !== "sets" || !item.setFields || !rows) return null;
  const row = rows[target.set - 1];
  if (!row) return null;
  const unit = item.setFields.find(field => field.key === "load")?.apiUnit ?? "kg";
  const weighed = item.setFields.some(field => field.key === "load");
  const confirmed = state.confirmedSetLogs?.[item.id]?.includes(target.set) ?? false;
  const unreported = state.unreportedSetLogs?.[item.id]?.includes(target.set) ?? false;
  const read = readSets([row], unit);
  const prescribed = (key: "reps" | "load") => item.setFields?.find(field => field.key === key)?.hint;
  const transition = state.phase !== "rest";
  const timingNote = state.phase === "rest" ? "Rest continues" : state.phase === "ready" ? "Next exercise is ready" : "Session complete";
  const targetText = [weighed && prescribed("load") ? `${prescribed("load")} ${unit}` : null, prescribed("reps")].filter(Boolean).join(" × ");
  const recommendation = item.weightRecommendation
    ? <WeightRecommendation subtle value={{ ...item.weightRecommendation, ...item.weightRecommendation.sets[target.set - 1] }} />
    : null;
  const renderField = (field: "load" | "reps" | "rpe") => <label key={field}>
        <span>{field === "load" ? unit : field === "rpe" ? "RPE" : "Reps"}</span>
        <input type="text" inputMode={field === "reps" ? "numeric" : "decimal"} autoComplete="off" enterKeyHint="done" maxLength={8}
          aria-label={`${field === "load" ? `Load ${unit}` : field === "rpe" ? "RPE" : "Reps"}, set ${target.set}`}
          aria-invalid={read.invalid.includes(0) || undefined}
          value={row[field] ?? ""} placeholder={field === "reps" ? prescribed("reps") ?? "–" : "–"}
          onFocus={event => event.target.select()} onChange={event => {
            const value = event.target.value;
            timer.update(s => ({ ...s, setDrafts: { ...s.setDrafts, [item.id]: rows.map((entry, at) => at === target.set - 1 ? { ...entry, [field]: value } : entry) },
              confirmedSetLogs: { ...s.confirmedSetLogs, [item.id]: (s.confirmedSetLogs?.[item.id] ?? []).filter(set => set !== target.set) } }));
          }} />
      </label>;
  if (confirmed || unreported) return <div className="st-log-confirmed" role="status">
    <span>{unreported ? `${transition ? `${item.title} · ` : ""}Set ${target.set} · measurements unreported` : <><span aria-hidden="true">✓</span> {transition ? `${item.title} · ` : ""}Set {target.set} logged <span className="st-log-result">· {weighed && row.load ? `${row.load} ${unit} × ` : ""}{row.reps || "–"}{row.rpe ? ` · RPE ${row.rpe}` : ""}</span></>}</span>
    <button type="button" className="st-link" aria-label={`Edit set ${target.set}`} onClick={() => timer.update(s => ({ ...s, unreportedSetLogs: { ...s.unreportedSetLogs, [item.id]: (s.unreportedSetLogs?.[item.id] ?? []).filter(set => set !== target.set) }, confirmedSetLogs: { ...s.confirmedSetLogs, [item.id]: (s.confirmedSetLogs?.[item.id] ?? []).filter(set => set !== target.set) } }))}>Edit</button>
  </div>;
  return <section className="st-rest-sheet" aria-label="Log completed set">
    <div className="st-rest-sheet-head"><h3>{transition ? `${item.title} · set ${target.set}` : "Log completed set"}</h3><span>{targetText ? `Target: ${targetText}` : `Set ${target.set}`}</span></div>
    {recommendation}
    <div className="st-rest-fields" data-count={weighed ? 2 : 1}>
      {([...(weighed ? ["load" as const] : []), "reps" as const]).map(field => renderField(field))}
    </div>
    <details className="st-rest-effort" key={`${item.id}:${target.set}`}
      open={read.invalid.length && Boolean(row.rpe) ? true : undefined}>
      <summary>{row.rpe ? `RPE ${row.rpe} · optional` : "Add RPE (optional)"}</summary>
      <div className="st-rest-fields" data-count="1">{renderField("rpe")}</div>
    </details>
    {read.invalid.length ? <p className="st-log-error" role="alert">Check the values. RPE is 1–10 in whole or half steps.</p> : null}
    <button type="button" className="st-primary st-confirm-set" disabled={read.invalid.length > 0}
      onClick={() => { timer.update(s => confirmSetLog(s, target.itemId, target.set)); }}>✓ Confirm set</button>
    <p className="st-rest-help">Edit values if different · {timingNote}</p>
  </section>;
}

/** Missed confirmations are reviewed after training, never during a set. */
export function TimerSetReview({ timer }: { timer: SessionTimerController }) {
  const { state } = timer;
  const [error, setError] = useState(false);
  return <div className="st-set-review">
    <h2>Review your sets</h2><p className="st-note">Unknown values can stay unreported.</p>
    <div className="st-set-review-list">{state.items.map((item, index) => {
      const count = state.completed[index] ?? 0;
      const rows = timerSetDrafts(state, item.id);
      if (item.kind !== "sets" || !item.setFields || !rows || !count) return null;
      const unit = item.setFields.find(field => field.key === "load")?.apiUnit ?? "kg";
      const doneRows = rows.slice(0, count);
      const read = readSets(doneRows, unit);
      return <details key={item.id} open><summary>{item.title} <span>{count} sets</span></summary>
        <SetRows rows={doneRows} unit={unit} editableCount={false} showRpe unreported={state.unreportedSetLogs?.[item.id]}
          onLeaveUnreported={at => timer.update(s => leaveSetUnreported(s, item.id, at + 1))} showLoad={item.setFields.some(field => field.key === "load")} invalid={read.invalid}
          onChange={updated => timer.update(s => {
            const changed = updated.flatMap((row, at) => row.reps !== rows[at].reps || row.load !== rows[at].load || row.rpe !== rows[at].rpe ? [at + 1] : []);
            return { ...s, setDrafts: { ...s.setDrafts, [item.id]: [...updated, ...rows.slice(count)] },
              confirmedSetLogs: { ...s.confirmedSetLogs, [item.id]: (s.confirmedSetLogs?.[item.id] ?? []).filter(set => !changed.includes(set)) },
              unreportedSetLogs: { ...s.unreportedSetLogs, [item.id]: (s.unreportedSetLogs?.[item.id] ?? []).filter(set => !changed.includes(set)) },
            };
          })} />
      </details>;
    })}</div>
    {error ? <p className="st-log-error" role="alert">Check the highlighted sets.</p> : null}
    <button type="button" className="st-primary" onClick={() => {
      const next = confirmAllSetLogs(timer.state);
      setError(pendingSetLogs(next).length > 0);
      timer.update(() => next);
    }}>Save sets & continue</button>
    <button type="button" className="st-link st-leave-unreported" onClick={() => timer.update(leaveRemainingUnreported)}>Leave remaining unreported</button>
  </div>;
}
