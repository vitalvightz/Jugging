"use client";

import { useState } from "react";
import type { InjuryFlagRecord } from "@/lib/types";
import { InjuryChevron, InjuryDetailSheet } from "./injury-detail-sheet";

const STAGES = ["calm", "restore", "load", "dynamic", "return"] as const;
const LABELS: Record<string, string> = { calm: "Calm", restore: "Restore", load: "Load", dynamic: "Dynamic", return: "Return" };
const REQUIREMENTS: Record<string, string> = {
  no_unresolved_setback: "Injury response", complete_history: "Complete rehab history",
  completed_reviewed_exposure: "Current-stage rehab", during_session_response: "Response after rehab",
  next_day_response: "Next-day response", functional_checkpoint: "Permission and exercise checks",
};
const REASONS: Record<string, string> = {
  episode_setback_loading_held: "Your injury got worse. Follow today’s guidance and your clinician’s limits.",
  ankle_uncomplicated_lateral_not_reported: "This starter is for a clinician-described uncomplicated outer ankle sprain. Set the sprain type in Edit injury if known; otherwise follow today's guidance.",
  ankle_conflicting_or_unsupported_presentation: "Your ankle details include another sprain type, instability or a concern needing assessment. Loading stays on hold.",
  ankle_loading_severity_not_supported: "This starter is only available for a mild uncomplicated sprain. Follow today's conservative guidance.",
  assessment_observation_missing: "LOAD is on hold. Your elbow records do not confirm grip, movement or exercise tolerance. Follow today’s guidance.",
  elbow_function_assessment_incomplete: "LOAD is on hold. Your elbow records do not confirm grip, movement or exercise tolerance. Follow today’s guidance.",
  elbow_lateral_applicability_not_confirmed: "Confirm the elbow location in Edit injury. This starter is for outside-of-elbow tendon problems only.",
  elbow_function_or_safety_not_acceptable: "Your reported elbow function or symptoms do not support this starter yet.",
  elbow_later_unsatisfactory_function: "A later assessment reported unsuitable elbow function. An older assessment cannot replace it.",
  elbow_hand_weight_option_not_recommended: "Your clinician has not recommended this exact starter.",
  elbow_conflicting_or_unsupported_presentation: "Your injury details include a different elbow presentation or another concern.",
  elbow_loading_severity_not_supported: "Your current injury severity does not support this starter. Follow today's conservative guidance.",
  assessment_observation_stale_or_future: "Suitability evidence isn't current for this injury. Loading on hold; follow today's guidance.",
  assessment_history_incomplete: "Assessment history is unavailable. Loading stays on hold.",
  assessment_medical_or_setback_hold: "A worsening response or medical concern is holding progression.",
  rehabilitation_loading_not_reported: "Confirm the rehab loading your clinician allows in Clearance & restrictions.",
  achilles_midportion_not_confirmed: "Confirm the Achilles location in your injury details. This exercise is for midportion problems only.",
  injury_loading_safety_hold: "Your current injury restrictions take priority over loading permission.",
  current_report_worse: "Your injury got worse. Follow today’s guidance.",
  unresolved_historical_negative_response: "A previous worsening response is still holding progression.",
  unresolved_injury_checkin_setback: "You reported this injury worse earlier. Log today’s rehab and how it felt to move on.",
  incomplete_prior_rehab_evidence: "Your past rehab record is incomplete. Loading stays on hold.",
  prior_stage_work_not_required_for_reported_permission: "Prior rehab logs are not required for this clinician-permitted entry.",
  prior_restore_not_required_for_reported_permission: "Prior RESTORE logs are not required for this clinician-permitted entry.",
  no_prior_rehab_response_to_resolve: "No past rehab response is recorded. This does not prove tolerance.",
  history_truncated: "Your full rehab history is unavailable. Progression stays on hold.",
  reviewed_target_content_unavailable: "No approved next-stage exercise matches your current injury and equipment.",
  target_equipment_unavailable: "This needs equipment you haven’t listed. Add it if you have it; otherwise follow today’s rehab.",
  target_adverse_response_unresolved: "This loading exercise is held after a worse response. Follow today’s rehab and clinician advice.",
  target_stage_not_activated: "The next stage is not available in Unlxck yet.",
  no_clinical_criteria_declared: "This progression has not been activated in Unlxck.",
  transition_closed_by_profile: "This stage is not available for this injury profile.",
  no_reviewed_stage_exposure: "Complete your current rehabilitation exercise in Today.",
  no_defined_dose_completed: "No completed current-stage prescription is recorded yet.",
  no_reviewed_response_group: "An injury response after current-stage rehab is still needed.",
  during_response_not_reported: "After your rehab, tap how it feels under How’s your injury. Your latest answer that day counts.",
  next_day_response_not_reported: "Answer the next-day rehab follow-up when it appears in Today.",
  next_day_response_not_yet_known: "Tomorrow, answer the follow-up on Today about this rehab.",
  next_day_response_not_sure: "Your next-day response is unclear. Loading stays on hold. Follow today’s rehab.",
  during_response_not_sure: "Your rehab response is unclear. Follow today’s permitted rehab.",
  inconsistent_response_group: "Your last rehab responses are missing or do not match. Loading stays on hold.",
  fail_stopped_due_to_symptoms: "Stopped rehab or worsening symptoms are still holding loading. Follow your current injury guidance.",
  fail_during_response_worse: "Your injury felt worse after rehab. Follow today’s guidance.",
  fail_next_day_response_worse: "Your injury felt worse the next day. Follow today’s guidance.",
  fail_worsening_reported: "Loading is held after a worse response. Follow today’s guidance.",
  shoulder_nontraumatic_cuff_not_reported: "Confirm what your clinician described in Edit injury. This starter is for non-traumatic rotator cuff tendinopathy without a tear.",
  shoulder_conflicting_or_unsupported_presentation: "Your shoulder details include another presentation or a concern needing assessment. Follow your current restrictions.",
  shoulder_loading_severity_not_supported: "Your current injury severity does not support this shoulder starter.",
  during_response_worse: "Your injury felt worse after rehab. Follow today’s guidance.",
  next_day_response_worse: "Your injury felt worse the next day. Follow today’s guidance.",
};

