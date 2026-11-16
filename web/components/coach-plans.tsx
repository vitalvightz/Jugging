"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import type { CoachPlan, CoachPlannedSession } from "@/lib/api-schema.generated";
import { weekStart, dayOffset, type Fighter } from "@/lib/coach";
import { assignCoachPlan, listCoachPlans, getFighterPlan } from "@/lib/api";
import { CustomPlanBuilder } from "./custom-plan-builder";
import { AssignedPlanPreview } from "./assigned-plan-preview";
import { formatAppDate } from "@/lib/date-format";
import styles from "./coach-workspace.module.css";

export function CoachPlans({ token, fighters }: { token: string; fighters: Fighter[] }) {
  const [editorOpen, setEditorOpen] = useState(false);
  const [selected, setSelected] = useState(fighters[0]?.id ?? "");
  const fighter = fighters.find(row => row.id === selected);
  return <section className={styles.panel}>
    <label className={styles.fighterSelector}>Fighter <select disabled={editorOpen && Boolean(fighter)} value={fighter?.id ?? ""} onChange={event => { setSelected(event.target.value); setEditorOpen(false); }}>
      <option value="" disabled>Select fighter</option>
      {fighters.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}
    </select></label>
    {!fighters.length ? <p>Connect with a fighter before creating a plan.</p> : null}
    {fighter ? <FighterPlans key={`${token}:${fighter.connection_id}`} token={token} fighter={fighter} onEditorChange={setEditorOpen} /> : null}
  </section>;
}

function FighterPlans({ token, fighter, onEditorChange }: { token: string; fighter: Fighter; onEditorChange: (open: boolean) => void }) {
  const [plans, setPlans] = useState<CoachPlan[]>();
  const [schedule, setSchedule] = useState<CoachPlannedSession[]>();
  const [editing, setEditing] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    listCoachPlans(token, fighter.id, controller.signal).then(rows => { if (!controller.signal.aborted) { setPlans(rows); setError(""); } })
      .catch(() => { if (!controller.signal.aborted) setError("Plans unavailable. Retry to check your connection."); });
    return () => controller.abort();
  }, [token, fighter, attempt]);
  useEffect(() => {
    let active = true;
    getFighterPlan(token, fighter.id, true).then(rows => { if (active) setSchedule(rows); })
      .catch(() => { if (active) { setSchedule(undefined); setError("Training progress unavailable. Retry to refresh."); } });
    return () => { active = false; };
  }, [token, fighter, attempt]);
  if (editing !== null) return <CustomPlanBuilder accessToken={token} coachFighterId={fighter.id} planId={editing || undefined} onCoachClose={() => { setEditing(null); onEditorChange(false); setAttempt(n => n + 1); }} />;
  return <>
    <p>Build a plan for {fighter.name}. They choose whether to activate it.</p>
    <div className={styles.actions}><button type="button" className="cta" onClick={() => { setEditing(""); onEditorChange(true); }}>Create Plan</button><button type="button" className="ghost-button" onClick={() => setAttempt(n => n + 1)}>Refresh plans</button></div>
    {error ? <p role="alert">{error} <button type="button" onClick={() => setAttempt(n => n + 1)}>Retry</button></p> : null}
    {!plans && !error ? <p role="status">Loading plans…</p> : null}
    {plans?.length === 0 ? <p>No plans yet.</p> : null}
    {plans?.map(plan => <article key={plan.id} className={styles.panel}>
      <div className={styles.pageHead}><h2>{plan.name}</h2><span>{plan.assignment_status === "draft" ? "Draft" : plan.assignment_status[0].toUpperCase() + plan.assignment_status.slice(1)}</span></div>
      {plan.assigned_at ? <p>Assigned {formatAppDate(plan.assigned_at)}</p> : null}
      {plan.assignment_status === "draft" ? <div className={styles.actions}>
        <button type="button" className="ghost-button" onClick={() => { setEditing(plan.id); onEditorChange(true); }}>Edit draft</button>
        <button type="button" className="cta" disabled={pending || plan.incomplete} onClick={async () => {
          setPending(true); setError("");
          try { const sent = await assignCoachPlan(token, fighter.id, plan.id); setPlans(rows => rows?.map(row => row.id === sent.id ? sent : row)); }
          catch { setError("Unable to assign plan. Refresh and check that the fighter is still connected."); }
          finally { setPending(false); }
        }}>Assign to fighter</button>
        {plan.incomplete ? <p>Finish the schedule and exercise doses before assigning.</p> : null}
      </div> : null}
      <button type="button" className="ghost-button" aria-expanded={preview === plan.id} onClick={() => setPreview(preview === plan.id ? null : plan.id)}>Preview Plan</button>
      {preview === plan.id ? <AssignedPlanPreview plan={plan} /> : null}
      {plan.assignment_status === "active" ? <AssignedProgress plan={plan} fighter={fighter} schedule={schedule} /> : null}
      {plan.published_plan_id ? <Link href={`/coach/fighters/${fighter.id}#training-history`}>View fighter progress</Link> : null}
    </article>)}
  </>;
}

function AssignedProgress({ plan, fighter, schedule }: { plan: CoachPlan; fighter: Fighter; schedule: CoachPlannedSession[] | undefined }) {
  if (!schedule) return <p role="status">Loading training progress…</p>;
  const start = weekStart(fighter.today), end = dayOffset(start, 6);
  const planned = schedule.filter(row => row.plan_id === plan.published_plan_id && row.day >= start && row.day <= end);
  const logged = fighter.sessions.filter(row => row.plan_id === plan.published_plan_id);
  const matching = planned.map(session => ({ session, log: logged.find(row => row.session_id === session.id && row.day === session.day) }));
  const completed = matching.filter(row => row.log && ["done", "modified"].includes(row.log.status)).length;
  const upcoming = matching.filter(row => row.session.day >= fighter.today && !row.log);
  return <div>
    <p>This week: {completed} / {planned.length} scheduled sessions completed</p>
    <p>{matching.filter(row => row.log?.status === "modified").length} modified · {matching.filter(row => row.log?.status === "skipped").length} skipped</p>
    {matching.some(row => row.log && ["modified", "skipped"].includes(row.log.status)) ? <ul>{matching.filter(row => row.log && ["modified", "skipped"].includes(row.log.status)).map(({ session, log }) => <li key={`${session.day}:${session.id}`}>{formatAppDate(session.day)} · {session.title} · {log!.status}</li>)}</ul> : null}
    <p>Latest recorded progress: {logged[0] ? `${formatAppDate(logged[0].day)} · ${logged[0].status}${logged[0].rpe ? ` · RPE ${logged[0].rpe}` : ""}` : "No sessions recorded"}</p>
    <h3>Upcoming this week</h3>
    {upcoming.length ? <ul>{upcoming.map(({ session }) => <li key={`${session.day}:${session.id}`}>{formatAppDate(session.day)} · {session.title}</li>)}</ul> : <p>No upcoming sessions this week.</p>}
  </div>;
}
