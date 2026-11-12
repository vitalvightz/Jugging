"use client";

import { useSeenOnce } from "@/components/once-hint";
import { useId, useRef, useState } from "react";
import { ApiError, submitInjuryEpisodeObservation } from "@/lib/api";
import type { InjuryFlagRecord, TodayCommandView } from "@/lib/types";
import { InjuryChevron, InjuryDetailSheet } from "./injury-detail-sheet";
import { RehabProgressStatus, rehabStageLabel } from "./rehab-progress-status";

const SCHEDULE_LABELS = { due: "Rehab due today", recovery_day: "Rest day", already_completed: "Done for today",
  held: "Today’s rehab on hold", deferred: "Rehab moved", unsupported: "No rehab yet" } as const;
const ROUTINE_SCHEDULE_STATES = new Set<string>(["due", "recovery_day", "already_completed"]);
const NEXT_DAY_FORMAT = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });

/** "Sat 10 Oct" for an ISO training day; the raw value if it does not parse. */
function formatNextDay(day: string): string {
  const date = new Date(`${day}T12:00:00Z`);
  return Number.isNaN(date.getTime()) ? day : NEXT_DAY_FORMAT.format(date).replace(",", "");
}

export function EffectiveClinicianClearanceStatus({ clearance }: {
  clearance: TodayCommandView["effective_clinician_clearance"];
}) {
  if (!clearance) return null;
  const label = { rehab_only: "Rehab only", train_no_contact: "Non-contact training", train_contact: "Contact training" }[clearance.level];
  return <div className="today-injury-guidance" role="note" aria-label="Effective clinician clearance">
    <p><strong>Reported clearance: {label}</strong> <small className="muted">· {clearance.level === "train_contact" ? "based on" : "limited by"} {clearance.limited_by.map(injury => injury.label).join(", ")}</small></p>
    {clearance.requires_update ? <p>Scope unclear. Rehab only until clarified.</p> : null}
  </div>;
}

