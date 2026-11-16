"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import type { CoachPlan, CoachPlannedSession } from "@/lib/api-schema.generated";
import { weekStart, dayOffset, humanize, type Fighter } from "@/lib/coach";
import { assignCoachPlan, listCoachPlans, getFighterPlan } from "@/lib/api";
import { CustomPlanBuilder } from "./custom-plan-builder";
import { AssignedPlanPreview } from "./assigned-plan-preview";
import { StatusIcon } from "./record-icons";
import { formatAppDate } from "@/lib/date-format";
import styles from "./coach-workspace.module.css";

function PlanIcon({ name }: { name: "arrow-path" | "eye" | "chart-bar" | "plus" | "calendar-days" }) {
  // Heroicons (MIT), supplied as static assets rather than a new dependency.
  // eslint-disable-next-line @next/next/no-img-element
  return <img className={styles.planIcon} src={`/icons/coach/${name}.svg`} width={16} height={16} alt="" aria-hidden="true" />;
}

export function CoachPlans({ token, fighters }: { token: string; fighters: Fighter[] }) {
  const [editing, setEditing] = useState<string | null>(null);
  const editorOpen = editing !== null;
  const [selected, setSelected] = useState(fighters[0]?.id ?? "");
  const [refresh, setRefresh] = useState(0);
  const fighter = fighters.find(row => row.id === selected);
  return <section className={styles.plansDashboard} aria-label="Fighter plans dashboard">
    <header className={styles.plansPageHeader}>
      <div><h1><span className={styles.desktopPlanText}>Plans</span><span className={styles.mobilePlanText}>Fighter Plans</span></h1><p className={styles.desktopPlanText}>Build, assign and track training plans for your fighters.</p></div>
      <button type="button" className={styles.planRefresh} aria-label="Refresh plans" title="Refresh plans" disabled={editorOpen} onClick={() => setRefresh(n => n + 1)}><PlanIcon name="arrow-path" /></button>
    </header>
    <div className={styles.plansSelectorRow}>
      <label className={styles.fighterSelector}>Select fighter
        <select aria-label="Fighter" disabled={editorOpen && Boolean(fighter)} value={fighter?.id ?? ""} onChange={event => { setSelected(event.target.value); setEditing(null); }}>
          <option value="" disabled>Select fighter</option>
          {fighters.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}
        </select>
      </label>
      {fighter && !editorOpen ? <button type="button" className={styles.desktopCreatePlan} aria-label="Create New Plan" onClick={() => setEditing("")}><PlanIcon name="plus" />Create Plan</button> : null}
    </div>
    {!fighters.length ? <p className={styles.empty}>Connect with a fighter before creating a plan.</p> : null}
    {fighter ? <FighterPlans key={`${token}:${fighter.connection_id}`} token={token} fighter={fighter} refresh={refresh} editing={editing} setEditing={setEditing} /> : null}
  </section>;
}

