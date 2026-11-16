"use client";

import { type TodayDecisionBanner, type TodayDecisionTier } from "@/lib/today";
import type { TodaySafetyCheck } from "@/lib/types";
import { stripInternalMarkers } from "@/lib/injury-display";

function sameGuidance(a?: string, b?: string): boolean {
  const normalize = (value?: string) => value?.trim().toLowerCase().replace(/\s+/g, " ").replace(/[.!]+$/, "");
  return Boolean(normalize(a)) && normalize(a) === normalize(b);
}

function isSupersededReadinessMessage(value?: string): boolean {
  return value?.trim() ===
    "Previous readiness guidance is superseded by the injury warning.";
}

/**
 * Current-day guidance above the session card. The same shell is used while a
 * session is live and after it is logged, when the card below previews the next
 * session. It never mutates the saved plan.
 *
 * The card answers the four questions an athlete actually has, in the order they
 * ask them:
 *
 *   DECISION    what should I do?      -> the tier headline and the action
 *   TRIGGER     why did it change?     -> what changed about the athlete
 *   CHECKED     what was assessed?     -> safety checks and their outcome
 *   CONTEXT     what influenced that?  -> the camp around the decision
 *   DECISION BASED ON                 -> which inputs were available
 *
 * Trigger and context are held apart because a flat list made "Fight week" a
 * peer of "High pain". Being in taper is a plan, not a symptom: it explains how
 * cautious the call is, and never that something is wrong. The backend does the
 * classification, so this panel only renders it.
 *
 * Triggers are contributors, never causes. The engine records which signals were
 * present when it decided; it does not establish that any one of them caused the
 * change, so the copy must not claim it did.
 *
 * The API keeps its confidence fields for compatibility. This panel describes
 * the evidence directly instead of translating data coverage into a confidence
 * claim the athlete could mistake for predictive certainty.
 */
