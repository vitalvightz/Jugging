"use client";

import { useState } from "react";
import { SessionCard } from "@/components/structured-plan-renderer";
import { submitTodaySessionCompletion } from "@/lib/api";
import type { TodayCommandView } from "@/lib/types";

/** The same prescribed exercise rows and completion endpoint as programme rehab. */
export function StandaloneRehab({ state, token, unavailable, refreshing = false, onRefresh }: {
  state: TodayCommandView; token: string; unavailable: boolean; refreshing?: boolean; onRefresh: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const live = state.live_prescription;
  // A planned mobility session is typed "rehab" too: only the standalone
  // rehab occurrence belongs here.
  if (!live || live.rehab_only !== true) return null;
  // A refresh only disables the buttons for its moment: a notice that came
  // and went with every refresh made the card jump.
  const held = live.safety_hold || unavailable || refreshing;
  async function complete(performance: "done_as_shown" | "changed" | "stopped") {
    if (!live || busy || (held && performance !== "stopped")) return;
    setBusy(true); setError("");
    try {
      await submitTodaySessionCompletion(token, {
        plan_id: null, session_id: live.session.session_id!, prescription_revision: live.revision,
        status: performance === "done_as_shown" ? "done" : "modified", rehab_performance: performance,
        rehab_tracking: "injury_checkin", modification_reason: performance === "changed" ? "Rehab modified" : performance === "stopped" ? "Stopped rehab" : "",
      });
      setSaved(performance === "done_as_shown" ? "Rehab completed" : performance === "changed" ? "Rehab modified" : "Rehab stopped");
      await onRefresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Could not save rehab. Refresh and try again."); }
    finally { setBusy(false); }
  }
  return <section id="today-rehab" className="panel today-card" aria-label="Today's rehab">
    <h2>Today’s rehab</h2>
    {saved ? <p role="status">{saved}</p> : <>
      {live.safety_hold || unavailable ? <p role="status">{unavailable ? "Refresh Today before completing rehab." : live.safety_hold_reason ?? "Rehab is on hold. Review your injury guidance."}</p> : null}
      <SessionCard session={live.session} layout="section" defaultOpenBlocks initiallyOpenFirstBlock={false} showDayContext={false} occurrenceDate={state.today.training_day} />
      <div className="today-action-row" role="group" aria-label="Rehab completion">
        <button type="button" className="cta" disabled={busy || held} onClick={() => void complete("done_as_shown")}>Complete rehab as shown</button>
        <button type="button" className="secondary-button" disabled={busy || held} onClick={() => void complete("changed")}>Modified rehab</button>
        <button type="button" className="ghost-button" disabled={busy || unavailable || refreshing || (held && live.session.completion_status !== "started")} onClick={() => void complete("stopped")}>Stopped rehab</button>
      </div>
    </>}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