function FighterPlans({ token, fighter, refresh, editing, setEditing }: { token: string; fighter: Fighter; refresh: number; editing: string | null; setEditing: (id: string | null) => void }) {
  const [plans, setPlans] = useState<CoachPlan[]>();
  const [schedule, setSchedule] = useState<CoachPlannedSession[]>();
  const [filter, setFilter] = useState<"all" | "active" | "draft" | "past">("all");
  const [scheduleOpen, setScheduleOpen] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    listCoachPlans(token, fighter.id, controller.signal).then(rows => { if (!controller.signal.aborted) { setPlans(rows); setError(""); } })
      .catch(() => { if (!controller.signal.aborted) setError("Plans unavailable. Retry to check your connection."); });
    return () => controller.abort();
  }, [token, fighter, attempt, refresh]);
  useEffect(() => {
    let active = true;
    getFighterPlan(token, fighter.id, true).then(rows => { if (active) setSchedule(rows); })
      .catch(() => { if (active) { setSchedule(undefined); setError("Training progress unavailable. Retry to refresh."); } });
    return () => { active = false; };
  }, [token, fighter, attempt, refresh]);
  if (editing !== null) return <CustomPlanBuilder accessToken={token} coachFighterId={fighter.id} athleteTimezone={fighter.timezone} initialPlan={editing.startsWith("revision:") ? plans?.find(plan => plan.id === editing.slice(9)) : undefined} planId={editing.startsWith("revision:") ? undefined : editing || undefined} onCoachClose={() => { setEditing(null); setAttempt(n => n + 1); }} />;
  // Put the current prescription first, retaining the saved order of other plans.
  const ordered = plans?.toSorted((a, b) => Number(b.assignment_status === "active") - Number(a.assignment_status === "active"));
  const past = (row: CoachPlan) => ["declined", "superseded"].includes(row.assignment_status);
  const visible = ordered?.filter(row => filter === "all" || (filter === "past" ? past(row) : row.assignment_status === filter));
  return <>
    {!plans?.length ? null : <div className={`${styles.planFilters} ${plans.length > 1 ? styles.multiplePlanFilters : ""}`} role="group" aria-label="Filter plans">
      {([['all', 'All Plans'], ['active', 'Active'], ['draft', 'Drafts'], ['past', 'Past']] as const).map(([value, label]) => <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}{value === 'all' ? '' : ` (${plans.filter(row => value === 'past' ? past(row) : row.assignment_status === value).length})`}</button>)}
    </div>}
    {error ? <p className={styles.error} role="alert">{error} <button type="button" onClick={() => setAttempt(n => n + 1)}>Retry</button></p> : null}
    {!plans && !error ? <p role="status">Loading plans…</p> : null}
    {plans?.length === 0 ? <div className={styles.planEmpty}><h2>No plans yet</h2><p>Create a draft for {fighter.name}, then send it for their review.</p></div> : null}
    {Boolean(plans?.length) && !visible?.length ? <p className={styles.empty}>No plans in this view.</p> : null}
    {visible?.map(plan => <article key={plan.id} className={styles.coachPlanCard} aria-label={plan.name}>
      <header className={styles.planCardHeader}>
        <span className={styles.planCalendarIcon}><PlanIcon name="calendar-days" /></span>
        <div><p className={styles.planEyebrow}>{plan.assignment_status === "active" ? "Current plan" : plan.assignment_status === "draft" ? "Draft plan" : "Assigned plan"}</p><h2>{plan.name}</h2>{plan.assigned_at ? <p className={styles.planAssignedDate}>Assigned {formatAppDate(plan.assigned_at).split(" ").slice(1).join(" ")}</p> : null}</div>
        <span className={styles.planStatus} data-assignment-status={plan.assignment_status}>{humanize(plan.assignment_status)}</span>
      </header>
      {plan.assignment_status === "active" ? <AssignedProgress plan={plan} fighter={fighter} schedule={schedule} /> : null}
      <div className={styles.planCardActions}>
        <button type="button" aria-expanded={preview === plan.id} onClick={() => setPreview(preview === plan.id ? null : plan.id)}><PlanIcon name="eye" />Preview Plan</button>
        {plan.published_plan_id ? <Link href={`/coach/fighters/${fighter.id}#training-history`}><PlanIcon name="chart-bar" /><span className={styles.mobilePlanText}>Progress</span><span className={styles.desktopPlanText}>View Fighter Progress</span></Link> : null}
        {plan.assignment_status === "active" ? <button className={styles.desktopScheduleAction} type="button" aria-expanded={scheduleOpen === plan.id} onClick={() => setScheduleOpen(scheduleOpen === plan.id ? null : plan.id)}><PlanIcon name="calendar-days" />View Schedule</button> : null}
      </div>
      {scheduleOpen === plan.id ? <section className={styles.planSchedule} aria-label={`${plan.name} schedule`}><h3>Training schedule</h3>{!schedule ? <p role="status">Loading schedule…</p> : schedule.filter(row => row.plan_id === plan.published_plan_id).length ? <ul>{schedule.filter(row => row.plan_id === plan.published_plan_id).map(row => <li key={`${row.day}:${row.id}`}><span>{formatAppDate(row.day)}</span><strong>{row.title}</strong></li>)}</ul> : <p>No scheduled sessions available.</p>}</section> : null}
      {preview === plan.id ? <AssignedPlanPreview plan={plan} /> : null}
      {plan.assignment_status !== "draft" ? <button type="button" className="ghost-button" onClick={() => setEditing(`revision:${plan.id}`)}>Create revised draft</button> : null}
      {plan.assignment_status === "draft" ? <div className={styles.planDraftActions}>
        <button type="button" className="ghost-button" onClick={() => { setEditing(plan.id); }}>Edit draft</button>
        <button type="button" className="cta" disabled={pending || plan.incomplete} onClick={async () => {
          setPending(true); setError("");
          try { const sent = await assignCoachPlan(token, fighter.id, plan.id); setPlans(rows => rows?.map(row => row.id === sent.id ? sent : row)); }
          catch { setError("Unable to assign plan. Refresh and check that the fighter is still connected."); }
          finally { setPending(false); }
        }}>Assign to fighter</button>
        {plan.incomplete ? <p>Finish the schedule and exercise doses before assigning.</p> : null}
      </div> : null}
    </article>)}
    <button type="button" className={styles.newPlanAction} onClick={() => { setEditing(""); }}><PlanIcon name="plus" />Create New Plan</button>
  </>;
}

