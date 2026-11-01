"use client";

import { createContext, useContext, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import Link from "next/link";
import { ExerciseGlyphIcon, type ExerciseGlyphName } from "./exercise-glyph";
import { getRehabExerciseGuide, rehabExerciseTarget } from "@/lib/rehab-exercise-guide";

import {
  compactTimeUnits,
  readableExerciseMetrics,
  classifySessionlessDay,
  isZeroLoadSupportSession,
  isCompactPlanSupportSession,
  mentalSessionCategory,
  mentalSessionSummary,
  mentalTaskSteps,
  normalizeForDedupe,
  cleanText,
  formatBlockLoad,
  formatCountdownLabel,
  formatClusterSummary,
  formatEffort,
  formatMeasured,
  formatRestNote,
  getBlockComponents,
  getBlocks,
  getBlockExecutionDisplay,
  getBlockAdjustmentDisplay,
  getActiveNotesExcludingRedFlags,
  getCoachLedContactView,
  getReducedContactReason,
  getDays,
  getDisplayableRedFlags,
  getFallbackSafetyNotes,
  getPriorityMicrodose,
  getRehabOrMobilityBlocks,
  isDeEmphasisedWeightCutSafety,
  planNoteLabel,
  formatSessionObjective,
  getSessionCoachingLines,
  getSessions,
  getStringList,
  getWeeks,
  redFlagView,
  resolvedWeekPhase,
  selectBlockMetric,
  shouldShowRest,
  type SessionlessDayKind,
} from "@/lib/structured-plan";
import {
  buildCompletionIndex,
  canRetroLog,
  completionForSession,
  completionSessionId,
  dayCompletion,
  getSessionDisplayStatus,
  resolveNextPlanFocusDay,
  resolveOpenPlanWeekNumber,
  resolvePlanProgress,
  weekCompletion,
  weekSessionSummary,
  type Completion,
  type CompletionIndex,
  type SessionDisplayStatus,
} from "@/lib/camp-map";
import {
  openBlockWeekDirective,
  openBlockWeekIntent,
  type OpenBlockWeekIntent,
} from "@/lib/open-block";
import {
  isRehabBlock,
  isCalmRehabBlock,
  resolveBlockRehabLabel,
  resolveSessionTypeLabel,
  resolveRehabSummaryLabel,
} from "@/lib/rehab-label";
import { useTrainingDay } from "@/lib/use-training-day";
import { describeRelativeDay, formatAppDate } from "@/lib/date-format";
import { buildPlanCalendar, calendarCaption, CALENDAR_WEEKDAYS } from "@/lib/plan-calendar";
import { resolveFiniteWeekNumber } from "@/lib/plan-format";
import { PlanCalendar } from "@/components/plan-calendar";
import {
  DECLARED_LIGHT_COMBAT_TITLE,
  formatPlanLabel,
  humanizeIfRawEnum,
  isDeclaredLightCombatTitle,
  stripLeadingWeekday,
} from "@/lib/plan-labels";
import { GlossaryTooltip } from "@/components/glossary-tooltip";
import { glossaryEntry } from "@/lib/glossary";
import { WhyTooltip } from "@/components/why-tooltip";
import { ExerciseDemo, ExerciseMediaProvider, useExerciseMedia } from "@/components/exercise-demo";
import { RehabExerciseGuide, RehabExerciseLimits, RehabPrescription } from "@/components/rehab-prescription";
import { PrivateExerciseMediaProvider, PrescribedPrivateVideo, PrescribedPrivateVideoThumbnail } from "@/components/private-exercise-video";
import { ExerciseLogBadge, ExerciseLogPanel, ExerciseLogTick, useExerciseLoggingActive } from "@/components/exercise-log";
import { ExerciseActions, ExerciseSessionScope, SessionChangesFoot } from "@/components/exercise-customisation";
import { demoThumbnailUrl } from "@/lib/exercise-demo";
import { SafetyNote } from "@/components/safety-note";
import { InjuryDetailSheet } from "@/components/today/injury-detail-sheet";
import { PLAN_SAFETY_NOTE } from "@/lib/safety-copy";
import {
  applySourceSetRange,
  getSourcePrescriptionRangeOverrides,
  getSourceExactVolumeOverride,
  getSourceStopRuleOverride,
  stripSafetyOwnedClause,
  stripSafetyOwnedStopRules,
} from "@/lib/block-display-guardrails";
import type {
  ExerciseMedia,
  MindsetAnchor,
  PlanScheduleContext,
  RehabLabelPolicy,
  StructuredBlock,
  StructuredDay,
  StructuredPlan,
  StructuredSession,
  StructuredWeek,
  TodaySessionCompletionRecord,
} from "@/lib/types";

/** Live logging info CampDayCard resolves per session and hands to SessionCard:
 * the display status (tone + label), the stored row (for RPE/reason lines) and
 * the retro-log hook when the session is still loggable. */
export type SessionCompletionInfo = {
  display: SessionDisplayStatus;
  completion?: TodaySessionCompletionRecord;
  onLog?: () => void;
};

const titleize = formatPlanLabel;

const DECLARED_LIGHT_COMBAT_DESCRIPTION =
  "Pads, drills, movement or other lower-intensity combat work.";

function PriorityMicrodoseCard({ day }: { day: StructuredDay }) {
  const dose = getPriorityMicrodose(day);
  if (!dose) {
    return null;
  }
  return (
    <div className="sp-priority-microdose">
      <p className="sp-eyebrow">{titleize(dose.goal)} microdose</p>
      <p className="sp-priority-microdose-name">{dose.name}</p>
      <p className="sp-priority-microdose-prescription">{dose.prescription}</p>
    </div>
  );
}

// A sessionless contact day carries no app S&C. The note must match the day kind.
const HARD_SPARRING_SESSIONLESS_NOTE =
  "No extra S&C. This is your declared hard-sparring/contact day. Stay fresh.";
const TECHNICAL_COMBAT_TITLE = "Technical Combat";
function contactDisplayTitle(title: string): string {
  const plain = title.replace(/^D[-−]\d+\s*[—–:-]\s*/i, "");
  return /^Technical[ -]only combat$/i.test(plain) ? TECHNICAL_COMBAT_TITLE : plain;
}
const TECHNICAL_COMBAT_HELP =
  "Hard sparring is reduced near fight day to cut fatigue and injury risk while keeping you sharp.";
const TECHNICAL_COMBAT_TAG = "Low load";
const TECHNICAL_COMBAT_RATIONALE =
  "Technical only, no hard sparring. Stay sharp and leave fresh.";
const TECHNICAL_COMBAT_STAGE_RATIONALES: Record<string, string> = {
  "Controlled fight-speed technical rounds":
    "Realistic exchanges at speed, controlled contact, low total volume.",
  "Technical rhythm only":
    "Light technical rounds. Prioritise timing, flow and clean execution.",
  "Technical touch — pads / shadow":
    "Pads or shadow only. Stay sharp without contact fatigue.",
  "Technical activation — no contact":
    "Brief movement and reactions. Finish feeling fresher than you started.",
};

// Coach-owned contact that coexists with app work on the same day, keyed the same
// way so a technical-only contact block never reads as hard sparring.
const HARD_SPARRING_CONTACT_NOTE =
  "Your declared hard-sparring/contact work, plus the app work below. Stay fresh.";

function sparringHeading(title: string): { title: string; caution: string | null } {
  const match = title.match(/^(.*\bhard sparring)\s*(?:—|–|-|\/)\s*controlled hard contact$/i);
  return match
    ? { title: match[1].trim(), caution: "Controlled hard contact" }
    : { title, caution: null };
}

function SparringCaution({ text }: { text: string | null }) {
  return text ? <p className="sp-sparring-caution"><span aria-hidden="true">⚠️</span> {text}</p> : null;
}

/** The red-edged coach-day box shared by Today, the calendar and the list. */
function CoachDayBox({ tag, tooltip, heading, children }: { tag: string | null; tooltip?: ReactNode; heading?: ReactNode; children: ReactNode }) {
  return (
    <div className="cm-day-context cm-day-context-contact">
      <div className="cm-light-technical cm-coach-led-contact">
        {tag || tooltip || heading ? (
          <div className="cm-light-technical-head">
            {tag ? <span className="sp-tag sp-accent">{tag}</span> : null}
            {heading}
            {tooltip}
          </div>
        ) : null}
        {children}
      </div>
    </div>
  );
}

function blockCountLabel(count: number): string {
  return `${count} block${count === 1 ? "" : "s"}`;
}

function blockSummaryMetric(block: StructuredBlock): string | null {
  const metric = selectBlockMetric(block)[0]?.value;
  return metric || formatBlockLoad(block.load) || formatMeasured(block.duration);
}

function CollapsibleSection({
  title,
  detailLabel,
  defaultOpen,
  syncKey,
  className,
  children,
}: {
  title: string;
  detailLabel?: string;
  defaultOpen?: boolean;
  /** When provided and it CHANGES, the open state re-syncs to `defaultOpen`.
   * Used to make the support phase follow the selected week: a new week supplies
   * a new syncKey, so the matching phase opens and the others close, while a
   * manual toggle is preserved until the next week change. Omit to keep the
   * mount-only `defaultOpen` behaviour. */
  syncKey?: string | null;
  className?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState<boolean>(Boolean(defaultOpen));
  // Reset-on-key-change during render (React's recommended pattern): re-open to
  // the new default when the sync key changes, without an effect or a flash.
  const [prevSyncKey, setPrevSyncKey] = useState<string | null | undefined>(syncKey);
  if (syncKey !== undefined && syncKey !== prevSyncKey) {
    setPrevSyncKey(syncKey);
    setOpen(Boolean(defaultOpen));
  }
  const actionTarget = detailLabel || title;
  return (
    <details
      className={`sp-collapse${className ? ` ${className}` : ""}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="sp-collapse-summary">
        <span className="sp-collapse-title">{title}</span>
        <span className="sp-collapse-action">{open ? "Hide" : "Show"} {actionTarget}</span>
      </summary>
      <div className="sp-collapse-body">{children}</div>
    </details>
  );
}

/**
 * Keep the saved planning rationale intact, but remove engine shorthand from
 * the athlete-facing card. Phase labels and timing rules are useful to the
 * generator and admin audit trail; an athlete needs the concise action or
 * rationale that remains after them.
 */
function athleteFacingRationale(value: string | null | undefined): string | null {
  const raw = cleanText(value);
  if (!raw) {
    return null;
  }

  const simplified = raw
    .replace(/^(?:GPP|SPP)(?:\s+|:\s*|[\u2014\u2013-]\s*)/i, "")
    .replace(
      /(?:^|\s)(?:from\s+)?D-\d+\+?\s*(?:onward|and beyond|or later|or closer)\b[^.!?]*(?:[.!?]|$)/gi,
      " ",
    )
    .replace(/\s+for\s+(?:a|an)\s+(?:[a-z-]+\s+){0,2}(?:brawler|boxer|fighter)\.?\s*$/i, ".")
    .replace(/\s{2,}/g, " ")
    .replace(/^[,;:\s]+/, "")
    .trim();

  return simplified ? simplified.charAt(0).toUpperCase() + simplified.slice(1) : null;
}

function TechnicalCombatRationale({ title }: { title?: string }) {
  const rationale =
    (title ? TECHNICAL_COMBAT_STAGE_RATIONALES[title] : undefined) || TECHNICAL_COMBAT_RATIONALE;
  return <p className="sp-today-note">{rationale}</p>;
}

function TechnicalCombatWhyTooltip({ day, declared = false }: {
  day?: StructuredDay;
  declared?: boolean;
}) {
  const anchor = day?.today_card?.mindset_anchor;
  const focus = cleanText(anchor?.focus_cue);
  const reset = cleanText(anchor?.reset_cue);
  const body = [
    declared ? DECLARED_LIGHT_COMBAT_DESCRIPTION : TECHNICAL_COMBAT_HELP,
    focus ? `Focus: ${focus}` : null,
    reset ? `Reset: ${reset}` : null,
  ].filter(Boolean).join("\n");
  return <WhyTooltip className="info-tooltip"
    title={declared ? DECLARED_LIGHT_COMBAT_TITLE : TECHNICAL_COMBAT_TITLE}
    ariaLabel={declared ? "About your declared technical session" : undefined}
    body={body} triggerLabel="i" />;
}

export function MindsetAnchorCard({
  anchor,
  dedupeContext,
  contextOnly = false,
}: {
  anchor?: MindsetAnchor | null;
  contextOnly?: boolean;
  /** Text already shown next to this card (the session objective or day
   * description). A Context line that only restates it is dropped so the same
   * sentence never prints twice on one card. */
  dedupeContext?: string | (string | null | undefined)[] | null;
}) {
  const sourceLines = getSessionCoachingLines(anchor);
  const lead = sourceLines.find(line => line.label === "Focus")
    || sourceLines.find(line => line.label === "Coach cue")
    || sourceLines.find(line => line.label === "Reset");
  const lines = [
    ...(!contextOnly && lead ? [{ label: "Coach cue", value: lead.value }] : []),
    ...sourceLines.filter(line => line.label === "Context"),
  ].flatMap(line => {
    const value = line.label === "Context" ? athleteFacingRationale(line.value) : line.value;
    return value ? [{ ...line, value }] : [];
  });
  const dedupeTargets = new Set(
    (Array.isArray(dedupeContext) ? dedupeContext : [dedupeContext])
      .map(normalizeForDedupe)
      .filter((text): text is string => text !== null),
  );
  const shown =
    dedupeTargets.size === 0
      ? lines
      : lines.filter(
          (line) => !(line.label === "Context" && dedupeTargets.has(normalizeForDedupe(line.value)!)),
        );
  if (shown.length === 0) {
    return null;
  }
  if (contextOnly) {
    return <>{shown.map(line => <p key={line.label} className="sp-muted sp-session-constraint">{line.value}</p>)}</>;
  }
  const renderLine = (line: { label: string; value: string }) => (
    <li key={line.label}>
      <span className="sp-coaching-label">{line.label}</span>
      <span>{line.value}</span>
    </li>
  );
  return (
    <div className="sp-coaching">
      <ul className="sp-coaching-list">{shown.map(renderLine)}</ul>
    </div>
  );
}

// Once the injury that seeded a plan's rehab work has cleared, that work is no
// longer rehabilitation — it is prophylactic (prehab). The decision is made per
// body region (see web/lib/rehab-label.ts) from the server-derived policy, which
// lives at the top of the render tree; this context relays it to the leaf cards
// that print the tag without drilling it through every intermediate day/session
// component. Null (no provider) keeps the unchanged "Rehab" wording.
const RehabLabelContext = createContext<RehabLabelPolicy | null>(null);
const PlanSourceTextContext = createContext<string | null>(null);
// "Builds" and "Why today" are for the day's session, not for browsing the whole
// plan: only the Today screen turns them on for its exercise rows.
const ExerciseRationaleContext = createContext(false);

/** Shows each exercise's "Builds" / "Why today" lines inside it (Today only). */
export function ExerciseRationaleProvider({ children }: { children: ReactNode }) {
  return <ExerciseRationaleContext.Provider value>{children}</ExerciseRationaleContext.Provider>;
}
const PlanSafetyTextContext = createContext<string[]>([]);

/** Relays the Rehab/Prehab policy to the block cards below it. Standalone
 * surfaces that render SessionCard directly (Today) mount this themselves;
 * without it every rehab block keeps reading "Rehab". */
export function RehabLabelProvider({
  policy,
  children,
}: {
  policy?: RehabLabelPolicy | null;
  children: ReactNode;
}) {
  return <RehabLabelContext.Provider value={policy ?? null}>{children}</RehabLabelContext.Provider>;
}

/** Tag label for a block: "Rehab"/"Prehab" per live injury region, else the raw type. */
function blockTagLabel(block: StructuredBlock, policy: RehabLabelPolicy | null): string {
  if (isRehabBlock(block)) {
    return resolveBlockRehabLabel(block, policy);
  }
  return titleize(cleanText(block.block_type));
}

/**
 * The athlete-facing prescription for one block, reconciled against the exact
 * source plan text (ranges, rest, effort) and with stop criteria the central
 * Safety Priority card owns stripped. Shared by the exercise row header and the
 * full block card so both always show the same dose and the same stop rule.
 */
function useBlockPrescription(block: StructuredBlock, sourceCountdown?: string | null) {
  const sourceText = useContext(PlanSourceTextContext);
  const planSafetyTexts = useContext(PlanSafetyTextContext);
  const title = cleanText(block.display_name) || "Block";
  const sourceOverrides = getSourcePrescriptionRangeOverrides(sourceText, title, sourceCountdown);
  // Loads are authored lower-case ("bodyweight", "easy angle"); they sit as a
  // value beside "RPE 4", so read them sentence-case like the other values.
  const loadText = formatBlockLoad(block.load)
    || (block.prescription_source === "athlete" ? cleanText(block.intensity) : null);
  const load = loadText ? loadText.charAt(0).toUpperCase() + loadText.slice(1) : null;
  // Times read short ("2 min", "90 sec") so a row of stats fits a phone; the
  // value itself never changes (see compactTimeUnits).
  // An AI card can omit the quantitative dose even when its saved final text
  // states it exactly. Recover only a uniquely matched scalar source line.
  // Do not replace authored metrics or athlete-edited prescriptions.
  const sourceVolume = block.prescription_source !== "athlete"
    && !block.athlete_change
    && block.sets == null && block.reps == null
    && block.rounds == null && block.work == null && block.duration == null
    && block.distance == null
    ? getSourceExactVolumeOverride(sourceText, title, sourceCountdown)
    : null;
  const metrics = applySourceSetRange(selectBlockMetric(sourceVolume ? { ...block, ...sourceVolume } : block), sourceOverrides.sets).map((metric) => ({
    ...metric,
    value: compactTimeUnits(metric.value),
  }));
  const restText = sourceOverrides.rest || (shouldShowRest(block.rest) ? formatMeasured(block.rest) : null)
    || formatRestNote(block);
  const rest = restText ? compactTimeUnits(restText) : null;
  const effort = sourceOverrides.effort || formatEffort(block);
  const { progression, stopRules } = getBlockAdjustmentDisplay(block);
  const sourceStopRule = getSourceStopRuleOverride(sourceText, title, sourceCountdown);
  // Drop stop-rule criteria that only restate injury-safety escalation the
  // Safety Priority card already owns, so the block keeps its own criterion
  // (technique/form/speed) instead of duplicating the centralised red flag.
  const sourceStopRules = sourceStopRule
    ? stripSafetyOwnedStopRules([sourceStopRule], planSafetyTexts)
    : [];
  // Prefer the exact raw Stop field. If centralised safety owns that complete
  // compound field, fall back to its structured fragments so a distinct
  // technique/form criterion remains visible instead of disappearing with it.
  const blockStopRules =
    sourceStopRules.length > 0
      ? sourceStopRules
      : stripSafetyOwnedStopRules(stopRules, planSafetyTexts);
  const compactStopRule =
    blockStopRules
      .map((rule) => rule.trim().replace(/^stop(?:\s+rule)?\s*:\s*/i, "").trim())
      .filter(Boolean)
      .join("; ") || null;
  return { title, load, metrics, rest, effort, compactStopRule, progression, sourceVolume };
}

export function BlockCard({
  block,
  openWeekIntent,
  sourceCountdown,
  embedded = false,
  hideLeadCue = false,
  hideCues = false,
  hideStopRule = false,
  hideAdjustments = false,
  hideInstructions = false,
}: {
  block: StructuredBlock;
  /** Development-block intent for an explicitly prescribed deload instruction.
   * The validated rule remains visible once; dated camps pass no week intent. */
  openWeekIntent?: OpenBlockWeekIntent | null;
  sourceCountdown?: string | null;
  /** Rendered inside an ExerciseRow, whose header already names the exercise. */
  embedded?: boolean;
  /** The demo pins the first cue under the video; skip it in the list. */
  hideLeadCue?: boolean;
  /** The exercise sheet shows cues and the stop rule in their own places. */
  hideCues?: boolean;
  hideStopRule?: boolean;
  /** The detail sheet already displays Swaps/Easier/Progress/Stop. */
  hideAdjustments?: boolean;
  hideInstructions?: boolean;
}) {
  const rehabLabelPolicy = useContext(RehabLabelContext);
  const { title, load, metrics, rest, effort, compactStopRule, progression, sourceVolume } = useBlockPrescription(
    block,
    sourceCountdown,
  );
  const blockType = cleanText(block.block_type);
  // The effort card is glossed from the METHOD, not from the word "Effort":
  // EffortMethod covers RPE, RIR, intent, velocity, heart_rate_zone, pace and
  // max_effort_percent, so a fixed RPE definition would mis-explain "Max intent"
  // as a 1-10 perceived-exertion score. An unrecognised or missing method falls
  // through to no tooltip rather than to a guess.
  const effortMethod = cleanText(block.effort?.method);
  const execution = getBlockExecutionDisplay(block);
  const { cues: allCues, regressions, substitutions } = execution;
  const prescriptionText = cleanText(block.prescription_text)
    || (metrics.length === 0 ? execution.prescription : null);
  // The demo can pin the lead cue; every remaining authored detail still belongs here.
  const visibleCues = hideCues ? [] : allCues.slice(hideLeadCue ? 1 : 0)
    .filter(cue => !sourceVolume || !/^\s*\d+\s*[-–—]\s*\d+\s+(?:controlled\s+)?sets?\b/i.test(cue))
    .map(compactCoachLine);
  const directive = openBlockWeekDirective(openWeekIntent, block);
  // Display the validated AI instruction once; deload retains its own directive.
  const weekDirective = directive?.usesProgressionRule ? null : directive;
  const adjustmentRules = [
    ...(progression && openWeekIntent?.key !== "deload" ? [{ label: "Progress" as const, text: progression }] : []),
    ...(compactStopRule && !hideStopRule
      ? [{ label: "Stop rule" as const, text: compactStopRule }]
      : []),
  ];

  const tagLabel = blockType ? blockTagLabel(block, rehabLabelPolicy) : null;
  // A cluster's load belongs to its heavy movement, so it is shown on that
  // movement's row rather than as a block-wide Load stat.
  const components = getBlockComponents(block);
  const blockLoad = components.length > 0 ? null : load;
  const statCount = metrics.length + [blockLoad, rest, effort].filter(Boolean).length;
  const hasAthleteMetrics = block.prescription_source === "athlete"
    && typeof block.athlete_dose?.mode === "string"
    && ["reps", "duration", "intervals", "distance"].includes(block.athlete_dose.mode);
  const showMetricTiles = statCount > 0 && (!block.prescription_text || hasAthleteMetrics);
  const asides = hideAdjustments ? [] : [
    ...(substitutions.length > 0 ? [{ label: "Swaps", text: substitutions[0], glossary: false }] : []),
    ...regressions.map(text => ({ label: "Easier", text: compactCoachLine(text), glossary: false })),
    ...adjustmentRules.map((rule) => ({ ...rule, glossary: true })),
  ];

  return (
    <div className={embedded ? "sp-block sp-block-embedded" : "sp-block"}>
      {/* Embedded, the exercise row's header already carries the name and type. */}
      {embedded ? null : (
        <div className="sp-block-head">
          <span className="sp-block-title">{title}</span>
          {tagLabel ? (
            <span className="sp-tag">
              {tagLabel}
              {/* Silent for ordinary types (Strength, Conditioning); only the
                  Rehab/Prehab/Mobility tags carry a definition. */}
              <GlossaryTooltip term={tagLabel} />
            </span>
          ) : null}
        </div>
      )}
      {!hideInstructions ? <ExecutionInstructions block={block} /> : null}
      {prescriptionText && !showMetricTiles ? <p className="sp-block-purpose">{prescriptionText}</p> : null}
      {block.notes && !hasAthleteMetrics ? <p className="sp-block-purpose">{block.notes}</p> : null}
      <RehabPrescription block={block} />
      {/* Athlete entries retain their exact text (tempo, side, free-form load)
          alongside the same metric tiles used by generated sessions. */}
      {showMetricTiles ? (
        <div className="sp-block-stats" data-count={statCount}>
          {metrics.map((metric) => (
            <span key={metric.label} className="sp-stat">
              <span className="sp-stat-head">
                <span className="sp-stat-label">{metric.label}</span>
                {/* Volume and Mode are glossed; Duration/Distance/Rounds/Work
                    are plain English and stay unadorned. */}
                <GlossaryTooltip term={metric.label} />
              </span>
              {metric.value}
            </span>
          ))}
          {blockLoad ? (
            <span className="sp-stat">
              <span className="sp-stat-head">
                <span className="sp-stat-label">Load</span>
                <GlossaryTooltip term="Load" />
              </span>
              {blockLoad}
            </span>
          ) : null}
          {rest ? (
            <span className="sp-stat">
              <span className="sp-stat-label">Rest</span>
              {rest}
            </span>
          ) : null}
          {effort ? (
            <span className="sp-stat">
              <span className="sp-stat-head">
                <span className="sp-stat-label">Effort</span>
                {/* The value reads "RPE 1.5" / "Max speed" with no scale
                    attached, so the gloss explains whichever scale is in play. */}
                <GlossaryTooltip term={effortMethod} />
              </span>
              {effort}
            </span>
          ) : null}
        </div>
      ) : null}
      {components.length > 0 ? (
        <ol className="sp-cluster" aria-label="Movements each round">
          {components.map((component) => (
            <li key={`${component.label}:${component.name}`} className="sp-cluster-row">
              <span className="sp-cluster-label">{component.label}</span>
              <span className="sp-cluster-name">{component.name}</span>
              {component.detail ? <span className="sp-cluster-detail">{component.detail}</span> : null}
            </li>
          ))}
        </ol>
      ) : null}
      {prescriptionText && showMetricTiles ? <p className="sp-block-purpose">{prescriptionText}</p> : null}
      {block.notes && hasAthleteMetrics ? <p className="sp-block-purpose">{block.notes}</p> : null}
      {weekDirective ? (
        <p className="sp-block-aside sp-week-directive">
          <span className="sp-stat-label">{weekDirective.label}</span>
          {weekDirective.text}
        </p>
      ) : null}
      {visibleCues.length > 0 ? (
        <ul className="sp-cues">
          {visibleCues.map((cue, index) => (
            <li key={`${cue}-${index}`}>{cue}</li>
          ))}
        </ul>
      ) : null}
      {asides.length > 0 ? (
        <div className="sp-block-asides">
          {asides.map((aside) => (
            <p
              key={`${aside.label}:${aside.text}`}
              className="sp-block-aside"
              data-kind={aside.label === "Stop rule" ? "stop" : undefined}
            >
              <span className="sp-stat-label">{aside.label}</span>
              {aside.glossary ? <GlossaryTooltip term={aside.label} /> : null}
              {aside.text}
            </p>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** One compact exercise-list stat line, not a full prescription paragraph. */
function exerciseRowSummary(
  block: StructuredBlock,
  prescription: { metrics: { label: string; value: string }[]; load: string | null; effort: string | null; rest: string | null },
  compact = false,
): string | null {
  if (compact && block.block_type === "rehab" && block.drill_snapshot) {
    const guide = getRehabExerciseGuide(block);
    return guide.selfPaced ? "Self-paced · follow prescribed instructions"
      : guide.metrics.map(metric => `${metric.value} ${metric.label.toLowerCase()}`).join(" · ") || null;
  }
  const cluster = formatClusterSummary(block);
  if (cluster) return cluster;
  if (compact && prescription.metrics.length > 0) return readableExerciseMetrics(prescription);
  if (block.prescription_text) {
    const full = block.prescription_text.trim();
    if (full.length <= 72) return full;
    const duration = full.match(/\b\d+(?:\.\d+)?\s*(?:minutes?|mins?|seconds?|secs?)\b/i)?.[0];
    const effort = full.match(/\bRPE\s*\d+(?:\.\d+)?\b/i)?.[0];
    // The full instructions remain in the expanded card; never use a whole
    // paragraph as a tiny exercise-list subtitle.
    return [duration ? compactTimeUnits(duration) : null, effort]
      .filter(Boolean).join(" · ") || "See prescription";
  }
  if (compact) {
    return readableExerciseMetrics(prescription) || getBlockExecutionDisplay(block).prescription;
  }
  const headline =
    prescription.metrics[0]?.value || getBlockExecutionDisplay(block).prescription
    || prescription.load || formatMeasured(block.duration);
  const parts = [headline, prescription.effort].filter((part): part is string => Boolean(part));
  return parts.length > 0 ? Array.from(new Set(parts)).join(" · ") : null;
}

/** Saved copy may be long. Never drop a condition to hit a word count. */
function compactCoachLine(value: string): string {
  return value.trim().replace(/^(?:Cue|Easier|Progress|Stop):\s*/i, "");
}

// A "How" line that only restates the dose ("6 x 20 sec work", "1x5s with
// 120s rest", "30min continuous") repeats the stats; it explains nothing.
const DOSE_ONLY_WORD_RE =
  /^(?:\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)?(?:s|secs?|seconds?|mins?|minutes?|m|kg|lbs?|reps?|sets?|%)?|x|×|@|rpe|rir|sets?|reps?|rounds?|work|rest|reset|recovery|between|with|of|and|per|each|side|technical|continuous|easy|low|max|total|s|secs?|seconds?|mins?|minutes?)$/i;

function isDoseOnlyInstruction(text: string): boolean {
  const words = text.toLowerCase().split(/[\s,;|/()+.:]+/).filter(Boolean);
  return words.length > 0 && words.some(word => /\d/.test(word)) && words.every(word => DOSE_ONLY_WORD_RE.test(word));
}

function ExecutionInstructions({ block }: { block: StructuredBlock }) {
  const media = useExerciseMedia(block.display_name, block.exercise_key);
  const hasVideo = (block.prescription_source !== "athlete" || block.exercise_key) && media;
  if (hasVideo || block.private_media_id || block.community_revision_id) return null;
  const raw = cleanText(block.execution_instructions);
  // Keep only lines that describe the movement, not a restated dose.
  const instructions = raw
    ? raw.split(/\n+/).map(line => line.trim()).filter(line => line && !isDoseOnlyInstruction(line)).join(" ") || null
    : null;
  return instructions ? (
    <p className="sp-block-purpose ex-execution-instructions">
      <span className="sp-stat-label">How</span>{instructions}
    </p>
  ) : null;
}

function sameCopy(a: string | null, b: string | null): boolean {
  const norm = (value: string | null) => (value ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "");
  return Boolean(a && b) && norm(a) === norm(b);
}

const BLOCK_TYPE_GLYPHS: Record<string, ExerciseGlyphName> = {
  strength: "strength",
  accessory: "strength",
  strength_speed: "power",
  plyometric_power: "power",
  speed: "power",
  conditioning: "conditioning",
  preparation: "mobility",
  mobility_activation: "mobility",
  cooldown_recovery: "recovery",
  rehab: "rehab",
  skill: "skill",
  sparring: "skill",
  mindset: "visualisation",
};

/** The icon that stands in for a missing demo: the exercise's name refines its
 * type (a "Tactical Watch" is a skill block, but it is film, not a drill). */
function exerciseGlyph(block: StructuredBlock): ExerciseGlyphName {
  const name = (cleanText(block.display_name) ?? "").toLowerCase();
  if (/tactical|watch|film|footage/.test(name)) return "tactical";
  if (/visuali|imagery|read\s*(?:→|->|and)\s*react/.test(name)) return "visualisation";
  if (/breath/.test(name)) return "breathing";
  if (/mobility|stretch|\bcars\b/.test(name)) return "mobility";
  return BLOCK_TYPE_GLYPHS[cleanText(block.block_type) ?? ""] ?? "strength";
}

function ExerciseGlyph({ block }: { block: StructuredBlock }) {
  return <ExerciseGlyphIcon name={exerciseGlyph(block)} />;
}

/**
 * One exercise in a session: a compact row (name, headline stat, demo thumbnail)
 * that expands to the demo, what the exercise builds, why it is in today's
 * session, and the full prescription. Rows in a session are single-open.
 */
export function ExerciseRow({
  block,
  open,
  onToggle,
  openWeekIntent,
  sourceCountdown,
  detail = "inline",
  compact = false,
  actions,
}: {
  block: StructuredBlock;
  open: boolean;
  onToggle: () => void;
  /** "sheet" opens the exercise in its own sheet (Today) instead of in place. */
  detail?: "inline" | "sheet";
  /** Plan lists use plain-language prescriptions and dividers, with full details in a sheet. */
  compact?: boolean;
  actions?: ReactNode;
  openWeekIntent?: OpenBlockWeekIntent | null;
  sourceCountdown?: string | null;
}) {
  const bodyId = useId();
  const rehabLabelPolicy = useContext(RehabLabelContext);
  const prescription = useBlockPrescription(block, sourceCountdown);
  const { title, compactStopRule } = prescription;
  const blockType = cleanText(block.block_type);
  const tagLabel = blockType && (!compact || isRehabBlock(block)) ? blockTagLabel(block, rehabLabelPolicy) : null;
  const resolvedMedia = useExerciseMedia(block.display_name, block.exercise_key);
  const media = block.prescription_source === "athlete" && !block.exercise_key ? null : resolvedMedia;
  const summary = exerciseRowSummary(block, prescription, compact);
  // Stop criteria live in the opened exercise. Active range/contact/clinical
  // restrictions still belong on the plan, independent of the stop disclosure.
  const rehabLimits = compact && isRehabBlock(block)
    ? getRehabExerciseGuide(block).safety.filter(rule => !/^stop\b/i.test(rule)) : [];
  const showRationale = useContext(ExerciseRationaleContext);
  const builds = showRationale ? athleteFacingRationale(block.purpose) : null;
  const whyTodayRaw = showRationale ? athleteFacingRationale(block.why_today) : null;
  const whyToday = sameCopy(builds, whyTodayRaw) ? null : whyTodayRaw;
  const leadCue = getBlockExecutionDisplay(block).cues[0] ?? null;
  // Matches the demo's first render (facade, not yet known to be watched), so
  // the server markup and hydration never print the lead cue twice.
  const [leadCuePinned, setLeadCuePinned] = useState(Boolean(media && leadCue));
  // A row whose detail opens in a sheet keeps its compact face underneath.
  const rowCollapsed = !open || detail === "sheet";

  const metadata = tagLabel || summary ? (
    <span className="ex-row-meta">
      {tagLabel ? (
        <span className="ex-row-type">
          {tagLabel}
          {/* Only Rehab/Prehab/Mobility carry a definition. */}
          <GlossaryTooltip term={tagLabel} />
        </span>
      ) : null}
      {summary ? (
        <span className="ex-row-summary">
          {/* Each part stays whole ("RPE 7", never "RPE / 7"); lines
              break only at the separators. */}
          {summary.split(" · ").map((part, index) => (
            <span key={`${index}:${part}`} className="ex-row-summary-part">
              {index > 0 ? " · " : null}
              <span>{part}</span>
            </span>
          ))}
        </span>
      ) : null}
    </span>
  ) : null;

  return (
    <div className={`ex-row${compact ? " ex-row-compact" : ""}`} data-open={open ? "true" : "false"}
      id={detail === "sheet" ? rehabExerciseTarget(block) : undefined}
      tabIndex={detail === "sheet" && rehabExerciseTarget(block) ? -1 : undefined}>
      {/* The toggle covers the whole header through its ::after, so the row
          stays one big tap target while the type's glossary "i" (a button of
          its own) can sit in the header without nesting inside it. */}
      <div className="ex-row-head">
        {/* Today, once the session is started: one tap logs it as written. */}
        <ExerciseLogTick block={block} onOpen={() => (open ? undefined : onToggle())} />
        <div className="ex-row-text">
          <button
            type="button"
            className="ex-row-toggle"
            aria-expanded={open}
            aria-controls={bodyId}
            aria-haspopup={detail === "sheet" ? "dialog" : undefined}
            onClick={onToggle}
          >
            <span className="ex-row-title">{title}</span>
            {media || block.private_media_id || block.community_revision_id ? <span className="sr-only"> Has demo video.</span> : null}
          </button>
          {!compact ? metadata : null}
          {/* What was logged for it today; open, the log panel below says so. */}
          {rowCollapsed ? <ExerciseLogBadge block={block} /> : null}
          {/* Stop criteria are safety instructions: never hidden behind the tap.
              Collapsed, the row carries it; open, the full card below does.
              Rows that open a sheet leave it to the sheet, which leads with it. */}
          {!compact && !open && detail === "inline" && compactStopRule ? <span className="ex-row-stop">
            <span className="sp-stat-label">Stop rule</span>{compactStopRule}
          </span> : null}
        </div>
        {/* Collapsed rows show which exercises have a demo; open, the demo
            itself is right below. On the right so every title lines up. */}
        {rowCollapsed && (block.private_media_id || block.community_revision_id) ? (
          <PrescribedPrivateVideoThumbnail id={block.private_media_id || block.community_revision_id!} community={!block.private_media_id} />
        ) : media && rowCollapsed ? (
          <span className="ex-row-thumb" aria-hidden="true">
            {/* eslint-disable-next-line @next/next/no-img-element -- remote YouTube thumbnail, not a local asset */}
            <img
              src={demoThumbnailUrl(media.video_id, "mq")}
              alt=""
              loading="lazy"
              onError={(event) => {
                // No thumbnail (restricted or removed video): keep the dark tile
                // and play glyph rather than a broken-image icon.
                event.currentTarget.style.display = "none";
              }}
            />
            <span className="ex-demo-play-glyph ex-demo-play-glyph-sm" />
          </span>
        ) : rowCollapsed ? (
          // Holds the thumbnail column where Today's section rows show one, so
          // every title still lines up; hidden everywhere else.
          <span className="ex-row-placeholder" aria-hidden="true">
            <ExerciseGlyph block={block} />
          </span>
        ) : null}
        {/* Swap / remove: only where the owning surface allows a change. */}
        {actions ?? <ExerciseActions block={block} title={title} />}
        <span className="ex-row-chevron" aria-hidden="true" />
      </div>
      {compact ? <div className="ex-row-summary-body">
        {metadata}
        {rehabLimits.length > 0 ? <RehabExerciseLimits rules={rehabLimits} compact /> : null}
      </div> : null}
      {open && detail === "sheet" ? (
        <InjuryDetailSheet title={title} onClose={onToggle}>
          <div id={bodyId}>
            <ExerciseDetail block={block} openWeekIntent={openWeekIntent} sourceCountdown={sourceCountdown} />
          </div>
        </InjuryDetailSheet>
      ) : open ? (
        <div id={bodyId} className="ex-row-body">
          {block.block_type === "rehab" && block.drill_snapshot ? (
            <RehabExerciseGuide block={block} demo={<RehabDemo block={block} media={media} title={title} />}>
              <ExerciseLogPanel block={block} />
            </RehabExerciseGuide>
          ) : <>
          {block.private_media_id || block.community_revision_id ? <PrescribedPrivateVideo
            id={block.private_media_id || block.community_revision_id!} community={!block.private_media_id} name={title} /> : null}
          {media ? (
            <ExerciseDemo
              media={media}
              exerciseName={title}
              leadCue={leadCue}
              onLeadCuePinnedChange={setLeadCuePinned}
            />
          ) : null}
          {builds || whyToday ? (
            <div className="ex-row-why">
              {builds ? (
                <p className="ex-row-why-line">
                  <span className="sp-stat-label">Builds</span>
                  {builds}
                </p>
              ) : null}
              {whyToday ? (
                <p className="ex-row-why-line ex-row-why-today">
                  <span className="sp-stat-label">Why today</span>
                  {whyToday}
                </p>
              ) : null}
            </div>
          ) : null}
          <BlockCard
            block={block}
            openWeekIntent={openWeekIntent}
            sourceCountdown={sourceCountdown}
            embedded
            hideLeadCue={Boolean(media) && leadCuePinned}
          />
          {/* Renders only where a started session provides logging (Today). */}
          <ExerciseLogPanel block={block} />
          </>}
        </div>
      ) : null}
    </div>
  );
}

/** The same curated or prescribed private demo the plan's exercise rows use. */
function RehabDemo({ block, media, title }: { block: StructuredBlock; media: ReturnType<typeof useExerciseMedia>; title: string }) {
  if (block.private_media_id || block.community_revision_id) {
    return <PrescribedPrivateVideo id={block.private_media_id || block.community_revision_id!} community={!block.private_media_id} name={title} />;
  }
  return media ? <ExerciseDemo media={media} exerciseName={title} /> : null;
}

/** Exercise detail for Today. Safety remains visible outside disclosures. */
function ExerciseDetail({
  block,
  openWeekIntent,
  sourceCountdown,
}: {
  block: StructuredBlock;
  openWeekIntent?: OpenBlockWeekIntent | null;
  sourceCountdown?: string | null;
}) {
  const rehabLabelPolicy = useContext(RehabLabelContext);
  const prescription = useBlockPrescription(block, sourceCountdown);
  const { title, compactStopRule, progression } = prescription;
  const blockType = cleanText(block.block_type);
  const tagLabel = blockType ? blockTagLabel(block, rehabLabelPolicy) : null;
  const resolvedMedia = useExerciseMedia(block.display_name, block.exercise_key);
  const media = block.prescription_source === "athlete" && !block.exercise_key ? null : resolvedMedia;
  const summary = exerciseRowSummary(block, prescription);
  const builds = athleteFacingRationale(block.purpose);
  const whyTodayRaw = athleteFacingRationale(block.why_today);
  const whyToday = sameCopy(builds, whyTodayRaw) ? null : whyTodayRaw;
  const execution = getBlockExecutionDisplay(block);
  const allCues = execution.cues.map(compactCoachLine);
  // Banked mindset drills (Tactical Watch, Fight Visualisation) store their
  // ordered instructions as cues: show those as numbered steps rather than
  // one "Cue" label per step. Labelled extras ("Cue: …") stay cues.
  const stepped = cleanText(block.block_type) === "mindset" && allCues.length > 1;
  const isLabelledCue = (line: string) => /^(?:cue|pre-bout):/i.test(line);
  const steps = stepped ? allCues.filter(line => !isLabelledCue(line)) : [];
  const cues = stepped ? allCues.filter(isLabelledCue).map(line => line.replace(/^cue:\s*/i, "")) : allCues;
  const easier = execution.regressions.map(compactCoachLine);
  const swap = execution.substitutions[0] ?? null;
  const sessionRunning = useExerciseLoggingActive();
  // Banked injury rehab has an authoritative prescription to guide, led by the
  // plan's shared demo when one exists. Legacy prehab/training blocks retain
  // their ordinary exercise adjustments/logging.
  if (block.block_type === "rehab" && block.drill_snapshot) {
    return <RehabExerciseGuide block={block} demo={<RehabDemo block={block} media={media} title={title} />}>
      <ExerciseLogPanel block={block} />
    </RehabExerciseGuide>;
  }
  // Safety instructions are never behind a tap, logging included: one plain line.
  const stopLine = compactStopRule ? (
    <p className="ex-detail-stop">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
      </svg>
      <span>Stop if {compactStopRule.replace(/^(?:(?:stop\s+)?(?:if|when|for))\s+/i, "")}</span>
    </p>
  ) : null;

  // Ordinary mid-session sheets focus on logging. Rehab keeps its demo and
  // approved instructions available while the athlete follows the exercise.
  if (sessionRunning && block.block_type !== "rehab") {
    return (
      <div className="injury-sheet-body ex-detail ex-detail-logging">
        {tagLabel || summary ? (
          <p className="ex-detail-summary">{[tagLabel, summary].filter(Boolean).join(" · ")}</p>
        ) : null}
        {stopLine}
        {steps.length > 0 ? (
          <div className="sp-block-aside ex-detail-steps">
            <span className="sp-stat-label">Steps</span>
            <ol>{steps.map(step => <li key={step}>{step}</li>)}</ol>
          </div>
        ) : null}
        <div className="ex-detail-prescription">
          <BlockCard block={block} openWeekIntent={openWeekIntent}
            sourceCountdown={sourceCountdown} embedded hideCues hideStopRule hideAdjustments
            hideInstructions={Boolean(media || block.private_media_id || block.community_revision_id)} />
        </div>
        <ExerciseLogPanel block={block} />
      </div>
    );
  }

  return (
    <div className="injury-sheet-body ex-detail">
      {tagLabel || summary ? (
        <p className="ex-detail-summary">{[tagLabel, summary].filter(Boolean).join(" · ")}</p>
      ) : null}
      {/* Safety instructions are never behind a tap: one plain line. */}
      {block.block_type === "rehab" && (block.stop_rules?.length || execution.stopRules.length)
        ? <RehabExerciseLimits rules={Array.from(new Set([...getStringList(block.stop_rules), ...execution.stopRules]))} /> : stopLine}
      {media ? (
        <details className="ex-detail-section" open={!sessionRunning || block.block_type === "rehab"}>
          <summary>Demo video</summary>
          <ExerciseDemo media={media} exerciseName={title} />
        </details>
      ) : null}
      {block.private_media_id || block.community_revision_id ? (
        <details className="ex-detail-section" open={!sessionRunning}>
          <summary>Demo video</summary>
          <PrescribedPrivateVideo id={block.private_media_id || block.community_revision_id!} community={!block.private_media_id} name={title} />
        </details>
      ) : null}
      {builds || whyToday ? (
        <details className="ex-detail-section">
          <summary>Why it&apos;s here</summary>
          {builds ? (
            <p className="ex-row-why-line">
              <span className="sp-stat-label">Builds</span>
              {builds}
            </p>
          ) : null}
          {whyToday ? (
            <p className="ex-row-why-line ex-row-why-today">
              <span className="sp-stat-label">Why today</span>
              {whyToday}
            </p>
          ) : null}
        </details>
      ) : null}
      <ExecutionInstructions block={block} />
      {/* Keep the cue and available adjustments outside nested disclosures. */}
      {steps.length > 0 ? (
        <div className="sp-block-aside ex-detail-steps">
          <span className="sp-stat-label">Steps</span>
          <ol>{steps.map(step => <li key={step}>{step}</li>)}</ol>
        </div>
      ) : null}
      {cues[0] ? (
        <p className="ex-detail-key-cue">
          <span className="sp-stat-label">Key cue</span>
          <strong>{cues[0]}</strong>
        </p>
      ) : null}
      {cues.length > 1 ? (
        <details className="ex-detail-section">
          <summary>Coaching cues</summary>
          {cues.slice(1).map(cue => <p className="sp-block-aside" key={cue}><span className="sp-stat-label">Cue</span>{cue}</p>)}
        </details>
      ) : null}
      {swap ? <p className="sp-block-aside"><span className="sp-stat-label">Swaps</span>{swap}</p> : null}
      {easier.map(text => <p className="sp-block-aside" key={text}><span className="sp-stat-label">Easier</span>{text}</p>)}
      {progression && openWeekIntent?.key !== "deload" ? <p className="sp-block-aside"><span className="sp-stat-label">Progress</span>{progression}</p> : null}
      <details className="ex-detail-section" open={block.block_type === "rehab" || undefined}>
        <summary>Full prescription</summary>
        <BlockCard block={block} openWeekIntent={openWeekIntent}
          sourceCountdown={sourceCountdown} embedded hideCues hideStopRule hideAdjustments hideInstructions />
      </details>
      {/* Renders only where a started session provides logging (Today). */}
      <ExerciseLogPanel block={block} />
    </div>
  );
}

function RehabSummary({ blocks }: { blocks: StructuredBlock[] }) {
  const rehabLabelPolicy = useContext(RehabLabelContext);
  if (blocks.length === 0) {
    return null;
  }
  // Mixed sessions read as "Rehab": one live region is enough to keep the whole
  // list honest, even when the other entries have cleared into prehab. `blocks`
  // also carries mobility work, which the helper ignores.
  const summaryLabel = resolveRehabSummaryLabel(blocks, rehabLabelPolicy);
  return (
    <div className="sp-rehab-summary">
      <p className="sp-eyebrow">
        {summaryLabel} / Mobility
        <GlossaryTooltip term={summaryLabel} />
      </p>
      <ul className="sp-rehab-list">
        {blocks.map((block, index) => {
          const title = cleanText(block.display_name) || "Rehab block";
          const metric = blockSummaryMetric(block);
          return (
            <li key={cleanText(block.block_id) || `${title}-${index}`}>
              <span className="sp-rehab-title">{title}</span>
              {metric ? <span className="sp-rehab-metric">{metric}</span> : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Completion copy is shared by physical and mental sessions; the stored reason stays intact. */
function SessionCompletionDetails({ info, compact }: { info?: SessionCompletionInfo; compact: boolean }) {
  const record = info?.completion;
  if (!record) return null;
  const reason = cleanText(record.modification_reason);
  const summary = reason?.replace(/^(?:Reason:\s*)?(?:Changed:\s*)?/i, "").split(/\n/)[0];
  return <>
    {record.session_rpe != null ? <p className="sp-session-log-meta">
      {`RPE ${record.session_rpe}/10`}<GlossaryTooltip term="RPE" />
    </p> : null}
    {reason ? compact ? <details className="sp-modification-detail">
      <summary>{summary && summary.length > 100 ? `${summary.slice(0, 100).trimEnd()}…` : summary || "Modification details"}</summary>
      <p>{reason}</p>
    </details> : <p className="sp-session-log-meta">Reason: {reason}</p> : null}
  </>;
}

function PlanSupportCard({ title, duration, category, optional, children }: {
  title: string;
  duration: string | null;
  category: string | null;
  optional: boolean;
  children: ReactNode;
}) {
  return (
    <article className={`sp-session sp-session-support${category && category !== "Breathe" ? " sp-mental-task" : ""}`} data-layout="plan" data-mental-category={category && category !== "Breathe" ? category.toLowerCase() : undefined}>
      <details className="sp-optional-disclosure">
        <summary className="sp-optional-summary">
          <span className="sp-optional-main">
            <span className="sp-optional-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{category === "Visualise" ? <><path d="M12 5a3 3 0 0 0-5.5-1.5A4 4 0 0 0 4 9a4 4 0 0 0 1 7 3.5 3.5 0 0 0 7 1V5Z"/><path d="M12 5a3 3 0 0 1 5.5-1.5A4 4 0 0 1 20 9a4 4 0 0 1-1 7 3.5 3.5 0 0 1-7 1V5ZM8 8c2 0 2 2 2 3M16 8c-2 0-2 2-2 3M8 15c2 0 2-2 2-3M16 15c-2 0-2-2-2-3"/></> : category === "Analyse" ? <><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6Z"/><circle cx="12" cy="12" r="3"/></> : category === "Breathe" ? <><path d="M3 8h12a3 3 0 1 0-3-3M3 12h16a3 3 0 1 1-3 3M3 16h6"/></> : <><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 8h7M8 12h7M8 16h4"/></>}</svg></span>
            <span className="sp-optional-heading"><strong>{title}</strong>{duration ? <span>{duration}</span> : null}</span>
          </span>
          <span className="sp-optional-side">{optional ? <span>Optional</span> : null}<span className="sp-optional-chevron" aria-hidden="true">⌄</span></span>
        </summary>
        <div className="sp-optional-content">{children}</div>
      </details>
    </article>
  );
}

export function SessionCard({
  session,
  day,
  sourceCountdown,
  defaultOpenBlocks,
  initiallyOpenFirstBlock = true,
  hideTitle = false,
  showDayContext = true,
  showDayLabels = true,
  completionInfo,
  openWeekIntent,
  layout = "card",
  sectionIndex = 0,
  occurrenceDate,
}: {
  /** Calendar date of this session's occurrence, for athlete exercise changes
   * (a session after the first one in a day is rendered without its day). */
  occurrenceDate?: string | null;
  /** "section" is Today's numbered session part: every exercise row in view,
   * the rationale and coaching cues behind one disclosure. */
  layout?: "card" | "section" | "plan";
  /** Zero-based position of this session in the day, for the "01 / ..." label. */
  sectionIndex?: number;
  session: StructuredSession;
  day?: StructuredDay;
  /** Countdown used only to reconcile exact source prescription ranges. */
  sourceCountdown?: string | null;
  defaultOpenBlocks?: boolean;
  /** Today keeps exercise details behind a tap so the session action stays in view. */
  initiallyOpenFirstBlock?: boolean;
  /** The parent Today card already names this exact session. */
  hideTitle?: boolean;
  /** When false, day-level context like warnings/nutrition/mindset is rendered by
   * the parent day card instead, so the same information does not repeat inside
   * every session. */
  showDayContext?: boolean;
  /** When false, the countdown/date labels are omitted because the parent day
   * row already shows them (the plan accordion); standalone surfaces like the
   * Today tab keep the default. */
  showDayLabels?: boolean;
  /** Live logged status for this session (plan viewer only). Absent on
   * surfaces without completion data (e.g. the Today screen's embedded card). */
  completionInfo?: SessionCompletionInfo;
  /** Development-block week intent of an open (renewable) plan, forwarded to
   * every block card. */
  openWeekIntent?: OpenBlockWeekIntent | null;
}) {
  const detailsId = useId();
  const rehabLabelPolicy = useContext(RehabLabelContext);
  const [showDetails, setShowDetails] = useState(Boolean(defaultOpenBlocks));
  const userToggledDetails = useRef(false);
  // One exercise open at a time. Today and the current plan day can start with
  // compact rows so a demo does not push the rest of the session below the fold.
  const [openBlockIndex, setOpenBlockIndex] = useState<number | null>(initiallyOpenFirstBlock && layout !== "plan" ? 0 : null);
  const planLayout = layout === "plan";

  useEffect(() => {
    if (!userToggledDetails.current) {
      setShowDetails(Boolean(defaultOpenBlocks));
    }
  }, [defaultOpenBlocks]);

  const card = day?.today_card;
  const title = humanizeIfRawEnum(stripLeadingWeekday(
    ((session.blocks?.length ?? 0) > 0 && session.blocks!.every(isCalmRehabBlock) ? (cleanText(session.title) ?? "").replace(/rehab/gi, "guidance") : cleanText(session.title)) ||
    cleanText(card?.headline) ||
    titleize(cleanText(session.session_type) || "Session"),
  ));
  const isDeclaredLightCombat = isDeclaredLightCombatTitle(title);
  const isTechnicalSession = /^(?:D[-−]\d+\s*[—–:-]\s*)?Technical[ -]only combat$/i.test(title);
  const sessionType = cleanText(session.session_type);
  const isOptional = session.optional === true || /^Optional\b/i.test(cleanText(session.objective) ?? "");
  const supportCard = planLayout && (isOptional || mentalSessionCategory(session) !== null || isCompactPlanSupportSession(session)
    || (getBlocks(session).length > 0 && getBlocks(session).every(block => block.block_type === "mindset")));
  const rawObjective = formatSessionObjective(session.objective);
  const objective = supportCard ? rawObjective?.replace(/^Optional[.:]\s*/i, "") || null : rawObjective;
  // A rationale that is just the session title again ("Strength" / "Why
  // Strength") explains nothing; show the line only when it adds a reason.
  const objectiveRepeatsTitle =
    objective != null && objective.trim().toLowerCase() === title.trim().toLowerCase();
  const duration = formatMeasured(session.planned_duration);
  const date = cleanText(day?.date);
  const countdown = formatCountdownLabel(day?.countdown_label);
  const warning = showDayContext ? cleanText(card?.primary_warning) : null;
  const nutrition = showDayContext ? cleanText(card?.nutrition_summary) : null;
  const weightCut = showDayContext ? cleanText(card?.weight_cut_warning) : null;
  const blocks = getBlocks(session);
  const repeatsOnlyExercise = planLayout && blocks.length === 1
    && sameCopy(title, cleanText(blocks[0].display_name));
  // `rehab` is the schema's home for mobility support too, so the chip is
  // resolved rather than printed raw: a joint-prep card must not claim rehab.
  const sessionTypeLabel =
    resolveSessionTypeLabel(session, rehabLabelPolicy) || titleize(sessionType ?? "");
  const isZeroLoad = isZeroLoadSupportSession(session);
  const rehabBlocks = getRehabOrMobilityBlocks(session);
  const blocksLabel = blockCountLabel(blocks.length);
  const sessionMindset =
    getSessionCoachingLines(session.mindset_anchor).length > 0
      ? session.mindset_anchor
      : showDayContext
        ? card?.mindset_anchor
        : undefined;

  const mentalCategory = mentalSessionCategory(session)
    || (planLayout && blocks.length > 0 && blocks.every(block => block.block_type === "mindset") ? "Mental" : null);
  if (mentalCategory) {
    // Tactical analysis and reflection are prompt-driven, not timed workouts.
    // Ignore synthetic durations in legacy saved cards as well as new plans.
    const mentalDuration = mentalCategory === "Analyse" || mentalCategory === "Reflect"
      ? null
      : duration || formatMeasured(blocks.find((block) => block.duration)?.duration);
    const summary = mentalSessionSummary(session) || (mentalCategory === "Reflect"
      ? "What worked? What needs adjusting next time?"
      : mentalCategory === "Analyse"
        ? "Review one tactical issue from sparring or available footage. Identify one adjustment for the next session."
        : "Rehearse one familiar fight situation and reset calmly.");
    const authored = blocks.flatMap(block => [
      ...getStringList(block.coaching_cues),
      ...(block.notes ? block.notes.split(/\n+/) : []),
    ]);
    const steps = mentalTaskSteps(authored);
    const mentalStops = Array.from(new Set(blocks.flatMap(block => getBlockAdjustmentDisplay(block).stopRules)));
    // The plan card carries the whole task: why it matters and how to progress
    // it sit inline beside the steps rather than behind a sheet.
    const mentalObjective = planLayout && objective && !objectiveRepeatsTitle
      && !steps.some(step => sameCopy(step, objective))
      && !(steps.length === 0 && sameCopy(summary, objective)) ? objective : null;
    const mentalProgressions = planLayout
      ? Array.from(new Set(blocks.map(block => getBlockAdjustmentDisplay(block).progression)
        .filter((line): line is string => Boolean(line) && !steps.some(step => sameCopy(step, line!)))))
      : [];
    const mentalDrill = blocks.map(block => cleanText(block.display_name))
      .find(name => name && !sameCopy(name, title));
    // Low-load tasks show the steps only: Cue / Reset / Coach cue restate the
    // drill and bloat the card. A constraint Context line still prints.
    const context = getSessionCoachingLines(sessionMindset).filter(line =>
      line.label === "Context" && !steps.some(step => sameCopy(step, line.value)));
    const cleanOptional = (value: string) => supportCard ? value.replace(/^Optional[.:]\s*/i, "") : value;
    const content = (
      <>
        <header className="sp-mental-head">
          {hideTitle ? (mentalDuration ? <p className="sp-mental-duration">{compactTimeUnits(mentalDuration)}</p> : null)
            : <h3 className="sp-session-title">{title || mentalCategory}{mentalDuration ? ` · ${compactTimeUnits(mentalDuration)}` : ""}</h3>}
          {completionInfo?.display.label ? (
            <span className="sp-tag sp-status-tag" data-tone={completionInfo.display.tone}>
              {completionInfo.display.label}
            </span>
          ) : null}
        </header>
        {mentalDrill ? <p className="sp-session-instruction">{mentalDrill}</p> : null}
        {mentalObjective ? <p className="sp-session-objective">{cleanOptional(mentalObjective)}</p> : null}
        {steps.length > 0 ? (
          <ol className="sp-cues sp-mental-steps">
            {steps.map((instruction, index) => <li key={`${index}-${instruction}`}>{instruction}</li>)}
          </ol>
        ) : <p className="sp-session-instruction">{cleanOptional(summary)}</p>}
        <SessionCompletionDetails info={completionInfo} compact={planLayout} />
        {context.map(line => <p className="sp-block-aside" key={`${line.label}-${line.value}`}>
          <span className="sp-stat-label">{line.label}</span>{line.value}
        </p>)}
        {mentalProgressions.map(line => <p className="sp-block-aside" data-kind="progression" key={line}>
          <span className="sp-stat-label">Progression</span>{line}
        </p>)}
        {mentalStops.length > 0 ? (
          <p className="sp-block-aside" data-kind="stop"><span className="sp-stat-label">Stop rule</span>{mentalStops.join("; ")}</p>
        ) : null}
        {completionInfo?.onLog ? (
          <button type="button" className="secondary-button sp-log-session" onClick={completionInfo.onLog}>
            Log this session
          </button>
        ) : null}
        {warning ? <p className="sp-warning">{warning}</p> : null}
        {weightCut ? <p className="sp-warning">{weightCut}</p> : null}
      </>
    );
    return supportCard ? (
      <PlanSupportCard title={title || mentalCategory} duration={mentalDuration} category={mentalCategory} optional={isOptional}>{content}</PlanSupportCard>
    ) : <article className="sp-session sp-mental-task" data-layout={planLayout ? "plan" : undefined} data-mental-category={mentalCategory.toLowerCase()}>{content}</article>;
  }

  if (layout === "section") {
    const sectionTitle = isDeclaredLightCombat
      ? DECLARED_LIGHT_COMBAT_TITLE
      : isTechnicalSession
        ? TECHNICAL_COMBAT_TITLE
        : title;
    const sectionType = isDeclaredLightCombat
      ? "Technical"
      : isTechnicalSession
        ? TECHNICAL_COMBAT_TAG
        : sessionTypeLabel;
    const sectionMeta = [
      // The plan's estimate, not a measured time.
      duration ? `~${duration}` : null,
      blocks.length > 1 ? `${blocks.length} exercises` : null,
    ].filter((part): part is string => Boolean(part));
    // With blocks, the objective is the reason for the work and waits behind
    // the disclosure. Without blocks it IS the prescription, so it stays out.
    const objectiveIsInstruction = blocks.length === 0 && objective && !objectiveRepeatsTitle;
    const whyText = isDeclaredLightCombat
      ? DECLARED_LIGHT_COMBAT_DESCRIPTION
      : !objectiveIsInstruction && objective && !objectiveRepeatsTitle
        ? objective
        : null;
    const hasDetails =
      Boolean(whyText) || isTechnicalSession || getSessionCoachingLines(sessionMindset).length > 0;
    return (
      <article className="sp-session" data-layout="section">
        <header className="sp-section-head">
          <div>
            <p className="sp-section-eyebrow">
              <span>{String(sectionIndex + 1).padStart(2, "0")}</span>
              {sectionType ? <span>{sectionType}</span> : null}
              {isZeroLoad ? <span>Zero load</span> : null}
            </p>
            {hideTitle && !isTechnicalSession ? null : (
              <h3 className="sp-session-title">{sectionTitle}</h3>
            )}
          </div>
          {sectionMeta.length > 0 || completionInfo?.display.label ? (
            <p className="sp-section-meta">
              {sectionMeta.join(" · ")}
              {completionInfo?.display.label ? (
                <span className="sp-tag sp-status-tag" data-tone={completionInfo.display.tone}>
                  {completionInfo.display.label}
                </span>
              ) : null}
            </p>
          ) : null}
        </header>
        {objectiveIsInstruction ? (
          <p className="sp-session-objective sp-session-instruction">{objective}</p>
        ) : null}
        {hasDetails ? (
          <details className="sp-section-details">
            <summary>Why this session</summary>
            <div className="sp-section-details-body">
              {isTechnicalSession ? <TechnicalCombatRationale /> : null}
              {whyText ? <p className="sp-session-objective">{whyText}</p> : null}
              <MindsetAnchorCard
                anchor={sessionMindset}
                contextOnly={blocks.some(block => getBlockExecutionDisplay(block).cues.length > 0)}
                dedupeContext={isTechnicalSession ? TECHNICAL_COMBAT_RATIONALE : whyText ?? objective}
              />
            </div>
          </details>
        ) : null}
        <ExerciseSessionScope date={occurrenceDate ?? day?.date} session={session}>
          {blocks.length > 0 ? (
            <div className="sp-blocks ex-rows">
              {blocks.map((block, index) => (
                <ExerciseRow
                  key={cleanText(block.block_id) || `block-${index}`}
                  block={block}
                  open={openBlockIndex === index}
                  onToggle={() => setOpenBlockIndex((current) => (current === index ? null : index))}
                  openWeekIntent={openWeekIntent}
                  sourceCountdown={sourceCountdown}
                  detail="sheet"
                />
              ))}
            </div>
          ) : null}
          <SessionChangesFoot session={session} />
        </ExerciseSessionScope>
      </article>
    );
  }

  const content = (
    <>
      <header className="sp-session-head">
        <div>
          {showDayLabels && (countdown || date) ? (
            <div className="sp-day-labels sp-session-day-labels">
              {countdown ? <span className="sp-countdown sp-accent">{countdown}</span> : null}
              {date ? <span className="sp-day-date">{formatAppDate(date)}</span> : null}
            </div>
          ) : null}
          {/* Today keeps the technical title (it carries the why); a plan sheet
              already titles the session, so its rationale moves beside the tag. */}
          {((hideTitle || repeatsOnlyExercise) && (!isTechnicalSession || planLayout)) || (planLayout && (isDeclaredLightCombat || isTechnicalSession)) ? null : <h3 className="sp-session-title">
            {isDeclaredLightCombat
              ? DECLARED_LIGHT_COMBAT_TITLE
              : isTechnicalSession
                ? TECHNICAL_COMBAT_TITLE
                : title}
            {(isTechnicalSession || isDeclaredLightCombat) ? <TechnicalCombatWhyTooltip day={day} declared={isDeclaredLightCombat} /> : null}
          </h3>}
          {/* The objective is the plan's "Why:" line, not a description of the
              work — the blocks below already carry that. Labelling it says so
              outright, so the reason for the session is impossible to miss.
              A session with NO blocks has nothing for a rationale to explain:
              there, the objective IS the whole prescription ("Neck CARs,
              shoulder CARs, wrist circles…"), so the kicker is dropped and the
              line reads as the instruction it is. */}
          {isDeclaredLightCombat || isTechnicalSession ? null : objective && !objectiveRepeatsTitle ? (
            <p
              className={`sp-session-objective${blocks.length > 0 ? "" : " sp-session-instruction"}`}
            >
              {blocks.length > 0 && !planLayout ? <span className="sp-session-why-label">Why</span> : null}
              {objective}
            </p>
          ) : null}
        </div>
        <div className="sp-session-meta">
          {isDeclaredLightCombat || isTechnicalSession ? null : sessionType && (!planLayout || !sameCopy(sessionTypeLabel, title)) ? (
            <span className="sp-tag">{sessionTypeLabel}</span>
          ) : null}
          {/* Scheduled mental / tactical / breathing-only work renders in full,
              but must never read as physical S&C — say so on the card itself. */}
          {isZeroLoad ? <span className="sp-tag">Zero load</span> : null}
          {duration ? <span className="sp-tag">{duration}</span> : null}
          {completionInfo?.display.label ? (
            <span className="sp-tag sp-status-tag" data-tone={completionInfo.display.tone}>
              {completionInfo.display.label}
            </span>
          ) : null}
        </div>
      </header>

      {isDeclaredLightCombat || isTechnicalSession ? (
        <CoachDayBox
          tag={isDeclaredLightCombat ? "Technical" : TECHNICAL_COMBAT_TAG}
          heading={planLayout ? <h3 className="sp-session-title">{isDeclaredLightCombat ? DECLARED_LIGHT_COMBAT_TITLE : TECHNICAL_COMBAT_TITLE}<TechnicalCombatWhyTooltip day={day} declared={isDeclaredLightCombat} /></h3> : null}
          tooltip={
            isDeclaredLightCombat
              ? hideTitle ? <TechnicalCombatWhyTooltip day={day} declared /> : null
              : hideTitle && planLayout ? <TechnicalCombatWhyTooltip day={day} declared={false} /> : null
          }
        >
          {isDeclaredLightCombat ? (
            <p className="sp-today-note">{DECLARED_LIGHT_COMBAT_DESCRIPTION}</p>
          ) : (
            <TechnicalCombatRationale />
          )}
        </CoachDayBox>
      ) : null}

      <SessionCompletionDetails info={completionInfo} compact={planLayout} />

      {completionInfo?.onLog ? (
        <button type="button" className="secondary-button sp-log-session" onClick={completionInfo.onLog}>
          Log this session
        </button>
      ) : null}

      {warning ? <p className="sp-warning">{warning}</p> : null}
      {nutrition ? <p className="sp-today-note">{nutrition}</p> : null}
      {weightCut ? <p className="sp-warning">{weightCut}</p> : null}

      {showDayContext && day ? <PriorityMicrodoseCard day={day} /> : null}

      <MindsetAnchorCard
        anchor={sessionMindset}
        contextOnly={blocks.some(block => getBlockExecutionDisplay(block).cues.length > 0)}
        dedupeContext={
          isDeclaredLightCombat
            ? DECLARED_LIGHT_COMBAT_DESCRIPTION
            : isTechnicalSession
              ? TECHNICAL_COMBAT_RATIONALE
              : objective
        }
      />
      {/* The rehab/mobility summary is a compact PREVIEW of the inserts shown
          only while the full blocks are collapsed. Once expanded, every rehab
          block renders in full below, so keeping the summary too would print the
          same insert as two separate cards. */}
      {!showDetails && !planLayout ? <RehabSummary blocks={rehabBlocks} /> : null}

      {blocks.length > 0 ? (
        <>
          {!planLayout ? <button
            type="button"
            className="sp-more-toggle sp-session-toggle"
            aria-expanded={showDetails}
            aria-controls={detailsId}
            aria-label={showDetails ? "Show less session detail" : `Show more session detail: ${blocksLabel}`}
            onClick={() => {
              userToggledDetails.current = true;
              setShowDetails((prev) => !prev);
            }}
          >
            {showDetails ? "Show less" : `Show more (${blocksLabel})`}
          </button> : null}

          {showDetails || planLayout ? (
            <ExerciseSessionScope date={occurrenceDate ?? day?.date} session={session}>
              <div id={detailsId} className="sp-blocks ex-rows">
                {blocks.map((block, index) => (
                  <ExerciseRow
                    key={cleanText(block.block_id) || `block-${index}`}
                    block={block}
                    open={openBlockIndex === index}
                    onToggle={() => setOpenBlockIndex((current) => (current === index ? null : index))}
                    openWeekIntent={openWeekIntent}
                    sourceCountdown={sourceCountdown}
                    detail={planLayout ? "sheet" : "inline"}
                    compact={planLayout}
                  />
                ))}
                <SessionChangesFoot session={session} />
              </div>
            </ExerciseSessionScope>
          ) : null}
        </>
      ) : <ExerciseSessionScope date={occurrenceDate ?? day?.date} session={session}>
        <SessionChangesFoot session={session} />
      </ExerciseSessionScope>}
    </>
  );
  return supportCard ? (
    <PlanSupportCard title={title} duration={duration} category={/breath/i.test(title) ? "Breathe" : null} optional={isOptional}>{content}</PlanSupportCard>
  ) : <article className="sp-session" data-layout={planLayout ? "plan" : undefined}>{content}</article>;
}

export function TodayCard({ day }: { day: StructuredDay }) {
  const card = day.today_card;
  const headline = humanizeIfRawEnum(cleanText(card?.headline));
  const readiness = cleanText(card?.readiness_status);
  const warning = cleanText(card?.primary_warning);
  const nutrition = cleanText(card?.nutrition_summary);
  const weightCut = cleanText(card?.weight_cut_warning);
  const mindsetLines = getSessionCoachingLines(card?.mindset_anchor);
  if (
    !headline &&
    !readiness &&
    !warning &&
    !nutrition &&
    !weightCut &&
    mindsetLines.length === 0
  ) {
    return null;
  }
  return (
    <div className="sp-today">
      {headline ? <p className="sp-today-headline">{headline}</p> : null}
      {readiness ? (
        <span className="sp-tag">{titleize(readiness)}</span>
      ) : null}
      {warning ? <p className="sp-warning">{warning}</p> : null}
      {nutrition ? <p className="sp-today-note">{nutrition}</p> : null}
      {weightCut ? <p className="sp-warning">{weightCut}</p> : null}
      <MindsetAnchorCard anchor={card?.mindset_anchor} dedupeContext={headline} />
    </div>
  );
}

/**
 * A day with no app sessions. Coach-led / sparring / technical days legitimately
 * carry no app S&C blocks, so they are rendered as a self-contained card derived
 * deterministically from the day data (see classifySessionlessDay) instead of
 * collapsing into "Rest day.". Only a genuine rest day reads as a rest day.
 */
export function SessionlessDayCard({
  day,
  showDayLabels = true,
  hideTitle = false,
}: {
  day: StructuredDay;
  /** False inside the plan accordion, where the day row already shows the
   * countdown/date; the Today tab keeps the default. */
  showDayLabels?: boolean;
  /** A sheet already titled with this day's work does not repeat it. */
  hideTitle?: boolean;
}) {
  const date = cleanText(day.date);
  const countdown = formatCountdownLabel(day.countdown_label);
  const card = day.today_card;
  const warning = cleanText(card?.primary_warning);
  const nutrition = cleanText(card?.nutrition_summary);
  const weightCut = cleanText(card?.weight_cut_warning);
  const { kind, title, tag, coachLed, converted } = classifySessionlessDay(day);
  // Keep declared and converted contact distinct.
  const isConverted = kind === "technical" && converted;
  const displayTitle =
    kind === "light_combat"
      ? DECLARED_LIGHT_COMBAT_TITLE
      : isConverted
        ? contactDisplayTitle(title)
        : title;
  const displayTag = isConverted ? TECHNICAL_COMBAT_TAG : tag;
  const sparring = kind === "sparring" ? sparringHeading(displayTitle) : null;
  const isRest = kind === "rest";
  const reducedReason = getReducedContactReason(day);
  // Every coach day reads as the same red-edged box as the Today contact block.
  const boxed = kind === "light_combat" || kind === "technical" || coachLed;

  return (
    <article className={`sp-session sp-day-card sp-day-card-${kind}`}>
      <header className="sp-session-head">
        <div>
          {showDayLabels && (countdown || date) ? (
            <div className="sp-day-labels sp-session-day-labels">
              {countdown ? <span className="sp-countdown sp-accent">{countdown}</span> : null}
              {date ? <span className="sp-day-date">{formatAppDate(date)}</span> : null}
            </div>
          ) : null}
          {hideTitle || sparring || boxed ? null : <h3 className="sp-session-title">
            {displayTitle}
          </h3>}
        </div>
        {boxed ? null : (
          <div className="sp-session-meta">
            {displayTag ? <span className="sp-tag sp-accent">{displayTag}</span> : null}
          </div>
        )}
      </header>
      {countdown === "D-0" && day.day_type === "competition" ? (
        <p className="sp-today-note">Follow your coach&apos;s warm-up and fight protocol; no additional S&amp;C.</p>
      ) : null}
      {boxed ? (
        <CoachDayBox
          tag={displayTag}
          heading={!hideTitle ? <h3 className="sp-session-title">{sparring?.title ?? (isConverted ? TECHNICAL_COMBAT_TITLE : displayTitle)}{kind === "light_combat" || kind === "technical" ? <TechnicalCombatWhyTooltip day={day} declared={!isConverted} /> : null}</h3> : null}
          tooltip={hideTitle && (kind === "light_combat" || kind === "technical") ? <TechnicalCombatWhyTooltip day={day} declared={!isConverted} /> : null}
        >
          <SparringCaution text={sparring?.caution ?? null} />
          {kind === "light_combat" || (kind === "technical" && !isConverted) ? (
            <p className="sp-today-note">{DECLARED_LIGHT_COMBAT_DESCRIPTION}</p>
          ) : isConverted ? (
            <TechnicalCombatRationale title={displayTitle} />
          ) : (
            <>
              <p className="sp-today-note">{HARD_SPARRING_SESSIONLESS_NOTE}</p>
              {reducedReason ? <p className="sp-today-note sp-contact-reason">{reducedReason}</p> : null}
            </>
          )}
        </CoachDayBox>
      ) : null}
      <PriorityMicrodoseCard day={day} />
      {warning ? <p className="sp-warning">{warning}</p> : null}
      {nutrition ? <p className="sp-today-note">{nutrition}</p> : null}
      {weightCut ? <p className="sp-warning">{weightCut}</p> : null}
      <MindsetAnchorCard
        anchor={card?.mindset_anchor}
        dedupeContext={[cleanText(card?.headline), displayTitle]}
      />
      {isRest ? <p className="sp-muted">Rest day.</p> : null}
      {date && (kind === "technical" || kind === "light_combat") ? <ExerciseSessionScope date={date} session={{
        session_id: date, session_type: "skill", title: cleanText(card?.headline) ?? displayTitle, blocks: [],
      }}><SessionChangesFoot session={{ session_id: date, session_type: "skill", blocks: [] }} /></ExerciseSessionScope> : null}
    </article>
  );
}

function LightTechnicalDayContext({
  tag, day,
}: {
  tag: string | null;
  day?: StructuredDay;
}) {
  return (
    <div className="cm-light-technical">
      <div className="cm-light-technical-head">
        {tag ? <span className="sp-tag sp-accent">{tag}</span> : null}
        <p className="sp-today-headline">{DECLARED_LIGHT_COMBAT_TITLE}<TechnicalCombatWhyTooltip day={day} declared /></p>
      </div>
      <p className="sp-today-note">{DECLARED_LIGHT_COMBAT_DESCRIPTION}</p>
    </div>
  );
}

/**
 * Coach-owned contact (declared / downgraded sparring) shown above the day's app
 * sessions so the two coexist in one card instead of the sparring day silently
 * disappearing behind the low-RPE app work scheduled on the same day.
 */
function CoachLedDayContext({
  title,
  tag,
  kind,
  converted,
  headline,
  reducedReason,
  day,
}: {
  title: string;
  tag: string | null;
  kind: SessionlessDayKind;
  converted: boolean;
  headline?: string;
  reducedReason?: string | null;
  day?: StructuredDay;
}) {
  const isLightCombat = kind === "light_combat";
  // Preserve declared versus converted day labels.
  const isTechnical = kind === "technical" && converted;
  const isDeclaredTechnical = kind === "technical" && !converted;
  const displayTitle = isLightCombat
    ? DECLARED_LIGHT_COMBAT_TITLE
    : isTechnical
      ? contactDisplayTitle(title)
      : title;
  const displayTag = isTechnical ? TECHNICAL_COMBAT_TAG : tag;
  const sparring = kind === "sparring" ? sparringHeading(displayTitle) : null;
  const repeatsHeadline = Boolean(
    headline && displayTitle.trim().toLocaleLowerCase() === headline.trim().toLocaleLowerCase(),
  );
  const description = isLightCombat || isDeclaredTechnical
    ? DECLARED_LIGHT_COMBAT_DESCRIPTION
    : HARD_SPARRING_CONTACT_NOTE;

  return (
    <div className="cm-light-technical cm-coach-led-contact">
      <div className="cm-light-technical-head">
        {displayTag ? <span className="sp-tag sp-accent">{displayTag}</span> : null}
        {repeatsHeadline && !sparring ? (
          isTechnical || isLightCombat || isDeclaredTechnical ? <TechnicalCombatWhyTooltip day={day} declared={!isTechnical} /> : null
        ) : (
          <p className="sp-today-headline">
            {sparring?.title ?? displayTitle}
            {isTechnical || isLightCombat || isDeclaredTechnical ? <TechnicalCombatWhyTooltip day={day} declared={!isTechnical} /> : null}
          </p>
        )}
      </div>
      <SparringCaution text={sparring?.caution ?? null} />
      {isTechnical ? (
        <TechnicalCombatRationale title={displayTitle} />
      ) : (
        <p className="sp-today-note">{description}</p>
      )}
      {reducedReason && !isTechnical && !isLightCombat && !isDeclaredTechnical ? (
        <p className="sp-today-note sp-contact-reason">{reducedReason}</p>
      ) : null}
    </div>
  );
}

export function DaySessionContext({ day, headline }: { day: StructuredDay; headline?: string }) {
  const card = day.today_card;
  const warning = cleanText(card?.primary_warning);
  const nutrition = cleanText(card?.nutrition_summary);
  const weightCut = cleanText(card?.weight_cut_warning);
  const priorityMicrodose = getPriorityMicrodose(day);
  const sessionlessDay = classifySessionlessDay(day);
  const lightTechnicalContext = sessionlessDay.kind === "light_combat";
  const contactView = getCoachLedContactView(day);
  // A declared light-combat slot the plan already lists as its own session card
  // needs no second contact block above it.
  const sessionShowsLightCombat = getSessions(day).some((session) => {
    const title = stripLeadingWeekday(cleanText(session.title) ?? "").replace(/^D[-−]\d+\s*[—–:-]\s*/i, "");
    return isDeclaredLightCombatTitle(title) || /^Technical[ -]only combat$/i.test(title);
  });
  const coachLedContact =
    contactView && !(["light_combat", "technical"].includes(contactView.kind) && sessionShowsLightCombat)
      ? contactView
      : null;
  // The contact block already names the day's combat work; a light-combat
  // headline on top of it would show the same session twice.
  const showLightTechnicalContext = lightTechnicalContext && !coachLedContact && !sessionShowsLightCombat;
  // Session-level anchors are the most specific coaching cue in this schema, so
  // keep them and suppress the broader day anchor whenever any session owns one.
  // The day anchor remains the fallback when none of the sessions has a usable
  // coaching cue.
  const dayMindset = card?.mindset_anchor;
  const hasSessionMindset = getSessions(day).some(
    (session) => getSessionCoachingLines(session.mindset_anchor).length > 0,
  );
  const hasDayMindset = !hasSessionMindset && getSessionCoachingLines(dayMindset).length > 0;
  // The day mindset's Context often restates a session's objective shown just
  // below it (same sentence, twice on screen); drop it against those.
  const dayObjectives = getSessions(day).map((session) => cleanText(session.objective));
  const hasDayContext = Boolean(
    warning ||
      nutrition ||
      weightCut ||
      priorityMicrodose ||
      showLightTechnicalContext ||
      coachLedContact ||
      hasDayMindset,
  );
  if (!hasDayContext) {
    return null;
  }

  return (
    <div className={`cm-day-context${coachLedContact ? " cm-day-context-contact" : ""}`}>
      {showLightTechnicalContext ? (
        <LightTechnicalDayContext tag={sessionlessDay.tag} day={day} />
      ) : null}
      {coachLedContact ? (
        <CoachLedDayContext
          title={coachLedContact.title}
          tag={coachLedContact.tag}
          kind={coachLedContact.kind}
          converted={coachLedContact.converted}
          headline={headline}
          reducedReason={getReducedContactReason(day)}
          day={day}
        />
      ) : null}
      {priorityMicrodose ? <PriorityMicrodoseCard day={day} /> : null}
      {warning ? <p className="sp-warning">{warning}</p> : null}
      {nutrition ? <p className="sp-today-note">{nutrition}</p> : null}
      {weightCut ? <p className="sp-warning">{weightCut}</p> : null}
      {hasDayMindset ? <MindsetAnchorCard anchor={dayMindset} dedupeContext={dayObjectives} /> : null}
    </div>
  );
}

/** Short weekday name from an ISO date string ("2026-06-19" -> "Fri"), or null. */
function weekdayLabel(date: string | null): string | null {
  if (!date) {
    return null;
  }
  const parsed = new Date(`${date.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  const weekdays = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  return weekdays[parsed.getDay()];
}

function openTimelineDayLabel(
  day: StructuredDay,
  weekNumber: number,
  fallbackLabel: string,
): string {
  const date = cleanText(day.date);
  if (date) {
    return formatAppDate(date).split(" ").slice(0, 3).join(" ").toUpperCase();
  }
  const weekday = cleanText(day.weekday)?.toUpperCase();
  return weekday ? `WEEK ${weekNumber} · ${weekday}` : fallbackLabel;
}

/** One row of the week's day timeline: a real plan day (with its original index
 * in the week's day list, which the current/default-open logic is keyed on) or a
 * synthesized gap day that fills a hole in the countdown. */
export type TimelineEntry =
  | { kind: "day"; day: StructuredDay; index: number }
  | { kind: "gap"; dateIso: string; weekday: string | null; countdown: string | null };

const COUNTDOWN_LABEL_RE = /^D-(\d+)$/i;

function parseIsoDay(iso: string | null | undefined): Date | null {
  const clean = cleanText(iso ?? null);
  if (!clean) {
    return null;
  }
  const parsed = new Date(`${clean.slice(0, 10)}T00:00:00`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

const ISO_DAY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

function toLocalIsoDay(date: Date): string {
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

/** Gap rows between two consecutive plan days. Fails closed: no rows unless
 * both dates parse and sit 2–7 calendar days apart (in-order, sane data), and
 * no countdown number unless both neighbours' labels parse as D-n and descend
 * exactly one per day — a weekday-only rest row beats a wrong number. */
function gapRowsBetween(prev: StructuredDay, next: StructuredDay): TimelineEntry[] {
  const prevDate = parseIsoDay(prev.date);
  const nextDate = parseIsoDay(next.date);
  if (!prevDate || !nextDate) {
    return [];
  }
  const diff = Math.round((nextDate.getTime() - prevDate.getTime()) / 86_400_000);
  if (diff < 2 || diff > 7) {
    return [];
  }
  const prevMatch = formatCountdownLabel(prev.countdown_label)?.match(COUNTDOWN_LABEL_RE);
  const nextMatch = formatCountdownLabel(next.countdown_label)?.match(COUNTDOWN_LABEL_RE);
  const prevN = prevMatch ? Number(prevMatch[1]) : null;
  const nextN = nextMatch ? Number(nextMatch[1]) : null;
  const countdownConsistent = prevN != null && nextN != null && prevN - diff === nextN;
  const rows: TimelineEntry[] = [];
  for (let offset = 1; offset < diff; offset += 1) {
    const gapDate = new Date(prevDate);
    gapDate.setDate(gapDate.getDate() + offset);
    const dateIso = toLocalIsoDay(gapDate);
    rows.push({
      kind: "gap",
      dateIso,
      weekday: weekdayLabel(dateIso),
      countdown: countdownConsistent ? `D-${prevN! - offset}` : null,
    });
  }
  return rows;
}

/** The selected week's days with rest rows filling intra-week date holes, so
 * the countdown reads continuous (D-10, D-9, D-8…) instead of skipping. Open
 * (weekday-only) plans carry no dates, so gap fill is disabled there. */
export function buildDayTimeline(days: StructuredDay[], enabled: boolean): TimelineEntry[] {
  const timeline: TimelineEntry[] = [];
  days.forEach((day, index) => {
    if (enabled && index > 0) {
      timeline.push(...gapRowsBetween(days[index - 1], day));
    }
    timeline.push({ kind: "day", day, index });
  });
  return timeline;
}

/** A plan day that is genuinely just rest — no sessions, classified as rest,
 * and no day-card context worth expanding — so it can render as a compact
 * non-interactive rest row identical to the synthesized gap rows. */
function isPlainRestDay(day: StructuredDay): boolean {
  if (getSessions(day).length > 0 || classifySessionlessDay(day).kind !== "rest") {
    return false;
  }
  const card = day.today_card;
  // Neutral calendar placeholders use the same compact row as a camp gap.
  // A prescribed recovery day or coach-owned contact keeps its full context.
  const neutral = day.day_type === "rest"
    && /^(?:unavailable|no (?:planned session|session planned))$/i.test(cleanText(card?.headline) ?? "")
    && !cleanText(card?.coach_led_contact);
  return !(
    (!neutral && cleanText(card?.headline)) ||
    cleanText(card?.primary_warning) ||
    cleanText(card?.nutrition_summary) ||
    cleanText(card?.weight_cut_warning) ||
    (!neutral && getSessionCoachingLines(card?.mindset_anchor).length > 0)
  );
}

/** Widest countdown at which an unprogrammed day is deliberate taper rest.
 * Inside fight week the session budget caps active work on purpose, so a day
 * with nothing scheduled is a planned rest day rather than missing data. */
const TAPER_REST_MAX_COUNTDOWN = 7;

/** How a synthesized countdown-gap row should read.
 *
 * Further out, a hole in a sparse plan could be rest, coach-led work, or simply
 * undocumented, so it keeps the honest "No planned session". Inside fight week
 * the plan's own budgets deliberately leave days unprogrammed, so the day is
 * named as the taper rest it is and given the reason — an athlete opening the
 * app on D-2 should read intent, not a blank. */
function gapDayLabel(countdown: string | null): { label: string; reason: string | null } {
  const match = cleanText(countdown)?.match(COUNTDOWN_LABEL_RE);
  const daysOut = match ? Number(match[1]) : null;
  if (daysOut != null && daysOut > 0 && daysOut <= TAPER_REST_MAX_COUNTDOWN) {
    return {
      label: "Taper rest",
      reason: "Planned rest to protect freshness for fight day.",
    };
  }
  return { label: "No planned session", reason: null };
}

/** Compact non-interactive rest row: same columns as a day summary (countdown,
 * weekday, optional current marker) with a right-aligned label where the session
 * count would sit. Deliberately a div, not a <details> — there is nothing to
 * expand, so it must not advertise an affordance.
 *
 * `label` distinguishes the sources: a backend-classified rest day reads
 * "Rest", while a synthesized countdown-gap row reads either "Taper rest" with
 * a reason (inside fight week, where the empty day is deliberate) or the honest
 * "No planned session" — it must not assert rest the data can't back. */
function RestDayRow({
  countdown,
  weekday,
  label = "Rest",
  reason = null,
  isCurrent,
  currentLabel = "Today",
  dayIndex,
}: {
  countdown: string | null;
  weekday: string | null;
  label?: string;
  reason?: string | null;
  isCurrent?: boolean;
  currentLabel?: string;
  dayIndex?: number;
}) {
  const fightWeekSessionless = /^D-[0-7]$/i.test(countdown ?? "");
  return (
    <div
      className={`sp-week cm-day cm-rest-day${fightWeekSessionless ? " cm-fight-week-sessionless" : ""}${isCurrent ? " cm-day-current" : ""}`}
      data-day-index={dayIndex}
      tabIndex={dayIndex != null ? -1 : undefined}
    >
      <span className="cm-day-head">
        {countdown ? (
          <span className={`sp-countdown cm-day-countdown${isCurrent ? " sp-accent" : ""}`}>
            {countdown}
          </span>
        ) : null}
        {weekday ? <span className="sp-week-title cm-day-title">{weekday}</span> : null}
        {isCurrent ? <span className="cm-day-now">{currentLabel}</span> : null}
        {reason ? <span className="cm-rest-reason">{reason}</span> : null}
      </span>
      <span className="cm-rest-label" data-neutral={/^(?:unavailable|no session planned)$/i.test(label) || undefined}>{label}</span>
    </div>
  );
}

function customDaySessionHref(day: StructuredDay, customPlanId?: string | null): string | null {
  if (!customPlanId || !["rest", "recovery"].includes(day.day_type ?? "")) return null;
  const weekday = CALENDAR_WEEKDAYS.find(value => value.toLowerCase() === (cleanText(day.weekday) || weekdayLabel(day.date ?? null))?.slice(0, 3).toLowerCase());
  return weekday ? `/plans/custom?plan=${encodeURIComponent(customPlanId)}&addSession=${weekday}` : null;
}

function CompletionTag({ completion }: { completion: Completion }) {
  if (completion.total === 0) {
    return null;
  }
  const done = completion.done >= completion.total && completion.total > 0;
  // "Done" is a positive, completed state — give it a calm success tone rather
  // than the brand red, which we keep reserved for current action / risk. The
  // completion wording keeps the closed row understandable at a glance.
  return (
    <span className={`cm-day-count${done ? " cm-day-count-done" : ""}`}>
      {done ? "✓ " : ""}
      {completion.done} of {completion.total} complete
    </span>
  );
}

/**
 * One day of the selected week, as a collapsible card. Collapsed by default
 * except the current/selected day so the athlete lands on what matters today;
 * the current day's session blocks are expanded too. Session-less days reuse the
 * deterministic coach-led / rest classification.
 */
type DayCompletionOptions = {
  completionIndex?: CompletionIndex;
  currentTrainingDayIso?: string | null;
  planStartIso?: string | null;
  onLogSession?: (day: StructuredDay, session: StructuredSession, sessionId: string) => void;
};

/** One session's live status and retro-log action, shared by the list day card
 * and the calendar's selected-day panel so both log the same way. */
function sessionCompletionInfo(
  day: StructuredDay,
  session: StructuredSession,
  { completionIndex, currentTrainingDayIso, planStartIso, onLogSession }: DayCompletionOptions,
): SessionCompletionInfo | undefined {
  if (!completionIndex || session.optional === true) {
    return undefined;
  }
  const storedDate = cleanText(day.date);
  const dayIso = storedDate ? storedDate.slice(0, 10) : null;
  const record = completionForSession(completionIndex, day, session);
  const sessionId = completionSessionId(day, session);
  // A session with no completion identity (id-less secondary) stays neutral.
  if (!sessionId) {
    return undefined;
  }
  const display = getSessionDisplayStatus(record, dayIso, currentTrainingDayIso ?? null, planStartIso ?? null);
  const terminal = display.state === "done" || display.state === "modified" || display.state === "skipped";
  const canLog =
    Boolean(onLogSession) && !terminal && canRetroLog(dayIso, currentTrainingDayIso ?? null, planStartIso ?? null);
  return {
    display,
    completion: record,
    onLog: canLog ? () => onLogSession?.(day, session, sessionId) : undefined,
  };
}

/** The calendar's selected day: every session on it as a compact row, each
 * opening the same content the list shows in a focused sheet, so the athlete
 * explores by date without leaving the calendar. */
function CalendarDayPanel({
  day,
  completionIndex,
  currentTrainingDayIso,
  planStartIso,
  onLogSession,
  openOngoing,
  weekNumber,
  addSessionHref,
}: DayCompletionOptions & { day: StructuredDay; openOngoing: boolean; weekNumber: number; addSessionHref?: string | null }) {
  const rehabLabelPolicy = useContext(RehabLabelContext);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const sessions = getSessions(day);
  const iso = cleanText(day.date)?.slice(0, 10) ?? null;
  const heading = iso ? formatAppDate(iso).replace(/\s\d{4}$/, "") : "Training day";
  const countdown = formatCountdownLabel(day.countdown_label);
  const completion = dayCompletion(day, completionIndex);
  const completionOptions = { completionIndex, currentTrainingDayIso, planStartIso, onLogSession };
  const weekIntent = openOngoing ? openBlockWeekIntent(weekNumber, sessions.some(s => s.prescription_source === "athlete")) : null;
  // Coach-owned work has no app card: a sessionless coach day is one row, and a
  // coach-led contact block beside app work gets its own row ahead of it.
  const sessionless = sessions.length === 0 ? classifySessionlessDay(day) : null;
  const contact = sessions.length > 0 ? getCoachLedContactView(day) : null;
  const restOnly = sessions.length === 0 && sessionless?.kind === "rest";
  const coachTitle = restOnly ? null : sessionless?.title ?? contact?.title ?? null;
  type Item = { key: string; title: string; detail: string | null; glyph: ExerciseGlyphName; session?: StructuredSession };
  const items: Item[] = [
    ...(coachTitle ? [{ key: "coach", title: coachTitle, detail: null, glyph: "strength" as const }] : []),
    ...sessions.map((session, index): Item => {
      const title = humanizeIfRawEnum(stripLeadingWeekday(cleanText(session.title) || titleize(cleanText(session.session_type) || "Session")));
      const duration = formatMeasured(session.planned_duration);
      const status = sessionCompletionInfo(day, session, completionOptions)?.display.label;
      const blocks = getBlocks(session);
      return {
        key: cleanText(session.session_id) || `session-${index}`,
        title,
        detail: [
          mentalSessionCategory(session) || resolveSessionTypeLabel(session, rehabLabelPolicy) || null,
          duration ? compactTimeUnits(duration) : null,
          blocks.length > 0 ? `${blocks.length} ${blocks.length === 1 ? "exercise" : "exercises"}` : null,
          status || null,
        ].filter(Boolean).join(" · ") || "Open session",
        glyph: blocks[0] ? exerciseGlyph(blocks[0]) : "skill",
        session,
      };
    }),
  ];
  const opened = items.find(item => item.key === openKey) ?? null;
  const coachOnly = sessions.length === 0 && !restOnly;

  return (
    <section className="sp-card cm-cal-day" aria-label={`${heading} sessions`}>
      <header className="cm-cal-day-head">
        <h3 className="cm-cal-day-title">
          {heading}
          {countdown ? <span className="cm-cal-day-countdown">{countdown}</span> : null}
        </h3>
        {restOnly ? (
          <span className="cm-cal-day-kind"><span className="cm-cal-dot" data-state="rest" aria-hidden="true" />{day.day_type === "recovery" ? "Recovery day" : "Rest day"}</span>
        ) : coachOnly ? (
          <span className="cm-cal-day-kind"><span className="cm-cal-dot" data-state="coach" aria-hidden="true" />Coach / gym day</span>
        ) : completion.total > 0 ? <CompletionTag completion={completion} /> : null}
      </header>
      <p className="cm-cal-day-count">{items.length} {items.length === 1 ? "session" : "sessions"}</p>
      <ul className="cm-cal-day-list">
        {items.map(item => (
          <li key={item.key}>
            <button type="button" className="cm-cal-day-row" aria-haspopup="dialog" onClick={() => setOpenKey(item.key)}>
              <span className="cm-cal-day-icon" aria-hidden="true"><ExerciseGlyphIcon name={item.glyph} /></span>
              <span className="cm-cal-day-text">
                <span className="cm-cal-day-name">{item.title}</span>
                {item.detail ? <span className="cm-cal-day-detail">{item.detail}</span> : null}
              </span>
              <span className="cm-cal-day-chevron" aria-hidden="true" />
            </button>
          </li>
        ))}
      </ul>
      {addSessionHref ? <Link className="secondary-button" href={addSessionHref}>+ Add weekly session</Link> : null}
      {opened ? (
        <InjuryDetailSheet title={opened.title} onClose={() => setOpenKey(null)}>
          <div className="injury-sheet-body cm-cal-day-sheet">
            {opened.session ? (
              <SessionCard
                session={opened.session}
                day={day}
                sourceCountdown={day.countdown_label}
                initiallyOpenFirstBlock={false}
                showDayContext={false}
                showDayLabels={false}
                completionInfo={sessionCompletionInfo(day, opened.session, completionOptions)}
                openWeekIntent={weekIntent}
                occurrenceDate={day.date}
                layout="plan"
                hideTitle
              />
            ) : coachOnly ? (
              <SessionlessDayCard day={day} showDayLabels={false} hideTitle />
            ) : (
              <DaySessionContext day={day} headline={opened.title} />
            )}
          </div>
        </InjuryDetailSheet>
      ) : null}
    </section>
  );
}

export function CampDayCard({
  day,
  displayDate,
  fallbackLabel,
  openOngoing = false,
  weekNumber = 1,
  isCurrent,
  isNext = false,
  currentLabel = "Today",
  defaultOpen,
  completionIndex,
  currentTrainingDayIso,
  planStartIso,
  onLogSession,
  dayIndex,
  openRequest,
  addSessionHref,
}: {
  day: StructuredDay;
  /** Truthful calendar date for a recurring weekday match. The stored projected
   * date remains untouched for completion and logging identity. */
  displayDate?: string | null;
  fallbackLabel?: string;
  openOngoing?: boolean;
  weekNumber?: number;
  isCurrent?: boolean;
  isNext?: boolean;
  /** Badge text for the highlighted day — "Today" normally, "Next session" once
   * the view has advanced past a logged today's session. */
  currentLabel?: string;
  defaultOpen?: boolean;
  /** Live completion rows keyed by training_day|session_id. When present the
   * session cards show real done/modified/skipped/missed status. */
  completionIndex?: CompletionIndex;
  /** Server-authoritative athlete-local day (drives Missed + the retro window). */
  currentTrainingDayIso?: string | null;
  /** Local day the plan was created; earlier sessions are never Missed. */
  planStartIso?: string | null;
  /** Opens the retro-log form for a past, still-loggable session. */
  onLogSession?: (day: StructuredDay, session: StructuredSession, sessionId: string) => void;
  /** Position in the week's day list; lets the calendar scroll to this card. */
  dayIndex?: number;
  /** Explicit calendar/Today navigation reopens even a manually closed day. */
  openRequest?: object;
  addSessionHref?: string | null;
}) {
  const [open, setOpen] = useState<boolean>(Boolean(defaultOpen));
  const userToggledOpen = useRef(false);

  useEffect(() => {
    if (!userToggledOpen.current) {
      setOpen(Boolean(defaultOpen));
    }
  }, [defaultOpen]);
  useEffect(() => {
    if (openRequest) setOpen(true);
  }, [openRequest]);

  const sessions = getSessions(day);
  const storedDate = cleanText(day.date);
  const date = cleanText(displayDate) || storedDate;
  const weekday = weekdayLabel(date);
  const countdown = formatCountdownLabel(day.countdown_label);
  const timelineLabel = openOngoing
    ? openTimelineDayLabel(
        displayDate ? { ...day, date: displayDate } : day,
        weekNumber,
        fallbackLabel || `Week ${weekNumber} training day`,
      )
    : weekday || date || fallbackLabel || "Training day";
  const weekIntent = openOngoing ? openBlockWeekIntent(weekNumber, sessions.some(s => s.prescription_source === "athlete")) : null;
  const completion = dayCompletion(day, completionIndex);

  const completionInfoFor = (session: StructuredSession) =>
    sessionCompletionInfo(day, session, { completionIndex, currentTrainingDayIso, planStartIso, onLogSession });

  return (
    <details
      className={`sp-week cm-day${isCurrent ? " cm-day-current" : ""}${isNext ? " cm-day-next" : ""}`}
      data-day-index={dayIndex}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary
        className="sp-week-summary cm-day-summary"
        onClick={() => {
          userToggledOpen.current = true;
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            userToggledOpen.current = true;
          }
        }}
      >
        <span className="cm-day-head">
          {countdown ? (
            <span className={`sp-countdown cm-day-countdown${isCurrent ? " sp-accent" : ""}`}>
              {countdown}
            </span>
          ) : null}
          <span className="sp-week-title cm-day-title">{timelineLabel}</span>
          {isCurrent ? <span className="cm-day-now">{currentLabel}</span> : null}
          {isNext ? <span className="cm-day-next-label">Next session</span> : null}
        </span>

        <span className="cm-day-meta">
          <CompletionTag completion={completion} />
          {completion.total === 0 && sessions.length > 0 ? (
            // Support-only day (a visualisation, a Tactical Watch): it has work
            // to open but nothing physical to count, so name it rather than
            // leave the row's count slot blank.
            <span className="cm-day-count cm-day-support">
              Zero load
              <span className="sr-only"> support only, no physical sessions</span>
            </span>
          ) : null}
        </span>
      </summary>

      <div className="sp-week-body">
        {sessions.length > 0 ? <DaySessionContext day={day} /> : null}

        {sessions.length > 0 ? (
          <div className="sp-sessions">
            {sessions.map((session, index) => (
              <SessionCard
                key={cleanText(session.session_id) || `session-${index}`}
                session={session}
                day={index === 0 ? day : undefined}
                sourceCountdown={day.countdown_label}
                initiallyOpenFirstBlock={false}
                showDayContext={false}
                showDayLabels={false}
                completionInfo={completionInfoFor(session)}
                openWeekIntent={weekIntent}
                occurrenceDate={day.date}
                layout="plan"
              />
            ))}
          </div>
        ) : (
          <SessionlessDayCard day={day} showDayLabels={false} />
        )}
        {addSessionHref ? <Link className="secondary-button" href={addSessionHref}>+ Add weekly session</Link> : null}
      </div>
    </details>
  );
}

function getSafetyPriorityTexts(plan: StructuredPlan): string[] {
  const rules = getDisplayableRedFlags(plan);
  if (rules.length > 0) {
    return rules.flatMap((rule) =>
      [cleanText(rule.display_text), cleanText(rule.action)].filter(
        (text): text is string => text !== null,
      ),
    );
  }
  return getFallbackSafetyNotes(plan)
    .map((note) => cleanText(note.text))
    .filter((text): text is string => text !== null);
}

// Plan-level notes ("Plan notes"): the short, always-on reminders (weight cut,
// injury, nutrition, general non-negotiables) that live outside any week. Kept
// as a standalone card near the top so this context is not lost in the
// structured view the way it would be if it only existed in the raw text.
export function ActiveNotesCard({ plan }: { plan: StructuredPlan }) {
  // Drop any note that just restates a red-flag rule — the Red Flags card is the
  // single home for stop/report rules, so Active Notes stays context-only.
  const safetyPriorityTexts = getSafetyPriorityTexts(plan);
  const notes = getActiveNotesExcludingRedFlags(plan).flatMap((note) => {
    const text = stripSafetyOwnedClause(cleanText(note.text) || "", safetyPriorityTexts);
    return text ? [{ ...note, text }] : [];
  });
  const [open, setOpen] = useState(false);
  if (notes.length === 0) {
    return null;
  }
  // Current injury, medical and weight-cut restrictions stay on the card face,
  // never folded; the rest of the context sits behind the tap.
  const restrictions = notes.filter(isActiveRestrictionNote);
  const context = notes.filter(note => !restrictions.includes(note));
  const renderNotes = (items: typeof notes) => (
    <ul className="sp-note-list">
      {items.map((note, index) => (
        <li key={`${note.category}-${index}`} className={`sp-note sp-note-${note.category}`}>
          <span className="sp-note-label">{planNoteLabel(note)}</span>
          <span className="sp-note-text">{note.text}</span>
        </li>
      ))}
    </ul>
  );
  const icon = (
    <span className="sp-plan-notes-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <rect x="5" y="4" width="14" height="17" rx="2" />
        <path d="M9 4h6v3H9zM9 11h6M9 15h4" />
      </svg>
    </span>
  );
  const urgent = restrictions.length > 0
    ? <div className="sp-plan-notes-urgent">{renderNotes(restrictions)}</div> : null;
  // Restriction-only plans have nothing to fold: a static heading, no arrow.
  if (context.length === 0) {
    return (
      <section className="sp-collapse sp-active-notes" aria-label="Plan notes">
        <header className="sp-collapse-summary sp-plan-notes-summary sp-plan-notes-static">
          {icon}
          <span className="sp-plan-notes-text"><h3 className="sp-collapse-title">Plan notes</h3></span>
        </header>
        {urgent}
      </section>
    );
  }
  return (
    <section className="sp-collapse sp-active-notes" aria-label="Plan notes">
    <details
      className="sp-plan-notes-details"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="sp-collapse-summary sp-plan-notes-summary">
        {icon}
        <span className="sp-plan-notes-text">
          <span className="sp-collapse-title">Plan notes</span>
          <span className="sp-plan-notes-meta">
            {`${notes.length} ${notes.length === 1 ? "note" : "notes"} · Training limits & goals`}
          </span>
        </span>
        <span className="sp-collapse-action">{open ? "Hide" : "Show"}</span>
      </summary>
      <div className="sp-collapse-body">
        {context.length > 0 ? renderNotes(context) : null}
      </div>
    </details>
      {/* Outside the disclosure: a current restriction is always seen. */}
      {urgent}
    </section>
  );
}

const URGENT_NOTE_CATEGORIES = new Set(["injury", "medical", "post_op", "weight_cut"]);
// "No weight-cut target is assumed" shares the category but restricts nothing.
// Only a whole note that states the absence of a target/limit/injury counts.
const INFORMATIONAL_NOTE = /^(?:(?:the )?target weight (?:is )?not set|no (?:weight[- ]cut )?(?:targets?|limits?|restrictions?|injur(?:y|ies)|medical (?:issue|condition)s?) (?:is |are )?(?:assumed|declared|reported|set|applied)|weight[- ]cut (?:is )?not (?:applicable|required|planned)|none (?:declared|reported))\.?$/i;
// Any of these words means the note asks the athlete to do or avoid something.
const RESTRICTIVE_WORDING = /\b(?:avoid|do not|don't|never|stop|limit|restrict|until|only|must|cleared?|clearance|max(?:imum)?|cap|reduce|keep|stay|follow|within|under|no contact|no sparring|hold)\b/i;

/** A note the athlete must act on now: an injury, medical or weight-cut
 * category, unless its whole text is plainly informational. Uncertain wording
 * stays visible. */
function isActiveRestrictionNote(note: { category?: string | null; text: string }): boolean {
  if (!URGENT_NOTE_CATEGORIES.has(cleanText(note.category) ?? "")) return false;
  const text = note.text.trim();
  return !INFORMATIONAL_NOTE.test(text) || RESTRICTIVE_WORDING.test(text);
}

/** Map a severity label ("Red", "Amber", "High", "Low"…) to a tone class so the
 *  badge colour carries the meaning. Unknown severities stay neutral. */
function severityToneClass(label: string | null): string {
  if (!label) {
    return "";
  }
  const l = label.toLowerCase();
  if (/\b(red|high|critical|severe)\b/.test(l)) {
    return "sp-sev-red";
  }
  if (/\b(amber|orange|moderate|medium|elevated)\b/.test(l)) {
    return "sp-sev-amber";
  }
  if (/\b(green|low|mild|minor)\b/.test(l)) {
    return "sp-sev-green";
  }
  return "";
}

export function RedFlagsCard({ plan }: { plan: StructuredPlan }) {
  const rules = getDisplayableRedFlags(plan);
  const fallbackNotes = rules.length === 0 ? getFallbackSafetyNotes(plan) : [];
  const hasStopRules = rules.length > 0 || fallbackNotes.length > 0;
  // Collapsed by default so the sessions sit higher; the "stop & report" title
  // stays on screen so the rules are always one tap away. The athlete's own
  // active notes render open beside it. The safety/medical disclaimer is folded
  // in here so safety lives in one block, with the stop/report rules above it.
  const [open, setOpen] = useState(false);
  return (
    <details
      className="sp-collapse sp-redflags"
      aria-label="Red flags and safety actions"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="sp-collapse-summary sp-redflags-summary">
        <span className="sp-redflags-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 3 5 6v5c0 4.5 3 8.3 7 10 4-1.7 7-5.5 7-10V6l-7-3Z" />
            <path d="M12 8v4.5M12 16h.01" />
          </svg>
        </span>
        <span className="sp-redflags-summary-text">
          <span className="sp-eyebrow">Safety priority</span>
          <span className="sp-collapse-title">Red flags - stop &amp; report</span>
        </span>
        <span className="sp-collapse-action">{open ? "Hide" : "Show"}</span>
      </summary>
      <div className="sp-collapse-body">
      {hasStopRules ? (
      <ul className="sp-redflag-list">
        {rules.length > 0 ? rules.map((rule, index) => {
          const { text, action, severityLabel } = redFlagView(rule);
          // A weight-cut symptom rule is always shown, but its secondary text is
          // softened when cut risk is EXPLICITLY below moderate so it does not
          // lead. An explicit red/critical/high severity is never softened (the
          // severity is passed so it overrides), and the severity badge itself
          // always stays full-strength (see the CSS — only text is muted).
          const deEmphasised = isDeEmphasisedWeightCutSafety(plan, text, rule.severity as string);
          return (
            <li
              key={cleanText(rule.rule_id) || `flag-${index}`}
              className={`sp-redflag${deEmphasised ? " sp-redflag-deemphasised" : ""}`}
            >
              <div className="sp-redflag-head">
                <span className="sp-redflag-kicker">Stop signal</span>
                {severityLabel ? (
                  <span
                    className={`sp-tag sp-redflag-badge ${severityToneClass(severityLabel)}`.trim()}
                  >
                    {severityLabel}
                  </span>
                ) : null}
              </div>
              {text ? <span className="sp-redflag-text">{text}</span> : null}
              {action ? <p className="sp-muted">{action}</p> : null}
            </li>
          );
        }) : fallbackNotes.map((note, index) => {
          const deEmphasised = isDeEmphasisedWeightCutSafety(plan, note.text);
          return (
          <li
            key={`${note.category}-${index}`}
            className={`sp-redflag${deEmphasised ? " sp-redflag-deemphasised" : ""}`}
          >
            <div className="sp-redflag-head">
              <span className="sp-redflag-kicker">Safety note</span>
              <span className="sp-tag sp-redflag-badge">{planNoteLabel(note)}</span>
            </div>
            <span className="sp-redflag-text">{note.text}</span>
          </li>
          );
        })}
      </ul>
      ) : null}
      <SafetyNote tone="warning" showRedFlags>{PLAN_SAFETY_NOTE}</SafetyNote>
      </div>
    </details>
  );
}

/** Threshold at which the week rail switches from "share the width evenly" to
 * "hold a readable width and scroll". Up to this many weeks fit the strip
 * without scrolling; beyond it the rail shows ~this many per viewport. */
const WEEK_STRIP_FIT_LIMIT = 4;

/** Horizontal scroll offset that centres a week card within the rail, clamped so
 * the first cards never scroll past the start. Pure for direct testing — jsdom
 * reports zero element geometry, so the centring math is verified here rather
 * than through a rendered layout. */
export function weekStripCenterOffset(
  stripWidth: number,
  pillOffset: number,
  pillWidth: number,
): number {
  return Math.max(0, pillOffset - (stripWidth - pillWidth) / 2);
}

/** Progression rail of week cards used to pick the week. Up to four weeks share
 * the strip width evenly (no scroll); five or more hold a readable ~quarter
 * width and the rail scrolls with snap, auto-centring the active week. */
function WeekStrip({
  weeks,
  selectedPos,
  currentPos,
  onSelect,
  completionIndex,
  openOngoing,
  controls,
  label,
}: {
  weeks: StructuredWeek[];
  selectedPos: number;
  currentPos: number | null;
  onSelect: (pos: number) => void;
  completionIndex?: CompletionIndex;
  openOngoing: boolean;
  /** Extra controls for the navigation row (the List / Calendar switch). */
  controls?: ReactNode;
  /** Navigation heading; an open block names its block ("Block 1 · Week 2 of 4"). */
  label?: string;
}) {
  const stripRef = useRef<HTMLElement>(null);
  const scrollable = weeks.length > WEEK_STRIP_FIT_LIMIT;
  // Centre on the week being viewed. `selectedPos` (the parent's `safePos`)
  // already defaults to the current week until the athlete picks another, so it
  // covers "land on now" on mount AND re-centres each manual selection — whereas
  // preferring `currentPos` would pin the target and never move when a different
  // week is tapped, leaving that card half cut off.
  const activePos = selectedPos;

  // Bring the active week into view when the rail scrolls. Adjusts only the
  // horizontal scroll of the strip itself (never scrollIntoView, which could
  // jump the whole page vertically). No-ops when the rail is not scrollable or
  // has no layout yet (SSR / jsdom report zero widths).
  useEffect(() => {
    const strip = stripRef.current;
    if (!strip || strip.scrollWidth <= strip.clientWidth) {
      return;
    }
    const pill = strip.querySelector<HTMLElement>(`[data-week-pos="${activePos}"]`);
    if (!pill) {
      return;
    }
    strip.scrollLeft = weekStripCenterOffset(strip.clientWidth, pill.offsetLeft, pill.clientWidth);
  }, [activePos, weeks.length]);

  return (
    <>
    <div className="cm-week-navigation">
      {/* The selected square already says which week this is, and Jump to
          Today returns to now; only an open block names its block here. */}
      {label ? (
        <p className="cm-week-navigation-label" aria-live="polite" aria-atomic="true">{label}</p>
      ) : (
        <p className="sr-only" aria-live="polite" aria-atomic="true">{`Week ${selectedPos + 1} of ${weeks.length}`}</p>
      )}
      <div className="cm-week-nav-actions">{controls}</div>
    </div>
    <nav
      ref={stripRef}
      className="cm-week-strip"
      data-scroll={scrollable ? "true" : undefined}
      aria-label={openOngoing ? "Training block weeks" : "Camp weeks"}
    >
      {weeks.map((week, pos) => {
        // This compact fraction is a progress action: count the same app sessions
        // as the week overview, not coach-owned days that cannot be logged here.
        const completion = weekCompletion(week, completionIndex, { includeCoachLed: false });
        const index =
          typeof week.week_index === "number" && Number.isFinite(week.week_index)
            ? week.week_index
            : pos + 1;
        const selected = pos === selectedPos;
        const current = pos === currentPos;
        return (
          <button
            key={cleanText(week.week_id) || `week-${pos}`}
            type="button"
            data-week-pos={pos}
            className={`cm-week-pill${selected ? " cm-week-pill-selected" : ""}${
              current ? " cm-week-pill-current" : ""
            }`}
            aria-current={current ? "step" : undefined}
            aria-pressed={selected}
            onClick={() => onSelect(pos)}
          >
            <span className="cm-week-pill-head">
              <span className="cm-week-pill-index">W{index}</span>
              {current ? <span className="cm-week-pill-dot" aria-hidden="true" /> : null}
            </span>
            {completion.total > 0 ? (
              <span
                className="cm-week-pill-completion"
                title="App sessions completed. Coach/gym days are tracked separately."
              >
                {completion.done}/{completion.total}
                <span className="sr-only"> sessions completed. Coach/gym days are tracked separately.</span>
              </span>
            ) : null}
            {current ? <span className="sr-only">Current week</span> : null}
          </button>
        );
      })}
    </nav>
    </>
  );
}

/** A week's phase as printed text, for phases the glossary does not define. */
function weekPhaseLabel(week: StructuredWeek | undefined): string | null {
  const phase = week ? resolvedWeekPhase(week) : null;
  return phase ? titleize(phase) : null;
}

/** The selected week's dates, phase badge and countdown, then one line of work
 *  sources. The week squares above already carry the week number and app
 *  completion, so this card never repeats them. */
function WeekOverview({
  week,
  weekNumber,
  openOngoing,
}: {
  week: StructuredWeek;
  /** 1-based position of the viewed week; drives the open-plan week intent. */
  weekNumber: number;
  openOngoing: boolean;
}) {
  const sessionSummary = weekSessionSummary(week);
  // Older payloads carry dates and countdowns only on their days.
  const days = getDays(week);
  const dayDates = days.map(day => cleanText(day.date)).filter((date): date is string => Boolean(date));
  const countdown = formatCountdownLabel(week.countdown_start) || formatCountdownLabel(week.countdown_end)
    || formatCountdownLabel(days[0]?.countdown_label);
  const startDate = cleanText(week.start_date) || dayDates[0] || null;
  const endDate = cleanText(week.end_date) || dayDates[dayDates.length - 1] || null;
  // "26 Oct – 1 Nov": short enough to share a line with the phase and countdown.
  const short = (value: string | null) => {
    const iso = value?.slice(0, 10);
    return iso && ISO_DAY_PATTERN.test(iso)
      ? new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })
      : value ? formatAppDate(value) : null;
  };
  const dateRange = startDate && endDate && startDate !== endDate
    ? `${short(startDate)} – ${short(endDate)}`
    : short(startDate || endDate);

  const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
  const facts = [
    sessionSummary.appSessions > 0 ? plural(sessionSummary.appSessions, "app session", "app sessions") : null,
    sessionSummary.coachLedSessions > 0 ? plural(sessionSummary.coachLedSessions, "coach day", "coach days") : null,
    sessionSummary.supportSessions > 0 ? plural(sessionSummary.supportSessions, "support card", "support cards") : null,
  ].filter((fact): fact is string => Boolean(fact));
  const weekIntent = openOngoing ? openBlockWeekIntent(weekNumber, days.some(d => getSessions(d).some(s => s.prescription_source === "athlete"))) : null;
  // The squares stay bare (W1 · 0/2); the phase is named and explained here.
  const phaseEntry = openOngoing ? null : glossaryEntry(resolvedWeekPhase(week));
  const phaseLabel = openOngoing ? null : phaseEntry?.term ?? weekPhaseLabel(week);

  if (!dateRange && !countdown && !phaseLabel && !weekIntent && facts.length === 0) {
    return null;
  }
  return (
    <section className="sp-card cm-week-overview">
      {dateRange || countdown || phaseLabel ? (
        <div className="cm-week-overview-head">
          <h2 className="cm-week-dates">
            {dateRange ?? `Week ${weekNumber}`}
            {phaseLabel ? (
              <span className="cm-week-phase">
                <span className="sp-tag sp-accent" title={phaseLabel}>{phaseLabel}</span>
                {phaseEntry ? <GlossaryTooltip term={phaseEntry.term} /> : null}
              </span>
            ) : null}
          </h2>
          {countdown ? <span className="cm-week-countdown">{countdown}</span> : null}
        </div>
      ) : null}

      {weekIntent ? (
        <p className="sp-block-purpose cm-week-intent">
          <span className="sp-tag sp-accent">{weekIntent.label}</span>
          {weekIntent.summary}
        </p>
      ) : null}

      {facts.length > 0 ? (
        <p className="cm-week-summary">
          {facts.map((fact) => (
            <span key={fact} className="cm-week-fact">{fact}</span>
          ))}
        </p>
      ) : null}
    </section>
  );
}

type PlanViewMode = "list" | "calendar";
const PLAN_VIEW_STORAGE_KEY = "unlxck.plan-view";

const subscribeToNothing = () => () => {};

function readStoredPlanView(fallback: PlanViewMode): PlanViewMode {
  try {
    const stored = window.localStorage.getItem(PLAN_VIEW_STORAGE_KEY);
    return stored === "calendar" || stored === "list" ? stored : fallback;
  } catch {
    // Storage blocked (private mode, previews): keep the default.
    return fallback;
  }
}

/** List / Calendar switch for the plan's week area. */
function PlanViewToggle({
  view,
  onChange,
}: {
  view: PlanViewMode;
  onChange: (view: PlanViewMode) => void;
}) {
  return (
    <div className="cm-view-toggle" role="group" aria-label="Plan view">
      {(["list", "calendar"] as const).map((mode) => (
        <button
          key={mode}
          type="button"
          className="cm-view-toggle-option"
          aria-pressed={view === mode}
          onClick={() => onChange(mode)}
        >
          {mode === "list" ? "List" : "Calendar"}
        </button>
      ))}
    </div>
  );
}

export function StructuredPlanRenderer({
  plan,
  openOngoing = false,
  today,
  createdAt,
  focusDay,
  currentDayLabel = "Today",
  completions,
  currentTrainingDayIso,
  scheduleContext,
  onLogSession,
  defaultView = "list",
  isAdmin = false,
  rehabLabelPolicy,
  exerciseMedia,
  accessToken,
  customPlanId,
}: {
  plan: StructuredPlan;
  /** Renewable four-week plan without a scheduled fight date. */
  openOngoing?: boolean;
  today?: Date;
  /** Plan creation timestamp. For an open plan without a server projection it
   * anchors which week of the renewable block is current (the Monday of the week
   * the plan starts training in, mirroring the backend timeline). Not
   * rendered. */
  createdAt?: string | null;
  /** Accepted for compatibility with callers, but not rendered on this plan view. */
  planStatus?: string | null;
  /** Optional advance target: the next scheduled session's day, passed once
   * today is already logged. It moves ONLY the opened week + day highlight, never
   * the truthful current week marker. Omit it for the normal calendar view. */
  focusDay?: Date;
  /** Badge text for the highlighted day. Callers pass "Next session" alongside
   * `focusDay` so the advanced day is not mislabelled "Today". */
  currentDayLabel?: string;
  /** Live completion rows from /api/plans/{id}/completions. When present the
   * day cards colour every session from real logging instead of the static
   * generation-time statuses. */
  completions?: readonly TodaySessionCompletionRecord[] | null;
  /** Server-authoritative athlete-local training day (YYYY-MM-DD). */
  currentTrainingDayIso?: string | null;
  /** Server-derived timing projection. Open plans use it for block identity and
   * fail-closed legacy messaging; dated camps keep their existing D-X spine. */
  scheduleContext?: PlanScheduleContext | null;
  /** Opens the retro-log flow for a past, still-loggable session. */
  onLogSession?: (day: StructuredDay, session: StructuredSession, sessionId: string) => void;
  /** View shown until the athlete picks one; the Plan page opens on the calendar. */
  defaultView?: PlanViewMode;
  /** Whether the viewer is an admin. The raw "Original plan text" dump is an
   * internal artefact and is hidden from athletes; it still appears for admins,
   * and for anyone in the fail-closed "schedule unavailable" state where the
   * on-screen message directs the athlete to the raw plan below. */
  isAdmin?: boolean;
  /** Server-derived per-region Rehab/Prehab policy (PlanDetail.rehab_label_policy).
   * Rehab blocks whose target region is no longer injured render as "Prehab".
   * Omitted → every rehab block keeps reading "Rehab". */
  rehabLabelPolicy?: RehabLabelPolicy | null;
  /** Curated demo videos keyed by block display_name (PlanOutputs.exercise_media). */
  exerciseMedia?: Record<string, ExerciseMedia> | null;
  accessToken?: string | null;
  /** Only the owning athlete's custom plan may add recurring sessions here. */
  customPlanId?: string | null;
}) {
  const weeks = getWeeks(plan);
  const completionIndex = useMemo(
    () => (completions ? buildCompletionIndex(completions) : undefined),
    [completions],
  );
  // Sessions dated before the plan existed were never available to train. The
  // day is resolved server-side in the athlete's timezone (same basis as the
  // current training day), never from the viewer's device clock.
  const planStartIso = cleanText(scheduleContext?.plan_start_day)?.slice(0, 10) ?? null;

  // Resolve "today" through the shared 03:00 training-day rollover so Plan Detail
  // and the Today tab can never disagree on the current day.
  const mountedDay = useTrainingDay();
  const serverTrainingDay = useMemo(() => {
    const iso = cleanText(currentTrainingDayIso) || cleanText(scheduleContext?.current_training_day);
    if (!iso) {
      return undefined;
    }
    const parsed = new Date(`${iso.slice(0, 10)}T12:00:00`);
    return Number.isNaN(parsed.getTime()) ? undefined : parsed;
  }, [currentTrainingDayIso, scheduleContext?.current_training_day]);
  const calendarDay = today ?? serverTrainingDay ?? mountedDay;

  // Weekday-only open plans need to know which week of the renewable block is
  // current; dated camps ignore the hint (they resolve by calendar date).
  const openWeekNumber = openOngoing
    ? resolveOpenPlanWeekNumber(plan, calendarDay ?? null, {
        currentWeekNumber: scheduleContext?.current_week_number,
        anchorDate: scheduleContext?.anchor_date,
        createdAt,
      })
    : null;

  // The real calendar training day owns the truthful current week marker.
  // `focusDay` only advances the opened week/day highlight.
  const calendarProgress = resolvePlanProgress(plan, calendarDay, {
    openWeekNumber,
    allowDatedWeekdayMatch: openOngoing,
  });
  const calendarPlanDay = calendarProgress.currentWeekPos != null && calendarProgress.currentDayPos != null
    ? getDays(weeks[calendarProgress.currentWeekPos])[calendarProgress.currentDayPos]
    : null;
  const hasCalendarTraining = getSessions(calendarPlanDay).length > 0
    || Boolean(getCoachLedContactView(calendarPlanDay)) || classifySessionlessDay(calendarPlanDay).coachLed;
  const resolvedFocusDay = focusDay || (openOngoing && !hasCalendarTraining)
    ? resolveNextPlanFocusDay(plan, calendarDay, focusDay, {
        openWeekNumber,
        allowDatedWeekdayMatch: openOngoing,
        completionIndex,
      })
    : undefined;
  const focusLabel = resolvedFocusDay && calendarDay && toLocalIsoDay(resolvedFocusDay) > toLocalIsoDay(calendarDay)
    ? "Next session" : currentDayLabel;
  const focusProgress = resolvedFocusDay
    ? resolvePlanProgress(plan, resolvedFocusDay, {
        openWeekNumber,
        allowDatedWeekdayMatch: openOngoing,
      })
    : calendarProgress;
  // The calendar owns the truthful active week while today is inside the camp.
  // Just before a dated camp starts, there is no calendar match yet; in that
  // case the already-resolved next-session focus supplies the actionable week
  // for the red live marker. Manual week selection never changes this value.
  const activeWeekPos =
    calendarProgress.currentWeekPos ?? focusProgress.currentWeekPos;

  // `selectedPos` holds ONLY an explicit manual week choice; while it is null the
  // view auto-follows the plan's current week via the fallback in `effectivePos`
  // below (so no effect is needed to sync it, and there is no render loop).
  const [selectedPos, setSelectedPos] = useState<number | null>(null);

  // A stable identity for the current plan's week structure. When the component
  // is handed a different plan without remounting — e.g. the plan viewer swaps an
  // adapted text plan for the richer structured payload — a retained manual
  // `selectedPos` could point at a week index from the OLD structure. Reset it
  // DURING render (React's recommended "reset state on prop change" pattern):
  // this drops the stale selection before anything renders, so the wrong week is
  // never briefly shown, with no extra effect/render pass.
  const weekSignature = useMemo(
    () =>
      `${weeks.length}:` +
      weeks
        .map((week) => cleanText(week.week_id) || cleanText(week.start_date) || "")
        .join("|"),
    [weeks],
  );
  // The calendar's picked day is a week/day position too: a replacement plan
  // drops it with the week so it can't land on an unrelated date.
  const [calendarSelection, setCalendarSelection] = useState<{ weekPos: number; dayIndex: number } | null>(null);
  const [prevWeekSignature, setPrevWeekSignature] = useState(weekSignature);
  if (weekSignature !== prevWeekSignature) {
    setPrevWeekSignature(weekSignature);
    setSelectedPos(null);
    setCalendarSelection(null);
  }

  // `defaultView` is the server render; a remembered choice takes over on the
  // client, and an explicit tap this visit wins over both.
  const storedView = useSyncExternalStore(subscribeToNothing, () => readStoredPlanView(defaultView), () => defaultView);
  const [chosenView, setChosenView] = useState<PlanViewMode | null>(null);
  const view = chosenView ?? storedView;
  const changeView = (next: PlanViewMode) => {
    setChosenView(next);
    try {
      window.localStorage.setItem(PLAN_VIEW_STORAGE_KEY, next);
    } catch {
      // Not persisting is fine; the choice still applies for this visit.
    }
  };
  // A day picked on the calendar: opened (and scrolled to) once the list shows.
  const [requestedDay, setRequestedDay] = useState<{ weekPos: number; dayIndex: number } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!requestedDay || view !== "list") {
      return;
    }
    const card = rootRef.current?.querySelector<HTMLElement>(
      `.cm-days [data-day-index="${requestedDay.dayIndex}"]`,
    );
    if (card && typeof card.scrollIntoView === "function") {
      card.scrollIntoView({ block: "start", behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
      (card.querySelector<HTMLElement>("summary") ?? card).focus({ preventScroll: true });
    }
  }, [requestedDay, view]);

  const handleSelectWeek = (pos: number) => {
    setSelectedPos(pos);
    setRequestedDay(null);
  };
  // List navigation (Jump to Today / Next session): open and scroll to the day.
  const openListDay = (weekPos: number, dayIndex: number) => {
    setSelectedPos(weekPos);
    setRequestedDay({ weekPos, dayIndex });
  };
  // Calendar taps stay in the calendar and show that day below it; only the
  // List button switches views. A tap also brings the day panel into view when
  // it sits below the fold, so a phone shows the change it just made.
  const selectCalendarDay = (weekPos: number, dayIndex: number) => {
    setCalendarSelection({ weekPos, dayIndex });
    setCalendarScrollRequest({});
  };
  const [calendarScrollRequest, setCalendarScrollRequest] = useState<object | null>(null);
  useEffect(() => {
    const panel = calendarScrollRequest ? rootRef.current?.querySelector<HTMLElement>(".cm-cal-day") : null;
    if (!panel || typeof panel.scrollIntoView !== "function") return;
    const rect = panel.getBoundingClientRect();
    const clearance = parseFloat(getComputedStyle(panel).scrollMarginBottom) || 0;
    const headVisible = rect.top >= 0 && rect.top + Math.min(rect.height, 160) <= window.innerHeight - clearance;
    if (headVisible) return;
    panel.scrollIntoView({
      block: "nearest",
      behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
    });
  }, [calendarScrollRequest]);

  const effectivePos = selectedPos ?? calendarProgress.currentWeekPos ?? focusProgress.currentWeekPos ?? 0;
  const safePos = effectivePos >= 0 && effectivePos < weeks.length ? effectivePos : 0;
  const selectedWeek = weeks[safePos];

  const rawFallback = cleanText(plan.raw_markdown_fallback);
  const safetyOwnershipTexts = useMemo(
    () => [
      ...getSafetyPriorityTexts(plan),
      ...getActiveNotesExcludingRedFlags(plan)
        .map((note) => cleanText(note.text))
        .filter((text): text is string => text !== null),
    ],
    [plan],
  );
  // The raw plan dump is internal: athletes see the structured cards only. It
  // stays for admins, and for the fail-closed schedule-unavailable state whose
  // message tells the athlete to read the original plan below.
  const scheduleUnavailable =
    openOngoing && scheduleContext?.projection_status === "unavailable";
  const showRawFallback = Boolean(rawFallback) && (isAdmin || scheduleUnavailable);
  const dayList = useMemo(() => (selectedWeek ? getDays(selectedWeek) : []), [selectedWeek]);
  const dayTimeline = useMemo(
    () => buildDayTimeline(dayList, !openOngoing),
    [dayList, openOngoing],
  );
  // Calendar day a synthesized gap row should highlight. Gap days never exist
  // in the plan, so focusProgress can't mark them — compare dates directly.
  // Only in the normal (non-advanced) view, though: once the plan advances to a
  // future "Next session" (focusDay set), that future session card owns the
  // marker, so gap rows stay plain instead of stamping today with a label that
  // belongs to a different day.
  const restCurrentIso = (cleanText(currentTrainingDayIso)?.slice(0, 10) ??
      cleanText(scheduleContext?.current_training_day)?.slice(0, 10) ??
      (calendarDay ? toLocalIsoDay(calendarDay) : null));
  // A block that starts on a future Monday — an open plan generated late enough
  // in the week to join the next one — has no current day to mark. Say when it
  // starts rather than showing a plain week with no "Today" anywhere on it and
  // leaving the athlete to guess whether the plan is live.
  //
  // The block starts on the server's anchor (the Monday the week runs from), not
  // on the first day that happens to carry a session: a block anchored to Mon 03
  // Aug whose first session is Thursday still starts on the Monday, and saying
  // "starts Thu 06 Aug" would contradict the week it is shown above. The
  // earliest scheduled day is only a fallback for a payload with no anchor.
  const blockStartsOn = useMemo(() => {
    if (!openOngoing || calendarProgress.currentWeekPos !== null) {
      return null;
    }
    const todayIso =
      cleanText(currentTrainingDayIso)?.slice(0, 10) ??
      cleanText(scheduleContext?.current_training_day)?.slice(0, 10) ??
      (calendarDay ? toLocalIsoDay(calendarDay) : null);
    if (!todayIso) {
      return null;
    }
    const anchorIso = cleanText(scheduleContext?.anchor_date)?.slice(0, 10);
    const startIso =
      anchorIso && ISO_DAY_PATTERN.test(anchorIso)
        ? anchorIso
        : weeks
            .flatMap((week) => getDays(week))
            .map((day) => cleanText(day.date)?.slice(0, 10))
            .filter((iso): iso is string => typeof iso === "string" && ISO_DAY_PATTERN.test(iso))
            .sort()[0];
    return startIso && startIso > todayIso
      ? { iso: startIso, relative: describeRelativeDay(startIso, todayIso) }
      : null;
  }, [
    openOngoing,
    calendarProgress.currentWeekPos,
    currentTrainingDayIso,
    scheduleContext?.current_training_day,
    scheduleContext?.anchor_date,
    calendarDay,
    weeks,
  ]);

  const calendarTodayIso =
    cleanText(currentTrainingDayIso)?.slice(0, 10) ??
    cleanText(scheduleContext?.current_training_day)?.slice(0, 10) ??
    (calendarDay ? toLocalIsoDay(calendarDay) : null);
  const calendarRows = useMemo(
    () =>
      view === "calendar"
        ? buildPlanCalendar(weeks, {
            completionIndex,
            currentDayIso: calendarTodayIso,
            planStartIso,
            anchorIso: scheduleContext?.anchor_date,
            currentWeekPos: activeWeekPos,
          })
        : [],
    [view, weeks, completionIndex, calendarTodayIso, planStartIso, scheduleContext?.anchor_date, activeWeekPos],
  );
  // Selected calendar day: the athlete's pick, else today when it is a training
  // day in the camp, else the next session, else the first openable day.
  const calendarDaySelection = useMemo(() => {
    if (view !== "calendar") return null;
    const cells = calendarRows.flatMap(row => row.cells)
      .filter(cell => cell.weekPos !== null && cell.dayIndex !== null && cell.state !== "outside" && (customPlanId || cell.state !== "rest"));
    const pick = (weekPos: number | null | undefined, dayIndex: number | null | undefined) =>
      cells.find(cell => cell.weekPos === weekPos && cell.dayIndex === dayIndex);
    const cell = (calendarSelection && pick(calendarSelection.weekPos, calendarSelection.dayIndex))
      || cells.find(candidate => candidate.isToday)
      || pick(focusProgress.currentWeekPos, focusProgress.currentDayPos)
      // Today is a rest/gap day: the next dated training day, not the first one.
      || (calendarTodayIso ? cells.find(candidate => candidate.iso !== null && candidate.iso > calendarTodayIso) : undefined)
      || cells[0];
    return cell ? { weekPos: cell.weekPos as number, dayIndex: cell.dayIndex as number } : null;
  }, [view, calendarRows, calendarSelection, focusProgress.currentWeekPos, focusProgress.currentDayPos, calendarTodayIso, customPlanId]);
  const calendarSelectedDay = calendarDaySelection
    ? getDays(weeks[calendarDaySelection.weekPos])[calendarDaySelection.dayIndex] ?? null
    : null;
  const viewToggle = <PlanViewToggle view={view} onChange={changeView} />;

  return (
    <RehabLabelProvider policy={rehabLabelPolicy}>
    <PrivateExerciseMediaProvider token={accessToken}>
    <ExerciseMediaProvider media={exerciseMedia}>
    <PlanSourceTextContext.Provider value={rawFallback}>
    <PlanSafetyTextContext.Provider value={safetyOwnershipTexts}>
    <div className="sp-root cm-root" ref={rootRef}>
      {weeks.length > 0 ? (
        <>
          {openOngoing && scheduleContext?.projection_status === "unavailable" ? (
            <section className="sp-card cm-schedule-unavailable" role="status">
              <p className="sp-eyebrow">Schedule unavailable</p>
              <p className="sp-block-purpose">
                This older plan couldn’t be matched to weekdays. Use the original below until it’s rebuilt.
              </p>
            </section>
          ) : null}
          {view === "calendar" ? (
            <>
              <div className="cm-week-navigation cm-cal-navigation">
                <div className="cm-cal-heading">
                  <p className="cm-week-navigation-label">
                    {openOngoing ? `Block ${scheduleContext?.block_number ?? 1}` : "Camp"} · {weeks.length}{" "}
                    {weeks.length === 1 ? "week" : "weeks"}
                  </p>
                  {calendarCaption(calendarRows) ? <p className="cm-cal-subtitle">{calendarCaption(calendarRows)}</p> : null}
                </div>
                <div className="cm-week-nav-actions">{viewToggle}</div>
              </div>
              <PlanCalendar
                rows={calendarRows}
                selectedWeekPos={calendarDaySelection?.weekPos ?? safePos}
                selected={calendarDaySelection}
                phaseFor={openOngoing ? undefined : (weekPos) =>
                  glossaryEntry(resolvedWeekPhase(weeks[weekPos]))?.term ?? weekPhaseLabel(weeks[weekPos])}
                onSelectDay={selectCalendarDay}
                allowRestSelection={Boolean(customPlanId)}
              />
              {calendarSelectedDay && calendarDaySelection ? (
                <CalendarDayPanel
                  key={`${calendarDaySelection.weekPos}-${calendarDaySelection.dayIndex}`}
                  day={calendarSelectedDay}
                  openOngoing={openOngoing}
                  weekNumber={calendarDaySelection.weekPos + 1}
                  completionIndex={completionIndex}
                  currentTrainingDayIso={currentTrainingDayIso}
                  planStartIso={planStartIso}
                  onLogSession={onLogSession}
                  addSessionHref={customDaySessionHref(calendarSelectedDay, customPlanId)}
                />
              ) : null}
            </>
          ) : (
            <>
              <WeekStrip
                weeks={weeks}
                selectedPos={safePos}
                currentPos={activeWeekPos}
                onSelect={handleSelectWeek}
                completionIndex={completionIndex}
                openOngoing={openOngoing}
                controls={viewToggle}
                label={openOngoing && selectedWeek
                  ? `Block ${scheduleContext?.block_number ?? 1} · Week ${resolveFiniteWeekNumber(selectedWeek.week_index, scheduleContext?.current_week_number)} of 4`
                  : undefined}
              />

              {selectedWeek ? (
                <WeekOverview
                  week={selectedWeek}
                  weekNumber={safePos + 1}
                  openOngoing={openOngoing}
                />
              ) : null}
            </>
          )}

          <div className="cm-guardrails">
            <RedFlagsCard plan={plan} />
          </div>

          {blockStartsOn ? (
            <p className="sp-today-note cm-block-start" role="status">
              {`Plan starts ${formatAppDate(blockStartsOn.iso)}`}
              {blockStartsOn.relative ? ` · ${blockStartsOn.relative.toLowerCase()}` : ""}
            </p>
          ) : null}

          {view === "list" ? (
          <>
          {calendarProgress.currentWeekPos != null && calendarProgress.currentDayPos != null ? (
            <div className="cm-day-navigation"><button type="button" className="secondary-button"
              onClick={() => openListDay(calendarProgress.currentWeekPos!, calendarProgress.currentDayPos!)}>Jump to Today</button>
              {focusLabel === "Next session" && focusProgress.currentWeekPos != null && focusProgress.currentDayPos != null ? <button type="button" className="secondary-button" onClick={() => openListDay(focusProgress.currentWeekPos!, focusProgress.currentDayPos!)}>Next session</button> : null}</div>
          ) : null}
          <div className="sp-weeks cm-days">
            {dayList.length > 0 ? (
              dayTimeline.map((entry) => {
                if (entry.kind === "gap") {
                  const gapLabel = gapDayLabel(entry.countdown);
                  return (
                    <RestDayRow
                      key={`rest-${entry.dateIso}`}
                      countdown={entry.countdown}
                      weekday={entry.weekday}
                      label={gapLabel.label}
                      reason={gapLabel.reason}
                      isCurrent={entry.dateIso === restCurrentIso}
                      currentLabel={calendarProgress.currentWeekPos != null ? "Today" : focusLabel}
                    />
                  );
                }
                const { day, index } = entry;
                // Dated days match on the calendar date. A weekday-only (open
                // plan) match carries no date, so it is identified by week/day
                // position instead — only while the matched week is the one
                // being viewed.
                const dayProgress = calendarProgress.currentWeekPos != null ? calendarProgress : focusProgress;
                const isCurrent = dayProgress.currentDayDate != null
                  ? cleanText(day.date)?.slice(0, 10) === dayProgress.currentDayDate
                  : dayProgress.currentWeekPos === safePos && dayProgress.currentDayPos === index;

                // A pure rest day renders identically to a synthesized gap row,
                // so real and filled-in rest days read as one continuous line.
                if (isPlainRestDay(day) && !customPlanId) {
                  const restDate = cleanText(day.date);
                  const displayDay = isCurrent && focusProgress.matchType === "weekday"
                    ? { ...day, date: focusProgress.trainingDayISO } : day;
                  return (
                    <RestDayRow
                      key={restDate || `day-${index}`}
                      dayIndex={index}
                      countdown={formatCountdownLabel(day.countdown_label)}
                      weekday={openOngoing ? openTimelineDayLabel(displayDay, safePos + 1, `Training day ${index + 1}`) : weekdayLabel(restDate)}
                      label={humanizeIfRawEnum(cleanText(day.today_card?.headline)) || "Rest"}
                      isCurrent={isCurrent}
                      currentLabel={calendarProgress.currentWeekPos != null ? "Today" : focusLabel}
                    />
                  );
                }

                return (
                  <CampDayCard
                    key={cleanText(day.date) || `day-${index}`}
                    day={day}
                    displayDate={
                      isCurrent && focusProgress.matchType === "weekday"
                        ? focusProgress.trainingDayISO
                        : null
                    }
                    fallbackLabel={`Training day ${index + 1}`}
                    openOngoing={openOngoing}
                    weekNumber={safePos + 1}
                    isCurrent={isCurrent}
                    isNext={!isCurrent && focusProgress.currentWeekPos === safePos && focusProgress.currentDayPos === index && focusLabel === "Next session"}
                    currentLabel={calendarProgress.currentWeekPos != null ? "Today" : focusLabel}
                    defaultOpen={
                      (calendarProgress.currentWeekPos === safePos && calendarProgress.currentDayPos === index) ||
                      (requestedDay?.weekPos === safePos && requestedDay.dayIndex === index)
                    }
                    openRequest={requestedDay?.weekPos === safePos && requestedDay.dayIndex === index ? requestedDay : undefined}
                    dayIndex={index}
                    completionIndex={completionIndex}
                    currentTrainingDayIso={currentTrainingDayIso}
                    planStartIso={planStartIso}
                    onLogSession={onLogSession}
                    addSessionHref={customDaySessionHref(day, customPlanId)}
                  />
                );
              })
            ) : (
              <p className="sp-muted">No days scheduled this week.</p>
            )}
          </div>
          </>
          ) : null}
        </>
      ) : null}

      {showRawFallback ? (
        <details className="sp-collapse cm-raw-fallback">
          <summary className="sp-collapse-summary">
            <span className="sp-collapse-title">Original plan text</span>
            <span className="sp-collapse-action">Show original</span>
          </summary>
          <div className="sp-collapse-body">
            <pre className="cm-raw-pre">{rawFallback}</pre>
          </div>
        </details>
      ) : null}
    </div>
    </PlanSafetyTextContext.Provider>
    </PlanSourceTextContext.Provider>
    </ExerciseMediaProvider>
    </PrivateExerciseMediaProvider>
    </RehabLabelProvider>
  );
}