type RehabDecision = NonNullable<InjuryFlagRecord["rehab_decision"]>;

const SETBACK_CODES = new Set(["unresolved_injury_checkin_setback", "unresolved_historical_negative_response"]);
const DURING_CODES = new Set(["during_response_not_reported", "no_reviewed_response_group", "during_response_not_sure"]);
const NEXT_DAY_CODES = new Set(["next_day_response_not_yet_known", "next_day_response_not_reported", "next_day_response_not_sure"]);
const WORK_CODES = new Set(["no_reviewed_stage_exposure", "no_defined_dose_completed"]);
const DAY_FORMAT = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });

function formatDay(day: string | null | undefined): string | null {
  const date = day ? new Date(`${day}T12:00:00Z`) : null;
  return date && !Number.isNaN(date.getTime()) ? DAY_FORMAT.format(date).replace(",", "") : null;
}

function unmetCodes(decision: RehabDecision): string[] {
  const next = decision.progression?.next_transition;
  return [...(next?.reason_codes ?? []), ...(next?.requirements ?? [])
    .filter(item => !["pass", "not_applicable"].includes(item.status)).map(item => item.reason_code)];
}

/** One next step for rehab-response checks, matched to today's rehab state so the
 * card never says "log today's rehab" beside "Done for today". */
export function rehabResponseStep(decision: RehabDecision): string | null {
  const codes = unmetCodes(decision);
  const setback = codes.some(code => SETBACK_CODES.has(code));
  const needsDuring = codes.some(code => DURING_CODES.has(code));
  const needsNextDay = codes.some(code => NEXT_DAY_CODES.has(code));
  const needsWork = codes.some(code => WORK_CODES.has(code));
  if (!setback && !needsDuring && !needsNextDay && !needsWork) return null;
  const schedule = decision.schedule;
  const nextDay = formatDay(schedule?.next_due_day);
  switch (schedule?.state) {
    case "due":
      return "Do today’s rehab, then tap how it feels under How’s your injury.";
    case "already_completed":
      if (needsDuring) return "Rehab done. Tap how it feels under How’s your injury.";
      if (needsNextDay) return "Rehab done. Tomorrow, answer how it felt the day after.";
      return setback
        ? `Rehab before your last Better doesn’t count. ${nextDay ? `Next rehab ${nextDay}` : "Do your next rehab"}, then say how it felt.`
        : null;
    case "recovery_day":
    case "deferred":
      if (needsNextDay && !setback) return `Answer the day-after question on Today. Next rehab ${nextDay ?? "soon"}.`;
      return `Next rehab ${nextDay ?? "soon"}. After it, say how it felt.`;
    default:
      return null;
  }
}

