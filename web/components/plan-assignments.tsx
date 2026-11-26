"use client";
import { DiscoveryHint } from "@/components/once-hint";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import type { CoachPlan } from "@/lib/api-schema.generated";
import { listPlanAssignments, respondPlanAssignment } from "@/lib/api";
import { isActivePlanOverlapError, type ActivePlanOverlapAction } from "@/lib/plan-active";
import { PlanSwitchDialog } from "./plan-switch-dialog";
import { AssignedPlanPreview } from "./assigned-plan-preview";
import { formatAppDate } from "@/lib/date-format";
import { readEpoch, recallView, rememberView } from "@/lib/shared-reads";
import styles from "./coach-workspace.module.css";

/** Remembered key for the coach-assigned plans this session last read. */
export const PLAN_ASSIGNMENTS_VIEW_KEY = "screen:plan-assignments";

/** The coach-assigned rows this session read moments ago, or null. */
export function recallPlanAssignments(token: string): CoachPlan[] | null {
  return recallView<CoachPlan[]>(token, PLAN_ASSIGNMENTS_VIEW_KEY);
}

export function PlanAssignments({ token, onActivated, onSettled }: {
  token: string;
  onActivated: () => void;
  /** Called once each read finishes, so the page can reveal everything together. */
  onSettled?: () => void;
}) {
  // A revisit shows the rows read last time at once, so the section never
  // pops in above the plan list and pushes it down.
  const [rows, setRows] = useState<CoachPlan[]>(() => recallPlanAssignments(token) ?? []);
  const [preview, setPreview] = useState<string | null>(null);
  const [overlap, setOverlap] = useState<CoachPlan | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const onSettledRef = useRef(onSettled);
  useEffect(() => { onSettledRef.current = onSettled; }, [onSettled]);
  useEffect(() => {
    const controller = new AbortController();
    const epoch = readEpoch();
    listPlanAssignments(token, controller.signal).then(data => {
      if (controller.signal.aborted) return;
      const next = data.filter(plan => plan.assignment_status === "pending" || plan.assignment_status === "active");
      setRows(next); setError("");
      rememberView(token, PLAN_ASSIGNMENTS_VIEW_KEY, next, epoch);
    })
      .catch(() => { if (!controller.signal.aborted) setError("Assigned plans unavailable."); })
      .finally(() => { if (!controller.signal.aborted) onSettledRef.current?.(); });
    return () => controller.abort();
  }, [token, attempt]);
  async function respond(plan: CoachPlan, action: "activate" | "decline", choice?: ActivePlanOverlapAction) {
    setPending(true); setError("");
    try {
      await respondPlanAssignment(token, plan.id, action, choice);
      setOverlap(null); setAttempt(n => n + 1);
      if (action === "activate") onActivated();
    } catch (reason) {
      if (action === "activate" && isActivePlanOverlapError(reason)) setOverlap(plan);
      else setError("Couldn't update. Refresh; the connection may have ended.");
    } finally { setPending(false); }
  }
  if (!rows.length && !error) return null;
  return <section aria-label="Coach assigned plans" className={styles.workspace}>
    <h2>From your coach</h2>
    {rows.some(plan => plan.assignment_status === "pending") ? <DiscoveryHint id="fighter-coach-plan" text="Have a look first. Your plan stays the same until you tap Activate." /> : null}
    {error ? <p role="alert">{error} <button type="button" disabled={pending} onClick={() => setAttempt(n => n + 1)}>Retry</button></p> : null}
    {rows.map(plan => <article key={plan.id} className={styles.panel}>
      <div className={styles.pageHead}><h3>{plan.name}</h3><span>{plan.assignment_status[0].toUpperCase() + plan.assignment_status.slice(1)}</span></div>
      <p>{plan.coach_name || "Your coach"} · Assigned {plan.assigned_at ? formatAppDate(plan.assigned_at) : ""}</p>
      <div className={styles.assignmentActions}>
        <button className="ghost-button" type="button" aria-expanded={preview === plan.id} onClick={() => setPreview(preview === plan.id ? null : plan.id)}>Preview Plan</button>
        {plan.published_plan_id ? <Link className="ghost-button" href={`/plans/${plan.published_plan_id}`}>View training plan</Link> : null}
      </div>
      {preview === plan.id ? <>
        <AssignedPlanPreview plan={plan} />
        {plan.assignment_status === "pending" ? <>
          <p className={styles.assignmentNote}>Your current plan stays active until you switch.</p>
          <div className={styles.assignmentActions}>
            <button type="button" className="ghost-button" disabled={pending} onClick={() => void respond(plan, "decline")}>Decline</button>
            <button type="button" className="cta" disabled={pending} onClick={() => void respond(plan, "activate")}>Activate plan</button>
          </div>
        </> : null}
      </> : null}
    </article>)}
    {overlap ? <PlanSwitchDialog plan={{ plan_name: overlap.name }} isPending={pending} errorMessage={error} onCancel={() => setOverlap(null)} onConfirm={choice => respond(overlap, "activate", choice)} /> : null}
  </section>;
}