export function TodayDecisionPanel({
  banner,
  tier,
  triggers,
  safetyChecks,
  context,
  sources,
  confidenceNote,
  compactPreview = false,
}: {
  banner: TodayDecisionBanner | null;
  tier?: TodayDecisionTier;
  triggers?: string[];
  /** Safety questions that were assessed, with their outcome — a stable skin
   * injury lands here rather than in the triggers, so "checked, no change"
   * never reads as "this reduced your session". Backend-classified. */
  safetyChecks?: TodaySafetyCheck[];
  context?: string[];
  sources?: string[];
  confidenceNote?: string;
  /** The next-session card below already names the exercise and explains its lock. */
  compactPreview?: boolean;
}) {
  if (!banner) {
    return null;
  }
  const isSafetyNotice = banner.displayState === "safety_notice";
  // Session timing cannot downgrade a current safety notice back to PREVIEW.
  // `tier` still describes session behavior; `displayState` owns this message.
  const isPreview = banner.displayState === "preview" ||
    (tier === "preview" && !isSafetyNotice);
  const isCompactPreview = banner.displayState === "preview" && compactPreview;
  const isCurrentGuidance = !isPreview;
  // Current-day readiness evidence cannot clear or restrict a future session.
  // Preview cards explain only which planned session their copy is framing.
  // A trigger that only names what the reason line already says ("Right
  // achilles tendonitis — restricts today's training" under "Your right
  // achilles tendonitis limits ...") is said once, up there.
  const detailText = (banner.detail ?? "").toLowerCase();
  const listedTriggers = clean(isPreview || isSafetyNotice ? undefined : triggers);
  const namedRestriction = listedTriggers.some(trigger => / — restricts today's training$/i.test(trigger));
  const triggerLabels = listedTriggers.filter((trigger) => {
    if (namedRestriction && trigger.trim().toLowerCase() === "current injury restrictions") return false;
    const [subject, effect] = trigger.split(" — ");
    if (!effect) return true;
    const name = subject.trim().toLowerCase();
    return !(name.length > 3 && detailText.includes(name));
  });
  const contextLabels = clean(isPreview || isSafetyNotice ? undefined : context);
  const safetyRows = (isPreview ? [] : (safetyChecks ?? [])).filter(
    (check, index, all) => check.code?.trim() && check.label?.trim() &&
      check.result_label?.trim() && SAFETY_RESULTS.has(check.result) &&
      // Incomplete payloads prove nothing. If duplicate evidence conflicts,
      // keep the restriction and omit the clearance rather than showing both.
      !(check.result === "no_session_change" && all.some(
        (other) => other.code === check.code && other.result !== check.result,
      )) && !all.slice(0, index).some(
        (other) => other.code === check.code && other.result === check.result,
      ),
  ).filter((check) => !isSafetyNotice || check.code === "surface_injury");
  const usedSources = clean(
    isCompactPreview
      ? []
      : isPreview
        ? ["next planned session"]
      : isSafetyNotice
        ? ["your tracked injuries"]
        : sources,
  );
  // Only structured engine results can describe an assessed check. Contributor
  // labels and source coverage are not a complete record of check-in answers.
  const checks: CheckedRow[] = [
    ...safetyRows.map((check) => ({
      code: check.code,
      label: check.label,
      result_label: check.result_label,
      clear: check.result === "no_session_change",
    })),
  ];
  const note = isPreview || isSafetyNotice ? "" : (confidenceNote ?? "").trim();
  const hasEvidence =
    triggerLabels.length > 0 ||
    checks.length > 0 ||
    contextLabels.length > 0 ||
    usedSources.length > 0 ||
    Boolean(note);
  const evidenceCount =
    Number(triggerLabels.length > 0) +
    Number(checks.length > 0) +
    Number(contextLabels.length > 0) +
    Number(usedSources.length > 0 || Boolean(note));
  const directive = splitDirective(banner.action);
  // The reasons open by themselves only when they decide whether to train at
  // all (a red day, a safety notice); an adjusted day keeps them a tap away.
  const openEvidence = isCurrentGuidance && (isSafetyNotice || banner.tone === "red");
  return (
    <div
      id="today-decision"
      className="today-decision-banner"
      data-state={banner.displayState}
      data-tone={banner.tone}
      data-compact={isCompactPreview || undefined}
      data-current-guidance={isCurrentGuidance || undefined}
      role="status"
    >
      <div className="today-decision-command">
        <div className="today-decision-heading">
          {isCurrentGuidance ? <span className="today-decision-scope">TODAY&apos;S GUIDANCE</span> : null}
          <span className="today-decision-icon" aria-hidden="true">
            {banner.chip}
          </span>
        </div>
        {directive ? (
          <p className="today-decision-action">
            {directive.lead}
            {directive.rest ? <span className="today-decision-action-rest">{directive.rest}</span> : null}
          </p>
        ) : null}
        {!isCompactPreview && !sameGuidance(banner.detail, banner.action) ? <p className="today-decision-detail">{banner.detail}</p> : null}
        {banner.safety && !isSupersededReadinessMessage(banner.safety) && !sameGuidance(banner.safety, banner.action) && (isCompactPreview || !sameGuidance(banner.safety, banner.detail)) ? (
          <p className="today-decision-safety">{banner.safety}</p>
        ) : null}
      </div>
      {hasEvidence ? (
        <details className="today-decision-disclosure" open={openEvidence}>
          <summary>{isSafetyNotice ? "Why this message?" : "Why this decision?"}</summary>
        <dl className="today-decision-evidence" data-evidence-count={evidenceCount}>
          {triggerLabels.length ? (
            <div className="today-decision-row">
              <dt>Trigger</dt>
              <dd>
                <ul className="today-decision-values">
                  {triggerLabels.map((trigger) => (
                    <li key={trigger}>{trigger}</li>
                  ))}
                </ul>
              </dd>
            </div>
          ) : null}
          {checks.length ? (
            <div className="today-decision-row">
              <dt>Checked</dt>
              <dd>
                <ul className="today-decision-values">
                  {checks.map((check) => (
                    <li key={check.code} data-result={check.clear ? "clear" : "flag"}>
                      {`${check.label}: ${check.result_label}`}
                    </li>
                  ))}
                </ul>
              </dd>
            </div>
          ) : null}
          {contextLabels.length ? (
            <div className="today-decision-row">
              <dt>Context</dt>
              <dd>
                <ul className="today-decision-values">
                  {contextLabels.map((contextLabel) => (
                    <li key={contextLabel}>{contextLabel}</li>
                  ))}
                </ul>
              </dd>
            </div>
          ) : null}
          {usedSources.length || note ? (
            <div className="today-decision-row">
              <dt>{isSafetyNotice ? "Message based on" : "Decision based on"}</dt>
              <dd>
                {usedSources.length ? (
                  <ul className="today-decision-inputs">
                    {usedSources.map((source) => (
                      <li key={source}>{source}</li>
                    ))}
                  </ul>
                ) : null}
                {note ? <span className="today-decision-gap">{note}</span> : null}
              </dd>
            </div>
          ) : null}
        </dl>
        </details>
      ) : null}
    </div>
  );
}

type CheckedRow = { code: string; label: string; result_label: string; clear: boolean };

const SAFETY_RESULTS = new Set<TodaySafetyCheck["result"]>([
  "no_session_change", "local_protection_only", "no_contact",
  "direct_impact_removed", "multiple_restrictions", "medical_review",
]);

/** "Keep it controlled: skip sparring, …" reads as a short title over the
 * specifics. Only a short lead splits; anything else stays one sentence. */
function splitDirective(action: string | undefined): { lead: string; rest: string } | null {
  const text = (action ?? "").trim();
  if (!text) return null;
  const match = /^([^:]{3,40}):\s+(.+)$/.exec(text);
  if (!match) return { lead: text, rest: "" };
  const rest = match[2].charAt(0).toUpperCase() + match[2].slice(1);
  return { lead: `${match[1]}.`, rest };
}

function clean(values: string[] | undefined): string[] {
  // Defensive: an internal injury marker must never reach the athlete.
  return (values ?? []).map(stripInternalMarkers).filter((value) => value.trim());
}