function rehabSafetyHold(decision: RehabDecision): string | null {
  const next = decision.progression?.next_transition;
  // A next stage UNLXCK doesn't offer can't be "on hold": its checks don't apply.
  const checked = next?.target_stage_live === false ? undefined : next;
  const codes = [...(decision.progression?.reason_codes ?? []), ...decision.reason_codes,
    ...(checked?.reason_codes ?? []), ...(checked?.requirements ?? [])
      .filter(item => !["pass", "not_applicable"].includes(item.status)).map(item => item.reason_code)];
  const reason = codes.find(code => /worse|worsening|setback|negative|safety_hold|stopped|medical/.test(code) && REASONS[code]);
  if (reason && SETBACK_CODES.has(reason)) return rehabResponseStep(decision) ?? REASONS[reason];
  return reason ? REASONS[reason] : null;
}

/** Explain the engine's recorded entry route, never infer it from logged work. */
export function rehabEntryExplanation(decision: RehabDecision): string | null {
  const stage = decision.stage;
  if (decision.outcome !== "prescribed_rehab" || !stage || rehabSafetyHold(decision)) return null;
  if (stage === "load" && decision.progression?.entry_basis === "reported_clinician_permission") {
    return "LOAD follows your reported clinician advice. RESTORE completion isn’t needed.";
  }
  const index = STAGES.indexOf(stage);
  if (index > 0 && !decision.progression?.entry_basis && decision.progression?.reason_codes?.includes(
    `transition_met:${STAGES[index - 1]}->${stage}`)) {
    return `You meet the checks for this ${stage.toUpperCase()} exercise.`;
  }
  return null;
}

/** Presentation only. Availability and eligibility always come from the engine. */
export function rehabNextStep(decision: RehabDecision): string | null {
  const safety = rehabSafetyHold(decision);
  if (safety) return `Progression is on hold. ${safety}`;
  const next = decision.progression?.next_transition;
  if (!next) return null;
  const codes = [...next.reason_codes, ...(next.requirements ?? [])
    .filter(item => !["pass", "not_applicable"].includes(item.status)).map(item => item.reason_code)];
  // Never present logging/permission requirements as a way to publish content.
  if (!next.target_stage_live) {
    const unavailable = `${(LABELS[next.to_stage] ?? "Next-stage").toUpperCase()} exercises aren’t available for this injury in UNLXCK yet.`;
    return unavailable;
  }
  if (next.status === "closed") return "Progression is on hold for this injury. Follow today’s rehab and your limits.";
  if (next.status === "met") return "Follow the permitted exercise shown in Today.";
  const responseStep = rehabResponseStep(decision);
  if (responseStep) return responseStep;
  // Specific selector blockers take precedence over its general content message.
  const reason = ["target_equipment_unavailable", "rehabilitation_loading_not_reported"]
    .find(code => codes.includes(code)) ?? codes.find(code => REASONS[code]);
  return reason ? REASONS[reason] : "Follow today’s rehab. The next stage still needs checks.";
}