export function AssignedProgress({ plan, fighter, schedule }: { plan: CoachPlan; fighter: Fighter; schedule: CoachPlannedSession[] | undefined }) {
  if (!schedule) return <p role="status">Loading training progress…</p>;
  const start = weekStart(fighter.today), end = dayOffset(start, 6);
  const planned = schedule.filter(row => row.plan_id === plan.published_plan_id && row.day >= start && row.day <= end);
  const logged = fighter.sessions.filter(row => row.plan_id === plan.published_plan_id);
  const matching = planned.map(session => ({ session, log: logged.find(row => row.session_id === session.id && row.day === session.day) }));
  const completed = matching.filter(row => row.log && ["done", "modified"].includes(row.log.status)).length;
  const modified = matching.filter(row => row.log?.status === "modified").length;
  const skipped = matching.filter(row => row.log?.status === "skipped").length;
  const upcoming = matching.filter(row => row.session.day >= fighter.today && !row.log);
  const percentage = planned.length ? Math.round(completed / planned.length * 100) : 0;
  const allComplete = planned.length > 0 && completed === planned.length;
  const latest = logged[0];
  const summary = allComplete ? "All sessions complete this week" : !planned.length ? "No sessions scheduled this week" : upcoming.length ? `${upcoming.length} upcoming ${upcoming.length === 1 ? "session" : "sessions"} this week` : skipped ? `${skipped} ${skipped === 1 ? "session" : "sessions"} skipped this week` : "Sessions still to record this week";
  return <div className={styles.planProgress}>
    <div className={styles.completionMeter}><p className={styles.completionLabel}>Weekly completion</p>
    <div className={styles.completionHeading}><strong>{completed}<span> / {planned.length}</span></strong><span className={styles.completionPercentage}>{planned.length ? `${percentage}%` : "—"}</span></div>
    <progress className={styles.completionBar} value={percentage} max={100} aria-label="Weekly completion" aria-valuetext={planned.length ? `${completed} of ${planned.length} sessions complete, ${percentage}%` : "No sessions scheduled"} /></div>
    <dl className={styles.completionStats}><div><dt>Modified</dt><dd>{modified}</dd></div><div><dt>Skipped</dt><dd>{skipped}</dd></div></dl>
    <div className={styles.planWeekSummary} data-complete={allComplete}>
      <p>{allComplete ? <StatusIcon status="done" /> : null}<strong>{summary}</strong></p>
      <p className={styles.planLatest}>Latest: {latest ? `${formatAppDate(latest.day).split(" ").slice(1, 3).join(" ")} · ${humanize(latest.status)}${latest.rpe ? ` · RPE ${latest.rpe}` : ""}` : "No sessions recorded"}</p>
    </div>
    {matching.some(row => row.log && ["modified", "skipped"].includes(row.log.status)) ? <details className={styles.planSessionDetails}><summary>Modified &amp; skipped sessions</summary><ul>{matching.filter(row => row.log && ["modified", "skipped"].includes(row.log.status)).map(({ session, log }) => <li key={`${session.day}:${session.id}`}>{formatAppDate(session.day)} · {session.title} · {humanize(log!.status)}</li>)}</ul></details> : null}
    {upcoming.length ? <ul className={styles.planUpcoming}>{upcoming.map(({ session }) => <li key={`${session.day}:${session.id}`}>{formatAppDate(session.day)} · {session.title}</li>)}</ul> : null}
  </div>;
}
