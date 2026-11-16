"use client";
import { DiscoveryHint } from "@/components/once-hint";
import { useEffect, useState } from "react";
import Link from "next/link";
import type { CoachPlan } from "@/lib/api-schema.generated";
import { listPlanAssignments, respondPlanAssignment } from "@/lib/api";
import { isActivePlanOverlapError, type ActivePlanOverlapAction } from "@/lib/plan-active";
import { PlanSwitchDialog } from "./plan-switch-dialog";
import { AssignedPlanPreview } from "./assigned-plan-preview";
import { formatAppDate } from "@/lib/date-format";
import styles from "./coach-workspace.module.css";

export function PlanAssignments({ token, onActivated }: { token: string; onActivated: () => void }) {
  const [rows, setRows] = useState<CoachPlan[]>([]);
  const [preview, setPreview] = useState<string | null>(null);
  const [overlap, setOverlap] = useState<CoachPlan | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    listPlanAssignments(token, controller.signal).then(data => { if (!controller.signal.aborted) { setRows(data); setError(""); } })
      .catch(() => { if (!controller.signal.aborted) setError("Assigned plans unavailable."); });
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
      else setError("Unable to update assignment. Refresh and try again; the connection may have ended.");
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
      <button className="ghost-button" type="button" aria-expanded={preview === plan.id} onClick={() => setPreview(preview === plan.id ? null : plan.id)}>Preview Plan</button>
      {preview === plan.id ? <>
        <AssignedPlanPreview plan={plan} />
        {plan.assignment_status === "pending" ? <>
          <p>Your current plan stays active until you choose to switch.</p>
          <div className={styles.actions}>
            <button type="button" className="cta" disabled={pending} onClick={() => void respond(plan, "activate")}>Activate plan</button>
            <button type="button" className="ghost-button" disabled={pending} onClick={() => void respond(plan, "decline")}>Decline</button>
          </div>
        </> : null}
      </> : null}
      {plan.published_plan_id ? <Link href={`/plans/${plan.published_plan_id}`}>View training plan</Link> : null}
    </article>)}
    {overlap ? <PlanSwitchDialog plan={{ plan_name: overlap.name }} isPending={pending} errorMessage={error} onCancel={() => setOverlap(null)} onConfirm={choice => respond(overlap, "activate", choice)} /> : null}
  </section>;
}