/** Availability and one next action stay visible; the row opens detailed checks. */
export function RehabProgressStatus({ decision, statusLine, summaryVisible = false }: {
  decision: RehabDecision;
  statusLine?: string | null;
  summaryVisible?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const stage = decision.stage;
  if (!stage || !STAGES.includes(stage)) return null;
  const next = decision.progression?.next_transition;
  const safety = rehabSafetyHold(decision);
  const entry = rehabEntryExplanation(decision);
  const unavailable = next && (!next.target_stage_live || next.status === "closed");
  // When the card header already shows the status sentence, this row is only
  // the way into the checks: one status line per card, never a second "on hold".
  const title = summaryVisible && next ? "Progression checks"
    : safety ? "Progression · On hold" : next ? `${LABELS[next.to_stage] ?? "Next stage"} · ${!next.target_stage_live ? "Unavailable in UNLXCK" : next.status === "met" ? "Available" : "On hold"}` : null;
  const nextStep = rehabNextStep(decision);
  // Checks blocked for the same reason show once.
  const checks = (next?.requirements ?? []).filter((item, index, items) =>
    item.status === "pass" || !REASONS[item.reason_code] || !items.slice(0, index).some(previous =>
      previous.status !== "pass" && REASONS[previous.reason_code] === REASONS[item.reason_code]));
  // One next action: the first unmet check, else the transition's own blocker.
  const nextAction = rehabResponseStep(decision) ?? [...checks.filter(item => item.status === "fail" || item.status === "unknown").map(item => item.reason_code),
    ...(next?.reason_codes ?? [])].map(code => REASONS[code]).find(Boolean) ?? null;
  if (summaryVisible && !next && !statusLine) return null;
  if (!title && !statusLine && !entry) return null;
  const content = <>
    <span className="injury-progress-main">
      {title ? <span className="injury-progress-title">{title}</span> : null}
      {!summaryVisible && nextStep ? <span className="injury-progress-status">{nextStep}</span> : null}
      {!next && entry ? <span className="injury-progress-status">{entry}</span> : null}
      {statusLine ? <span className="injury-progress-status injury-progress-schedule">{statusLine}</span> : null}
    </span>
    {next ? <InjuryChevron /> : null}
  </>;
  return <>
    {next ? <button type="button" className="injury-progress" aria-haspopup="dialog"
      aria-label={`${title}${nextStep ? `. ${nextStep}` : ""}${entry ? `. ${entry}` : ""}${statusLine ? `. ${statusLine}` : ""}. Progression requirements`}
      onClick={() => setOpen(true)}>{content}</button>
      : <div className="injury-progress">{content}</div>}
    {open && next ? <InjuryDetailSheet title="Progression requirements" onClose={() => setOpen(false)}>
      <div className="injury-sheet-body">
        <p className="injury-sheet-lead">{LABELS[stage]} → {LABELS[next.to_stage] ?? "Next stage"}
          {!unavailable && checks.length ? <small className="muted"> · {checks.filter(item => item.status !== "unknown" && item.status !== "fail").length} of {checks.length} done</small> : null}</p>
        {unavailable ? <p className="muted">{nextStep}</p>
          : nextAction ? <p className="injury-requirements-next"><strong>Next:</strong> {nextAction}</p> : null}
        {!unavailable ? <ul className="injury-requirements">{checks.map(item => <li key={item.requirement_id} data-status={item.status}>
          <div><strong><span aria-hidden="true" className="injury-requirement-mark">{item.status === "pass" ? "✓" : item.status === "not_applicable" ? "–" : "○"}</span>{REQUIREMENTS[item.kind] ?? "Progression check"}</strong>
            <span>{item.status === "pass" ? "Done" : item.status === "not_applicable" ? "Not needed" : item.status === "fail" ? "On hold" : "Waiting"}</span></div>
        </li>)}</ul> : null}
        {decision.recent_log?.length ? <div className="injury-recent-log">
          <p className="injury-sheet-caption">Recent</p>
          <ul>{decision.recent_log.map((entry, index) => <li key={`${entry.kind}-${entry.date}-${index}`}>
            <span className="muted">{formatDay(entry.date) ?? entry.date}</span>{" "}
            {entry.kind === "rehab"
              ? <>{entry.label} · Felt: {entry.felt} · Next day: {entry.next_day}{entry.counts === false
                ? <small className="muted"> · doesn’t count</small> : null}</>
              : <>Injury: {entry.label}</>}
          </li>)}</ul>
        </div> : null}
      </div>
    </InjuryDetailSheet> : null}
  </>;
}

/** The stage at a glance for an injury's summary row: "Restore", the
 * five-step meter, and where it goes next. Null when there is no stage. */
export function RehabStageMeter({ decision }: { decision: InjuryFlagRecord["rehab_decision"] }) {
  const stage = decision?.stage;
  if (!decision || !stage || !STAGES.includes(stage)) return null;
  const current = STAGES.indexOf(stage);
  const next = decision.progression?.next_transition;
  const entry = rehabEntryExplanation(decision);
  const nextStep = rehabNextStep(decision);
  return <><span className="injury-stage-meter">
    <span className="injury-stage-meter-steps" aria-hidden="true">
      {STAGES.map((value, index) => <span key={value} data-complete={index <= current}
        data-current={index === current || undefined} />)}
    </span>
    <span className="injury-stage-meter-text">
      <span className="sr-only">Current rehab stage: {LABELS[stage]}. CALM → RESTORE → LOAD → DYNAMIC → RETURN. Future stages depend on published exercises and eligibility. </span>
      {entry ? <span className="injury-stage-entry">{entry}</span> : null}
      {nextStep ?? (next ? null : `Stage ${current + 1} of ${STAGES.length} · Future availability varies by injury`)}
    </span>
  </span>
    <details className="injury-stage-guide">
      <summary>What do the stages mean?</summary>
      <dl>
        <div><dt>Calm</dt><dd>Protecting the area.</dd></div>
        <div><dt>Restore</dt><dd>Comfortable movement.</dd></div>
        <div><dt>Load</dt><dd>Strengthening.</dd></div>
        <div><dt>Dynamic</dt><dd>Faster, controlled movement.</dd></div>
        <div><dt>Return</dt><dd>Sport-specific rehab.</dd></div>
      </dl>
    </details>
  </>;
}

export function rehabStageLabel(decision: InjuryFlagRecord["rehab_decision"]): string | null {
  const stage = decision?.stage;
  return stage && STAGES.includes(stage) ? LABELS[stage] : null;
}