export function InjuryClearance({ injury, token, onRefresh }: {
  injury: InjuryFlagRecord; token: string; onRefresh: () => Promise<void>;
}) {
  const [, dismissClearanceHint] = useSeenOnce("discovery-injury-clearance-seen");
  const [choosing, setChoosing] = useState(false);
  const [rehabLevel, setRehabLevel] = useState<"gentle_recovery" | "loading" | "sport_specific" | "not_cleared">("not_cleared");
  const [trainingLevel, setTrainingLevel] = useState("rehab");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const clearanceEditorId = useId();
  const pendingReport = useRef<{ key: string; id: string } | null>(null);
  async function report() {
    const scopes: Array<"rehab" | "training" | "contact"> = trainingLevel === "contact" ? ["rehab", "training", "contact"] : trainingLevel === "training" ? ["rehab", "training"] : ["rehab"];
    if (!injury.episode_id || busy) return;
    setBusy(true); setError("");
    try {
      const key = `${injury.episode_id}:${scopes.join(",")}:${rehabLevel}`;
      if (pendingReport.current?.key !== key) pendingReport.current = { key, id: crypto.randomUUID() };
      await submitInjuryEpisodeObservation(token, { injury_id: injury.id, injury_episode_id: injury.episode_id,
        event_type: "clinician_clearance_report", scopes, rehabilitation_permission: { schema_version: 1, level: rehabLevel }, report_id: pendingReport.current.id });
      await onRefresh();
      pendingReport.current = null;
      setChoosing(false);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not save your report."); }
    finally { setBusy(false); }
  }
  const clearance = injury.clinician_clearance;
  const scopeLabels: Record<string, string> = {
    rehab: "Rehab only",
    "rehab,training": "Non-contact training",
    "contact,rehab,training": "Contact training",
  };
  const scopeLabel = scopeLabels[[...(clearance?.scopes ?? [])].sort().join(",")]
    ?? "Scope unclear — update required";
  const surface = injury.rehab_decision?.outcome === "wound_care" || Boolean(injury.surface_class && injury.surface_class !== "non_surface");
  if (!clearance && (!injury.episode_id || surface)) return null;
  const labels = { gentle_recovery: "Gentle recovery", loading: "Strengthening / loading", sport_specific: "Sport-specific rehab", not_cleared: "Not sure / not cleared" };
  return <div className="injury-clearance">
    <button type="button" className="injury-detail-link" aria-expanded={choosing} aria-haspopup="dialog"
      aria-label={clearance ? "Change clearance" : "Report clearance"} disabled={busy}
      onClick={() => { dismissClearanceHint(); setRehabLevel(clearance?.rehabilitation_permission?.level ?? "not_cleared"); setTrainingLevel(clearance?.scopes.includes("contact") ? "contact" : clearance?.scopes.includes("training") ? "training" : "rehab"); setError(""); setChoosing(true); }}>
      <span>Clearance</span>
      {/* Just the training level, on the same line; the rehab level and its
          self-reported source are in the "Clearance & restrictions" sheet. */}
      {clearance ? <span className="injury-clearance-value">{scopeLabel}</span> : null}
      <InjuryChevron />
    </button>
    {choosing ? <InjuryDetailSheet title="Clearance & restrictions" busy={busy} onClose={() => setChoosing(false)}>
      <form onSubmit={event => { event.preventDefault(); void report(); }}>
        <div className="injury-sheet-body">
          <p className="injury-sheet-caption">Clinical Clearance · Self-reported</p>
          <p className="muted">Record your clinician’s advice. Rehab follows the level you report, with injury restrictions and response holds. Stages without published rehab stay unavailable.</p>
          <fieldset className="injury-permission-options" disabled={busy || !injury.episode_id || surface}>
            <legend>Rehabilitation level</legend>
            <p className="injury-entry-help">The rehab exercises your clinician allows.</p>
            {(["not_cleared", "gentle_recovery", "loading", "sport_specific"] as const).map(value => <label key={value} data-selected={rehabLevel === value}>
              <input type="radio" name={`${clearanceEditorId}-rehab`} value={value} checked={rehabLevel === value} onChange={() => setRehabLevel(value)} />
              <span>{labels[value]}</span>
              {rehabLevel === value ? <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg> : null}
            </label>)}
          </fieldset>
          <fieldset className="injury-permission-options" disabled={busy || !injury.episode_id || surface}>
            <legend>Training level</legend>
            <p className="injury-entry-help">Whether your clinician allows training and contact.</p>
            {([['rehab', 'Rehab only'], ['training', 'Non-contact training'], ['contact', 'Contact training']] as const).map(([value, label]) => <label key={value} data-selected={trainingLevel === value}>
              <input type="radio" name={`${clearanceEditorId}-training`} value={value} checked={trainingLevel === value} onChange={() => setTrainingLevel(value)} />
              <span>{label}</span>
              {trainingLevel === value ? <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg> : null}
            </label>)}
          </fieldset>
          <p className="injury-sheet-note">Update only when your clinician’s advice changes. Restrictions and safety holds still apply.</p>
          <p className="injury-sheet-note">Unlxck does not issue or verify medical clearance.</p>
          {injury.rehab_decision?.prescription?.sources?.length ? <details className="injury-guidance-sources"><summary>Guidance sources</summary>
            {injury.rehab_decision.prescription.sources.map((source, index) => <a key={source} href={source} target="_blank" rel="noopener noreferrer">Routine guidance{index ? ` ${index + 1}` : ""}</a>)}
          </details> : null}
          {error ? <p role="alert">{error}</p> : null}
        </div>
        <footer className="injury-sheet-footer"><button type="submit" className="injury-sheet-save" disabled={busy || !injury.episode_id || surface}>{busy ? "Saving…" : "Save changes"}</button></footer>
      </form>
    </InjuryDetailSheet> : null}
  </div>;
}

export function InjuryCareStatus({ injury, token, onRefresh, showClearance = true, summaryVisible = false, rehabHref }: {
  injury: InjuryFlagRecord; token: string; onRefresh: () => Promise<void>; showClearance?: boolean; summaryVisible?: boolean; rehabHref?: string;
}) {
  const schedule = injury.rehab_decision?.schedule;
  const outcome = injury.rehab_decision?.outcome;
  // Explain only engine-reported medical gates; never infer one from severity.
  const medicalReason = outcome !== "medical_review" ? null
    : injury.rehab_decision?.reason_codes.includes("red_flag_medical_gate")
      ? "A reported warning sign needs medical review."
      : injury.rehab_decision?.reason_codes.includes("urgent_injury_type")
        ? "The injury type you reported needs medical review before training."
        : null;
  const trackingOnly = outcome === "unsupported_prescription" || (outcome === "missing_information" && injury.rehab_decision?.reason_codes.includes("missing_injury_identity"));
  const resolved = outcome === "no_rehab_indicated" && injury.rehab_decision?.reason_codes.includes("resolved_episode");
  const summary = injury.rehab_decision?.reason_codes.includes("missing_injury_identity")
    ? "Guided rehab could not be matched. Review your injury details."
    : injury.rehab_decision?.summary;
  // Routine states say everything in their label, so their backend reason
  // ("You have already logged rehab for this injury today.") is not repeated.
  // Held, deferred and unsupported keep it: it says why.
  const routine = schedule ? ROUTINE_SCHEDULE_STATES.has(schedule.state) : false;
  const scheduleLabel = schedule
    ? schedule.state === "already_completed" && /started session/i.test(schedule.reason)
      ? "In today's session"
      : schedule.state === "due" && !rehabHref ? "Rehab unavailable" : SCHEDULE_LABELS[schedule.state]
    : "";
  const scheduleReason = !schedule || routine ? ""
    : schedule.reason === injury.rehab_decision?.summary ? summary ?? "" : schedule.reason;
  // A matched routine's summary ("Your rehab is matched…") only restates that
  // there is a schedule; the schedule line carries the state instead.
  const showSummary = Boolean(summary) && !(injury.rehab_decision?.outcome === "prescribed_rehab" && schedule)
    && summary !== scheduleReason;
  // Under a stage block, a routine non-actionable state ("Done for today",
  // "Rest day") folds into it as a small line instead of its own row. "Rehab
  // due today" keeps its row: it links to today's exercises.
  const foldSchedule = outcome === "prescribed_rehab" && Boolean(rehabStageLabel(injury.rehab_decision))
    && Boolean(schedule) && routine && schedule?.state !== "due";
  const stageStatusLine = foldSchedule && schedule
    // Non-breaking spaces keep "next Sat 10 Oct" on one line when it wraps.
    ? `${scheduleLabel}${schedule.next_due_day ? ` · next\u00a0${formatNextDay(schedule.next_due_day).replace(/ /g, "\u00a0")}` : ""}`
    : null;
  const showScheduleRow = !trackingOnly && !resolved && Boolean(schedule) && !foldSchedule;
  const showTrackingSchedule = trackingOnly && (schedule?.state === "held" || schedule?.state === "deferred");
  const showReason = Boolean(scheduleReason) && !resolved
    && (!trackingOnly || ((schedule?.state === "held" || schedule?.state === "deferred") && scheduleReason !== summary));
  const hasGuidance = Boolean(medicalReason) || trackingOnly || resolved || showSummary || showScheduleRow || showTrackingSchedule || showReason;
  return <div className="today-injury-care">
    {injury.rehab_decision ? <>
      {outcome === "prescribed_rehab" ? <RehabProgressStatus decision={injury.rehab_decision} statusLine={stageStatusLine} summaryVisible={summaryVisible} /> : null}
      {hasGuidance ? <div className="injury-rehab-status" role="note" aria-label="Guidance">
        {medicalReason ? <p className="injury-safety-reason">{medicalReason}</p> : null}
        {trackingOnly ? <p className="muted">{outcome === "unsupported_prescription" ? "No rehab exercises available for this stage yet." : summary}</p>
          : resolved ? <p className="injury-rehab-label"><strong>Recovered</strong></p>
          : showSummary ? <p>{summary}</p> : null}
        {showScheduleRow && schedule ? <>
          {schedule.state === "due" && rehabHref ? <a className="injury-rehab-action" href={rehabHref}><span><strong>{scheduleLabel}</strong><small>View rehab</small></span><InjuryChevron /></a>
            : <p className="injury-rehab-label"><strong>{scheduleLabel}</strong>{schedule.next_due_day ? <small>Next {formatNextDay(schedule.next_due_day)}</small> : null}</p>}
        </> : null}
        {showTrackingSchedule ? <p className="injury-rehab-label"><strong>{scheduleLabel}</strong></p> : null}
        {showReason ? <p className="injury-safety-reason">{scheduleReason}</p> : null}
      </div> : null}
      {(!injury.episode_id || injury.rehab_decision.outcome === "wound_care" || Boolean(injury.surface_class && injury.surface_class !== "non_surface")) && injury.rehab_decision.prescription?.sources?.length ? <details className="injury-guidance-sources"><summary>Guidance sources</summary>
        {injury.rehab_decision.prescription.sources.map((source, index) => <a key={source} href={source} target="_blank" rel="noopener noreferrer">Routine guidance{index ? ` ${index + 1}` : ""}</a>)}
      </details> : null}
    </> : null}
    {showClearance ? <InjuryClearance key={`${injury.id}:${injury.episode_id}`} injury={injury} token={token} onRefresh={onRefresh} /> : null}
  </div>;
}

export function DelayedRehabResponse({ prompt, token, onRefresh }: {
  prompt: NonNullable<TodayCommandView["delayed_rehab_prompts"]>[number];
  token: string; onRefresh: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [closed, setClosed] = useState(false);
  const [error, setError] = useState("");
  async function answer(response: typeof prompt.options[number]) {
    if (busy) return;
    setBusy(true); setError("");
    try {
      await submitInjuryEpisodeObservation(token, { injury_id: prompt.injury_id, injury_episode_id: prompt.injury_episode_id,
        event_type: "delayed_rehab_response", exposure_id: prompt.exposure_id, response });
      setSaved(true);
      await onRefresh();
    } catch (e) {
      // 409: the question is not open (yet or any more), e.g. a Today screen
      // loaded before a day rollover or a deploy. Drop it instead of showing a
      // raw server error; the refresh brings back the correct prompts.
      if (e instanceof ApiError && e.status === 409) {
        setClosed(true);
        await onRefresh().catch(() => {});
      } else {
        setError(e instanceof Error ? e.message : "Could not save your response.");
      }
    }
    finally { setBusy(false); }
  }
  if (closed) return null;
  if (saved) return <p role="status">Saved. Thanks.</p>;
  return <div className="today-injury-guidance" role="group" aria-label={`Next-day response for ${prompt.region}`}>
    <p>How&apos;s your <strong>{prompt.region.toLowerCase()}</strong> after yesterday&apos;s rehab?</p>
    <div className="today-segment-row">
      {prompt.options.map(option => <button key={option} type="button" className="today-segment" disabled={busy} onClick={() => answer(option)}>
        {({ better: "Better", same: "Same", worse: "Worse", not_sure: "Not sure" })[option]}
      </button>)}
    </div>
    {error ? <p role="alert">{error}</p> : null}
  </div>;
}
