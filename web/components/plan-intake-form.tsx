"use client";

import Link from "next/link";
import { ActionLink } from "@/components/action-link";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState, useTransition } from "react";

import { RequireAuth } from "@/components/auth-guard";
import { useAppSession } from "@/components/auth-provider";
import { BodyMap, type BodyMapSide } from "@/components/body-map";
import { CustomSelect } from "@/components/custom-select";
import { EquipmentSelector } from "@/components/equipment-selector";
import { saveOnboardingDraft } from "@/lib/api";
import { formatAppDate } from "@/lib/date-format";
import { markGenerationIntent } from "@/lib/generation-intent";
import {
  detectDeviceTimeZone,
  EQUIPMENT_ACCESS_OPTIONS,
  canonicalizeEquipmentAccessValues,
  getOptionLabel,
  getOptionLabels,
  isValidRecordFormat,
  KEY_GOAL_OPTIONS,
  normalizeGuidedInjurySeverity,
  PROFESSIONAL_STATUS_OPTIONS,
  retainKnownOptionValues,
  sanitizeRecordInput,
  STANCE_OPTIONS,
  TACTICAL_STYLE_OPTIONS,
  TECHNICAL_STYLE_OPTIONS,
  toggleListValue,
  TRAINING_AVAILABILITY_OPTIONS,
  type IntakeOption,
  WEAK_AREA_OPTIONS,
} from "@/lib/intake-options";
import {
  buildGuidedInjuryFields,
  coerceGuidedInjuryEditState,
  EMPTY_GUIDED_INJURY,
  hasGuidedInjuryContent,
  hasGuidedInjuryDescriptorWithoutArea,
  hydrateGuidedInjuryStates,
  type GuidedInjuryState,
} from "@/lib/guided-injury";
import { StepValidationPanel, type StepValidationCheck } from "@/components/onboarding-step-validation";
import { GuidedInjuryCard } from "@/components/guided-injury-card";
import { formatInjuryDetail, stripInternalMarkers } from "@/lib/injury-display";
import { OnboardingTrustNote } from "@/components/onboarding-trust-note";
import { SafetyNote } from "@/components/safety-note";
import { WhyTooltip } from "@/components/why-tooltip";
import { INJURY_INTAKE_SAFETY } from "@/lib/safety-copy";
import { LevelSlider, type LevelValue } from "@/components/rating-controls";
import { applyNoScheduledFightSnapshot, canonicalizePerformanceFocus, emptyPlanRequest, hydratePlanRequest, mergePlanRequestDraft, mergeSavedOnboardingDraft } from "@/lib/onboarding";
import { writePendingGenerationPayload } from "@/lib/generation-pending-payload";
import { buildRoundsFormat, parseRoundsFormat, ROUND_COUNT_OPTIONS, ROUND_DURATION_OPTIONS } from "@/lib/rounds-format";
import { FOCUS_CAP_DISABLED_REASON, getPerformanceFocusCap, validatePerformanceFocusSelections } from "@/lib/performance-focus-cap";
import { canSelectWizardStep } from "@/lib/step-navigation";
import {
  getAvailabilityConsistency,
  getHardSparringWarning,
  getSparringConsistency,
  HARD_SPARRING_DAY_CAP,
} from "@/lib/training-schedule";
import {
  buildDaysOutContext,
  computeDaysUntilFight,
  isFightDateInPast,
  getFightDayLockedWeekday,
  filterAvailablePerformanceFocusValues,
  getPerformanceFocusOptionAvailability,
  HARD_SPARRING_STRENGTH_REMOVAL_MESSAGE,
  shouldHideField,
  shouldDisableField,
  shouldDeEmphasizeField,
  getFieldHelperText,
  type DaysOutContext,
  type PerformanceFocusGroup,
} from "@/lib/days-out-policy";
import type { PlanRequest } from "@/lib/types";
import { deriveAgeFromDateOfBirth, hasHealthDataConsent } from "@/lib/compliance";
import { HEALTH_CONSENT_BLOCKED_MESSAGE, withoutIntakeHealthData } from "@/lib/health-consent-ui";
import {
  ATHLETE_FULL_NAME_MAX,
  MENTAL_BLOCKERS_MAX,
  PREVIOUS_PLAN_FEEDBACK_MAX,
  RECORD_MAX,
  TRAINING_PREFERENCE_MAX,
} from "@/lib/input-limits";
import { useFadeNavigate } from "@/components/fade-through";

const steps = ["Profile", "Fight Context", "Training", "Restrictions", "Performance", "Review"] as const;
const PERFORMANCE_STEP_INDEX = 4;
const GUIDED_INJURY_MISSING_AREA_MESSAGE = "Add where it hurts before choosing how bad it is or how it's changing.";

// Each validation field lives on one step. Used as a safety net so any
// reportInvalidField caller still routes the user to the right step even
// if the call site forgets to pass `step`.
const FIELD_STEP_MAP: Record<string, number> = {
  fullName: 0,
  record: 0,
  technicalStyle: 0,
  fightDate: 1,
  roundCount: 1,
  roundDuration: 1,
  sessionsPerWeek: 1,
  trainingAvailabilityGroup: 2,
  hardSparringAck: 2,
  availabilityConsistencyAlert: 2,
  sparringConsistencyAlert: 2,
  keyGoalsGroup: PERFORMANCE_STEP_INDEX,
};

function resolveFieldStep(fieldId: string): number | undefined {
  if (fieldId.startsWith("guidedInjuryCard-")) return 3;
  return FIELD_STEP_MAP[fieldId];
}
const SEX_OPTIONS: IntakeOption[] = [
  { label: "Male", value: "male" },
  { label: "Female", value: "female" },
];

type PriorityOverlap = {
  label: string;
  normalizedTag: string;
  tag: string;
};

const POWER_CLARIFICATION_OPTIONS = [
  "Overall power",
  "Power drops when tired",
  "First-step explosiveness",
  "Punching or striking power",
  "Kicking power",
  "Lower-body power",
  "Rotational power through hips and trunk",
  "Not sure",
];
const CONDITIONING_CLARIFICATION_OPTIONS = [
  "Overall gas tank",
  "Late-round fatigue",
  "Recovery between bursts",
  "Baseline cardio",
  "Repeated hard efforts",
  "Not sure",
];
const MOBILITY_CLARIFICATION_OPTIONS = [
  "General mobility",
  "Hip mobility",
  "Shoulder mobility",
  "Ankle mobility",
  "Stiff movement when tired",
  "Not sure",
];
const GENERIC_CLARIFICATION_OPTIONS = [
  "I want to improve it overall",
  "It drops off when tired",
  "It affects my technique",
  "It affects my power",
  "It affects my conditioning",
  "Not sure",
];
const PRIORITY_OVERLAP_ALIASES: Record<string, string> = {
  gas_tank: "conditioning",
};

function normalizePriorityOverlapValue(value: string): string {
  const normalized = value
    .trim()
    .toLowerCase()
    .replace(/&/g, " ")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return PRIORITY_OVERLAP_ALIASES[normalized] ?? normalized;
}

function getPriorityOptionLabel(value: string): string {
  return (
    KEY_GOAL_OPTIONS.find((option) => option.value === value)?.label
    ?? WEAK_AREA_OPTIONS.find((option) => option.value === value)?.label
    ?? value
  );
}

function getGoalWeakAreaOverlaps(keyGoals: string[], weakAreas: string[]): PriorityOverlap[] {
  const weakAreaSet = new Set(weakAreas.map(normalizePriorityOverlapValue).filter(Boolean));
  const overlaps: PriorityOverlap[] = [];
  const seen = new Set<string>();

  for (const goal of keyGoals) {
    const normalizedTag = normalizePriorityOverlapValue(goal);
    if (!normalizedTag || !weakAreaSet.has(normalizedTag) || seen.has(normalizedTag)) {
      continue;
    }
    seen.add(normalizedTag);
    overlaps.push({
      label: getPriorityOptionLabel(goal),
      normalizedTag,
      tag: goal,
    });
  }

  return overlaps;
}

function getClarificationOptions(
  normalizedTag: string,
  technicalStyles: string[],
  tacticalStyles: string[],
): string[] {
  const styleSet = new Set([...technicalStyles, ...tacticalStyles].map(normalizePriorityOverlapValue));

  if (normalizedTag === "power" || normalizedTag === "power_explosiveness") {
    return POWER_CLARIFICATION_OPTIONS.filter((option) => {
      if (option === "Kicking power") {
        return styleSet.has("kickboxing") || styleSet.has("muay_thai") || styleSet.has("mma");
      }
      if (option === "Punching or striking power") {
        return styleSet.has("boxing") || styleSet.has("kickboxing") || styleSet.has("muay_thai") || styleSet.has("mma");
      }
      return true;
    });
  }

  if (normalizedTag === "conditioning" || normalizedTag === "gas_tank") {
    return CONDITIONING_CLARIFICATION_OPTIONS;
  }

  if (normalizedTag === "mobility") {
    return MOBILITY_CLARIFICATION_OPTIONS;
  }

  return GENERIC_CLARIFICATION_OPTIONS;
}

// Weight cut support, coach notes and priority clarification detail are switched
// off ("Coming soon"), the same way unsupported sports are. Clear anything a
// saved draft still carries so it never reaches review or the plan request.
const PRIORITY_CLARIFICATION_ENABLED = false;

function withoutComingSoonFields(form: PlanRequest): PlanRequest {
  const keyGoals = form.key_goals.filter((goal) => goal !== "weight_cut");
  if (
    keyGoals.length === form.key_goals.length
    && form.primary_goal !== "weight_cut"
    && !form.mindset_challenges
    && !form.notes
  ) {
    return form;
  }
  return {
    ...form,
    key_goals: keyGoals,
    primary_goal: form.primary_goal === "weight_cut" ? "" : form.primary_goal,
    mindset_challenges: "",
    notes: "",
  };
}

function sanitizeCollisionMetadata(form: PlanRequest): Pick<PlanRequest, "goal_weakness_collision_detail" | "goal_weakness_collision_tags" | "goal_weakness_collision_details"> {
  const overlaps = getGoalWeakAreaOverlaps(form.key_goals, form.weak_areas);
  if (!PRIORITY_CLARIFICATION_ENABLED || !overlaps.length) {
    return {
      goal_weakness_collision_detail: "",
      goal_weakness_collision_tags: [],
      goal_weakness_collision_details: [],
    };
  }

  const currentDetailMap = new Map((form.goal_weakness_collision_details ?? [])
    .map((entry) => [normalizePriorityOverlapValue(entry.tag), entry.detail?.trim() ?? ""]));
  const nextDetails = overlaps.map((overlap) => {
    const options = getClarificationOptions(overlap.normalizedTag, form.athlete.technical_style, form.athlete.tactical_style);
    const detail = currentDetailMap.get(overlap.normalizedTag) ?? "";
    return {
      tag: overlap.tag,
      label: overlap.label,
      detail: options.includes(detail) ? detail : "",
    };
  });

  const primaryOptions = getClarificationOptions(overlaps[0].normalizedTag, form.athlete.technical_style, form.athlete.tactical_style);
  const primaryDetail = nextDetails[0]?.detail ?? "";
  const currentSingularDetail = form.goal_weakness_collision_detail?.trim() ?? "";

  return {
    goal_weakness_collision_detail: primaryOptions.includes(currentSingularDetail) ? currentSingularDetail : primaryDetail,
    goal_weakness_collision_tags: overlaps.map((overlap) => overlap.tag),
    goal_weakness_collision_details: nextDetails,
  };
}

type DraftMetadata = {
  current_step?: number;
  guided_injury?: Partial<GuidedInjuryState> | null;
  guided_injuries?: Array<Partial<GuidedInjuryState> | null> | null;
  no_scheduled_fight?: boolean | null;
};

function numberOrNull(value: string): number | null {
  if (!value.trim()) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function integerOrNull(value: string): number | null {
  const parsed = numberOrNull(value);
  return parsed === null ? null : Math.round(parsed);
}

function formatValue(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "Not provided";
  }
  return String(value);
}

function formatFightDateValue(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "Not provided";
  }
  return formatAppDate(value);
}

function hasValue(value: string | number | null | undefined): boolean {
  return !(value === null || value === undefined || value === "");
}

function humanizeToken(value: string): string {
  return value.replace(/_/g, " ").trim();
}

// Athlete-facing injury line: "Left bicep: cut, low severity, stable. Avoid
// friction." The stored planner text carries internal keys and markers
// ("Type: surface_injury", "[training_impact:not_limiting]") that stay hidden.
function formatRestrictionSummary(injuries: GuidedInjuryState[], fallback: string | null | undefined): string {
  const lines = injuries
    .filter((injury) => hasGuidedInjuryContent(injury))
    .map((injury) => {
      const descriptors = [
        humanizeToken(injury.surface_type || injury.injury_type),
        injury.severity ? `${humanizeToken(injury.severity)} severity` : "",
        humanizeToken(injury.trend),
      ].filter(Boolean);
      const head = [injury.area.trim(), descriptors.join(", ")].filter(Boolean).join(": ");
      const avoid = injury.avoid.trim() ? `Avoid ${humanizeToken(injury.avoid)}` : "";
      const notes = stripInternalMarkers(injury.notes);
      return [head, avoid, notes].filter(Boolean).join(". ");
    })
    .filter(Boolean);
  if (lines.length) {
    return lines.join("; ");
  }
  const detail = formatInjuryDetail(fallback);
  return detail || "No injuries or restrictions reported.";
}

function getTodayIsoDate(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

const FIGHT_DATE_IN_PAST_MESSAGE =
  "Fight date can't be in the past. Pick an upcoming date or mark \"No scheduled fight\".";

function formatJoinedLabels(values: string[], emptyLabel: string): string {
  return values.length ? values.join(", ") : emptyLabel;
}

function formatWeightCutStatus(currentWeight: number | null | undefined, targetWeight: number | null | undefined): string | null {
  if (currentWeight == null && targetWeight == null) {
    return null;
  }
  if (currentWeight == null) {
    return `Target weight set at ${targetWeight} kg`;
  }
  if (targetWeight == null) {
    return `Current weight ${currentWeight} kg with no target weight set`;
  }

  const difference = Number((currentWeight - targetWeight).toFixed(1));
  if (difference <= -0.5) {
    return `Target weight is ${Math.abs(difference)} kg above current weight`;
  }
  if (difference <= 0.5) {
    return `At target range (${targetWeight} kg)`;
  }
  if (difference <= 2) {
    return `Small cut of ${difference} kg`;
  }
  if (difference <= 4) {
    return `Moderate cut pressure (${difference} kg)`;
  }
  return `High cut pressure (${difference} kg)`;
}

function formatEquipmentLimitations(selectedEquipment: string[]): string | null {
  if (!selectedEquipment.length) {
    return "Not provided";
  }

  const loadedStrengthOptions = ["barbell", "dumbbells", "kettlebells", "trap_bar", "cable", "landmine"];
  const conditioningOptions = ["assault_bike", "rower", "sled", "heavy_bag", "pads"];
  const hasLoadedStrengthOption = selectedEquipment.some((item) => loadedStrengthOptions.includes(item));
  const hasConditioningOption = selectedEquipment.some((item) => conditioningOptions.includes(item));

  if (selectedEquipment.length <= 2) {
    return "Tight equipment setup";
  }
  if (!hasLoadedStrengthOption) {
    return "Limited loaded strength options";
  }
  if (!hasConditioningOption) {
    return "Limited conditioning tool options";
  }
  return "No major equipment limitation flagged";
}

function getPerformanceFocusGroupForField(key: string): PerformanceFocusGroup | null {
  if (key === "key_goals" || key === "weak_areas") {
    return key;
  }
  return null;
}

function formatSparringCollisionRisk({
  fatigueLevel,
  injuries,
  sessionsPerWeek,
  technicalStyle,
  hardSparringDays,
}: {
  fatigueLevel: string;
  injuries: string;
  sessionsPerWeek: number | null | undefined;
  technicalStyle: string;
  hardSparringDays: string[];
}): string | null {
  if (!technicalStyle) {
    return null;
  }

  const hasTissueIssue = injuries.trim().length > 0;
  const highLoad = (sessionsPerWeek ?? 0) >= 5;
  const fixedHardSparring = hardSparringDays.length >= 2;

  if (fixedHardSparring) {
    return `Hard sparring on ${hardSparringDays.join(", ")} will be kept apart from your heaviest strength and conditioning days`;
  }

  if (fatigueLevel === "high" || (hasTissueIssue && highLoad)) {
    return "Hard sparring will be kept well away from heavy strength and conditioning";
  }
  if (fatigueLevel === "moderate" || hasTissueIssue || highLoad) {
    return "Hard sparring will be separated from your hardest strength and conditioning days";
  }
  return "Hard sparring won't be stacked on your hardest strength and conditioning days";
}

function StepPills({
  currentStep,
  onStepSelect,
}: {
  currentStep: number;
  onStepSelect: (step: number) => void;
}) {
  return (
    <div className="step-progress" aria-label="Intake progress">
      {steps.map((label, index) => {
        const statusClass = index < currentStep ? "step-pill-complete" : index === currentStep ? "step-pill-active" : "";
        const statusText = index < currentStep ? "Complete" : index === currentStep ? "Current" : "Upcoming";
        const pillContent = (
          <>
            <span className="step-pill-index">{String(index + 1).padStart(2, "0")}</span>
            <div>
              <div className="step-pill-title">{label}</div>
              <p className="step-pill-meta">{statusText}</p>
            </div>
          </>
        );

        return (
          <button
            key={label}
            type="button"
            className={`step-pill step-pill-button ${statusClass}`.trim()}
            onClick={() => onStepSelect(index)}
            aria-current={index === currentStep ? "step" : undefined}
          >
            {pillContent}
          </button>
        );
      })}
    </div>
  );
}

function AutoSaveIndicator({
  status,
  lastSavedAt,
  onRetry,
  retryDisabled,
}: {
  status: "idle" | "saving" | "saved" | "error";
  lastSavedAt: number | null;
  onRetry: () => void;
  retryDisabled: boolean;
}) {
  if (status === "idle" && lastSavedAt === null) {
    return null;
  }
  const label =
    status === "saving"
      ? "Saving draft…"
      : status === "error"
        ? "Couldn't save draft"
        : "Draft saved";
  return (
    <p className={`onboarding-save-indicator onboarding-save-indicator-${status === "idle" ? "saved" : status}`} aria-live="polite">
      <span className="onboarding-save-indicator-dot" aria-hidden="true" />
      <span>{label}</span>
      {status === "error" ? (
        <button type="button" className="onboarding-save-indicator-retry" onClick={onRetry} disabled={retryDisabled}>
          Retry
        </button>
      ) : null}
    </p>
  );
}

function getOnboardingProgressState(currentStep: number) {
  const stepNumber = currentStep + 1;
  const totalSteps = steps.length;
  const remainingSteps = Math.max(totalSteps - stepNumber, 0);

  return {
    stepNumber,
    totalSteps,
    progressValue: (stepNumber / totalSteps) * 100,
    badgeText: remainingSteps === 0 ? "Ready" : "In progress",
    helperText:
      remainingSteps === 0
        ? "All intake steps are complete. Review your answers, then generate the plan."
        : `${remainingSteps} step${remainingSteps === 1 ? "" : "s"} remaining before plan generation.`,
  };
}

function OnboardingProgressStrip({
  currentStep,
  isExpandable = false,
  isExpanded = false,
  onToggle,
  controlsId,
}: {
  currentStep: number;
  isExpandable?: boolean;
  isExpanded?: boolean;
  onToggle?: () => void;
  controlsId?: string;
}) {
  const progress = getOnboardingProgressState(currentStep);
  const content = (
    <>
      <div className="onboarding-progress-strip-topline">
        <p className="kicker">Intake progress</p>
        <span
          className={`onboarding-progress-badge ${progress.badgeText === "Ready" ? "onboarding-progress-badge-ready" : ""}`.trim()}
        >
          {progress.badgeText}
        </span>
      </div>
      <p className="onboarding-progress-strip-title">
        Step {progress.stepNumber} of {progress.totalSteps}
      </p>
      <div className="overview-progress-track onboarding-progress-track" role="presentation" aria-hidden="true">
        <span className="overview-progress-fill onboarding-progress-fill" style={{ width: `${progress.progressValue}%` }} />
      </div>
      <div className="onboarding-progress-strip-footer">
        <p className="overview-progress-helper onboarding-progress-helper">{progress.helperText}</p>
        {isExpandable ? (
          <span className="onboarding-progress-affordance" aria-hidden="true">
            <span className="onboarding-progress-affordance-label">{isExpanded ? "Close" : "All steps"}</span>
            <span className="onboarding-progress-chevron" />
          </span>
        ) : null}
      </div>
    </>
  );

  if (isExpandable && onToggle && controlsId) {
    return (
      <button
        type="button"
        className="onboarding-progress-strip onboarding-progress-strip-button onboarding-mobile-step-trigger"
        aria-expanded={isExpanded}
        aria-controls={controlsId}
        onClick={onToggle}
      >
        {content}
      </button>
    );
  }

  return <div className="onboarding-progress-strip">{content}</div>;
}

function MobileStepRail({
  currentStep,
  onStepSelect,
}: {
  currentStep: number;
  onStepSelect: (step: number) => void;
}) {
  const railRef = useRef<HTMLDivElement | null>(null);
  const itemRefs = useRef<Array<HTMLElement | null>>([]);

  useEffect(() => {
    const rail = railRef.current;
    const activeItem = itemRefs.current[currentStep];
    if (!rail || !activeItem) {
      return;
    }

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const itemCenter = activeItem.offsetLeft + activeItem.offsetWidth / 2;
    const targetLeft = Math.max(itemCenter - rail.clientWidth / 2, 0);
    const maxScrollLeft = Math.max(rail.scrollWidth - rail.clientWidth, 0);

    rail.scrollTo({
      left: Math.min(targetLeft, maxScrollLeft),
      behavior: reducedMotion ? "auto" : "smooth",
    });
  }, [currentStep]);

  return (
    <div className="mobile-step-rail" data-state="open">
      <div ref={railRef} className="mobile-step-rail-scroll" aria-label="Intake steps">
        {steps.map((label, index) => {
          const statusClass = index < currentStep ? "mobile-step-rail-item-complete" : index === currentStep ? "mobile-step-rail-item-active" : "";
          const pillContent = (
            <>
              <span className="mobile-step-rail-index">{String(index + 1).padStart(2, "0")}</span>
              <span className="mobile-step-rail-label">{label}</span>
            </>
          );

          return (
            <button
              key={label}
              type="button"
              ref={(node) => {
                itemRefs.current[index] = node;
              }}
              className={`mobile-step-rail-item ${statusClass}`.trim()}
              onClick={() => onStepSelect(index)}
              aria-current={index === currentStep ? "step" : undefined}
              aria-label={`${label}, step ${index + 1}, ${index < currentStep ? "complete" : index === currentStep ? "current" : "upcoming"}`}
            >
              {pillContent}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function PlanSetupAlternatives() {
  return (
    <nav className="action-alternatives" aria-label="Other ways to build a plan">
      <p className="action-alternatives-label">Other ways to build</p>
      <ActionLink href="/quick-build">Quick Build</ActionLink>
      <ActionLink href="/plans/custom">Add my plan</ActionLink>
    </nav>
  );
}

function MobileOnboardingHeader({
  currentStep,
  isOpen,
  onToggle,
  onStepSelect,
  saveStatus,
  lastSavedAt,
  onRetrySave,
  retrySaveDisabled,
}: {
  currentStep: number;
  isOpen: boolean;
  onToggle: () => void;
  onStepSelect: (step: number) => void;
  saveStatus: "idle" | "saving" | "saved" | "error";
  lastSavedAt: number | null;
  onRetrySave: () => void;
  retrySaveDisabled: boolean;
}) {
  return (
    <div className="onboarding-heading-mobile">
      <div className="onboarding-mobile-header-copy">
        <p className="kicker">Camp Setup</p>
        <p className="onboarding-mobile-title">Build your camp profile.</p>
        <p className="muted">Your answers save as you go.</p>
        <PlanSetupAlternatives />
      </div>
      <OnboardingProgressStrip
        currentStep={currentStep}
        isExpandable
        isExpanded={isOpen}
        onToggle={onToggle}
        controlsId="onboarding-mobile-steps"
      />
      <AutoSaveIndicator
        status={saveStatus}
        lastSavedAt={lastSavedAt}
        onRetry={onRetrySave}
        retryDisabled={retrySaveDisabled}
      />
      {isOpen ? (
        <div id="onboarding-mobile-steps" className="onboarding-mobile-progress-panel">
          <MobileStepRail currentStep={currentStep} onStepSelect={onStepSelect} />
        </div>
      ) : null}
    </div>
  );
}

function CheckboxGroup({
  id,
  label,
  options,
  selectedValues,
  onToggle,
  disableAdditionalSelections = false,
  capDisabledReason,
  disableAll = false,
  getOptionDisabledReason,
  invalid = false,
  describedBy,
  hideLabel = false,
}: {
  id?: string;
  label: string;
  options: IntakeOption[];
  selectedValues: string[];
  onToggle: (value: string) => void;
  disableAdditionalSelections?: boolean;
  capDisabledReason?: string;
  disableAll?: boolean;
  getOptionDisabledReason?: (option: IntakeOption, checked: boolean) => string | null;
  invalid?: boolean;
  describedBy?: string;
  hideLabel?: boolean;
}) {
  return (
    <div
      id={id}
      className={`field${invalid ? " field-invalid" : ""}`}
      role="group"
      aria-label={label}
      aria-describedby={describedBy}
      tabIndex={invalid ? -1 : undefined}
    >
      <span className={`checkbox-group-label${hideLabel ? " sr-only" : ""}`}>{label}</span>
      <div className="checkbox-grid">
        {options.map((option) => {
          const checked = selectedValues.includes(option.value);
          const daysOutDisabledReason = getOptionDisabledReason?.(option, checked) ?? null;
          const capDisabled = disableAdditionalSelections && !checked;
          const comingSoon = Boolean(option.disabled) && !checked;
          const disabled = disableAll || comingSoon || Boolean(daysOutDisabledReason) || capDisabled;
          const labelTitle = comingSoon ? undefined : daysOutDisabledReason ?? (capDisabled ? capDisabledReason ?? "Limit reached." : undefined);
          return (
            <label
              key={option.value}
              className={`checkbox-card ${checked ? "checkbox-card-checked" : ""} ${disabled ? "checkbox-card-disabled" : ""}`.trim()}
              title={comingSoon ? "Coming soon" : labelTitle}
            >
              <input
                type="checkbox"
                checked={checked}
                disabled={disabled}
                aria-invalid={invalid ? true : undefined}
                aria-describedby={describedBy}
                onChange={() => onToggle(option.value)}
              />
              <span className="checkbox-card-copy">
                <span className="checkbox-card-title">
                  {option.label}
                  {comingSoon ? <span aria-label="Coming soon" role="img">{"\u00a0"}🚫</span> : null}
                </span>
              </span>
              {labelTitle ? (
                <WhyTooltip
                  title="Unavailable"
                  body={labelTitle}
                  triggerLabel="?"
                  ariaLabel={`Why ${option.label} is unavailable`}
                />
              ) : null}
            </label>
          );
        })}
      </div>
    </div>
  );
}

function OptionalDetails({ title, hint, comingSoon = false, children }: { title: string; hint?: string; comingSoon?: boolean; children: React.ReactNode }) {
  return (
    <details className="overview-disclosure onboarding-optional-disclosure">
      <summary className="overview-disclosure-summary">
        <div className="overview-disclosure-copy">
          <p className="overview-disclosure-title">
            {title} <span className="onboarding-optional-tag">Optional</span>
            {comingSoon ? <> <span className="badge coming-soon-badge">Coming soon</span></> : null}
          </p>
          {hint ? <p className="muted onboarding-optional-hint">{hint}</p> : null}
        </div>
        <span className="overview-disclosure-meta">
          <span className="overview-disclosure-chevron" aria-hidden="true" />
        </span>
      </summary>
      <div className="overview-disclosure-body">{children}</div>
    </details>
  );
}

function ReviewDetailList({ items }: { items: Array<{ label: string; value: string }> }) {
  return (
    <div className="review-detail-list">
      {items.map((item) => (
        <div key={`${item.label}-${item.value}`} className="review-detail-row">
          <p className="review-detail-label">{item.label}</p>
          <p className="review-detail-value">{item.value}</p>
        </div>
      ))}
    </div>
  );
}

function getReviewStepBlockingIssue(
  nextForm: PlanRequest,
  options: {
    hardSparringWarningLocked: boolean;
    noScheduledFight: boolean;
  },
): { message: string; step: number; fieldId: string } | null {
  if (!isValidRecordFormat(nextForm.athlete.record ?? "")) return { message: "Use x-x or x-x-x, like 5-1 or 12-2-1.", step: 0, fieldId: "record" };
  if (!nextForm.athlete.technical_style.length) return { message: "Select a combat sport to continue.", step: 0, fieldId: "technicalStyle" };
  if (!nextForm.fight_date && !options.noScheduledFight) return { message: "Choose a fight date or mark \"No scheduled fight\" to continue.", step: 1, fieldId: "fightDate" };
  if (!options.noScheduledFight && isFightDateInPast(nextForm.fight_date)) return { message: FIGHT_DATE_IN_PAST_MESSAGE, step: 1, fieldId: "fightDate" };
  if (!nextForm.training_availability.length) return { message: "Pick at least one training day to continue.", step: 2, fieldId: "trainingAvailabilityGroup" };
  if (!nextForm.weekly_training_frequency || nextForm.weekly_training_frequency < 1) return { message: "Training days per week must be at least 1.", step: 1, fieldId: "sessionsPerWeek" };
  if (!Number.isInteger(nextForm.weekly_training_frequency)) return { message: "Training days per week must be a whole number.", step: 1, fieldId: "sessionsPerWeek" };
  if (nextForm.weekly_training_frequency > 7) return { message: "Training days per week cannot exceed 7.", step: 1, fieldId: "sessionsPerWeek" };
  const parsedRounds = parseRoundsFormat(nextForm.rounds_format);
  if (!parsedRounds.roundCount) return { message: "Choose round count and duration to continue.", step: 1, fieldId: "roundCount" };
  if (!parsedRounds.roundDuration) return { message: "Choose round count and duration to continue.", step: 1, fieldId: "roundDuration" };
  if (options.hardSparringWarningLocked) {
    return { message: "Confirm the hard sparring warning on the Training step to continue.", step: 2, fieldId: "hardSparringAck" };
  }
  const focusValidation = validatePerformanceFocusSelections(
    nextForm.fight_date,
    { keyGoals: nextForm.key_goals, weakAreas: nextForm.weak_areas },
    { timeZone: nextForm.athlete.athlete_timezone },
  );
  if (focusValidation.isOverCap) {
    return {
      message: focusValidation.errorMessage ?? "Too many goals and weak areas. Remove some to continue.",
      step: PERFORMANCE_STEP_INDEX,
      fieldId: "keyGoalsGroup",
    };
  }
  return null;
}

function syncDeviceFields(current: PlanRequest, savedTimeZone?: string | null): PlanRequest {
  // The device zone only fills a first-time setup; it never overwrites a saved one.
  const detectedTimeZone = detectDeviceTimeZone();
  return {
    ...current,
    athlete: {
      ...current.athlete,
      athlete_timezone: savedTimeZone?.trim() || detectedTimeZone || current.athlete.athlete_timezone || "",
    },
  };
}

const WEEKDAY_FROM_ISO_DATE = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function getWeekdayFromIsoDate(dateValue: string): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateValue);
  if (!match) return null;

  const year = Number(match[1]);
  const monthIndex = Number(match[2]) - 1;
  const day = Number(match[3]);
  const parsed = new Date(Date.UTC(year, monthIndex, day));

  if (Number.isNaN(parsed.getTime())) return null;
  return WEEKDAY_FROM_ISO_DATE[parsed.getUTCDay()] ?? null;
}

type TrainingGateAction = "save_draft" | "next" | "step_select" | "generate";

type TrainingGateDecision =
  | { kind: "allow" }
  | { kind: "hard_error"; message: string; source: "availability" | "sparring" }
  | { kind: "warning_ack_required"; message: string; shouldRedirectToTraining: boolean };

export function PlanIntakeForm() {
  const t = useTranslations("Onboarding");
  const router = useRouter();
  const fadeNavigate = useFadeNavigate();
  const searchParams = useSearchParams();
  const refiningFromQuickBuild = searchParams.get("from") === "quick_build";
  const { me, session, replaceMe } = useAppSession();
  // Server-derived age band; the client never decides this.
  const isMinorAthlete = Boolean(me?.profile.is_minor);
  // Displayed age, recomputed from the profile's date of birth on every render
  // rather than read out of the form: it then follows a date-of-birth change in
  // Settings and rolls over on a birthday with nothing for the athlete to edit.
  const derivedAge = deriveAgeFromDateOfBirth(me?.profile.date_of_birth);
  const healthConsentGranted = hasHealthDataConsent(me);
  const [currentStep, setCurrentStep] = useState(0);
  const [isMobileProgressOpen, setIsMobileProgressOpen] = useState(false);
  const [form, setForm] = useState<PlanRequest>(emptyPlanRequest());
  const [guidedInjuries, setGuidedInjuries] = useState<GuidedInjuryState[]>([]);
  const [activeGuidedInjuryIndex, setActiveGuidedInjuryIndex] = useState<number | null>(null);
  const [isInjuryMapOpen, setIsInjuryMapOpen] = useState(true);
  const [noRestrictions, setNoRestrictions] = useState(true);
  const [showClearInjuriesConfirm, setShowClearInjuriesConfirm] = useState(false);
  const [bodyMapSide, setBodyMapSide] = useState<BodyMapSide>("front");
  const [noScheduledFight, setNoScheduledFight] = useState(false);
  const [pendingInjuryRemovalIndex, setPendingInjuryRemovalIndex] = useState<number | null>(null);
  const [hydrated, setHydrated] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();
  const [acknowledgedHardSparringWarningKey, setAcknowledgedHardSparringWarningKey] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [lastSavedAt, setLastSavedAt] = useState<number | null>(null);
  const [invalidFieldId, setInvalidFieldId] = useState<string | null>(null);
  const [validationFocusRequest, setValidationFocusRequest] = useState<{ fieldId: string; nonce: number } | null>(null);
  const lastSavedSnapshotRef = useRef<string>("");
  const issueRedirectConsumedRef = useRef(false);
  const pendingRemovalRef = useRef<HTMLDivElement | null>(null);
  const recordHasError = !isValidRecordFormat(form.athlete.record ?? "");

    // ── Days-out policy: compute field visibility/disablement ───────────
  const daysUntilFight = computeDaysUntilFight(form.fight_date);
  const hasHardSparring = form.hard_sparring_days.length > 0;
  const daysOutCtx: DaysOutContext = buildDaysOutContext(daysUntilFight, { hasHardSparring });
  const fightDateWeekday = noScheduledFight ? null : getWeekdayFromIsoDate(form.fight_date);
  // Only fight week locks the weekday out of the combat-load tags. Earlier
  // instances of the same weekday are ordinary training days, and the backend
  // fight-day override already clamps the real fight day on the final week.
  const lockedFightWeekday = getFightDayLockedWeekday(fightDateWeekday, daysUntilFight);

  useEffect(() => {
    if (!lockedFightWeekday) {
      return;
    }

    const lockedDay = lockedFightWeekday.trim().toLowerCase();

    setForm((current) => {
      const hardSparringDays = current.hard_sparring_days.filter(
        (day) => day.trim().toLowerCase() !== lockedDay,
      );
      const supportWorkDays = current.support_work_days.filter(
        (day) => day.trim().toLowerCase() !== lockedDay,
      );

      if (
        hardSparringDays.length === current.hard_sparring_days.length &&
        supportWorkDays.length === current.support_work_days.length
      ) {
        return current;
      }

      return {
        ...current,
        hard_sparring_days: hardSparringDays,
        support_work_days: supportWorkDays,
      };
    });
  }, [lockedFightWeekday]);

  useEffect(() => {
    if (!me || hydrated) {
      return;
    }
    const nextForm = syncDeviceFields(hydratePlanRequest(me), me.profile.athlete_timezone);
    const draft = (me.profile.onboarding_draft as DraftMetadata | null | undefined) ?? null;
    const nextGuidedInjuries = hydrateGuidedInjuryStates({
      injuries: nextForm.injuries,
      guided_injury: draft?.guided_injury ?? nextForm.guided_injury,
      guided_injuries: draft?.guided_injuries ?? nextForm.guided_injuries,
    });
    const nextGuidedInjuryFields = buildGuidedInjuryFields(nextGuidedInjuries);
    const hasStoredRestrictions = Boolean(
      nextGuidedInjuryFields.injuries || nextForm.injuries?.trim() || nextGuidedInjuries.some((injury) => hasGuidedInjuryContent(injury)),
    );

    setForm({
      ...nextForm,
      ...nextGuidedInjuryFields,
    });
    setGuidedInjuries(nextGuidedInjuries);
    setActiveGuidedInjuryIndex(nextGuidedInjuries.length ? 0 : null);
    setIsInjuryMapOpen(!nextGuidedInjuries[0]?.area.trim());
    setNoRestrictions(!hasStoredRestrictions);
    setNoScheduledFight(Boolean(draft?.no_scheduled_fight ?? nextForm.no_scheduled_fight));
    const savedStep = Number(draft?.current_step ?? 0);
    setCurrentStep(Number.isFinite(savedStep) ? Math.min(Math.max(savedStep, 0), steps.length - 1) : 0);
    setHydrated(true);
  }, [hydrated, me]);

  useEffect(() => {
    if (!healthConsentGranted) {
      setForm((current) => withoutIntakeHealthData(current));
      setGuidedInjuries([]);
      setNoRestrictions(true);
      setActiveGuidedInjuryIndex(null);
    }
  }, [healthConsentGranted]);

  useEffect(() => {
    // Skip top-scroll when a validation focus is pending — the focus effect
    // will scroll the user directly to the invalid field instead.
    if (validationFocusRequest) {
      return;
    }
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: 0, behavior: reducedMotion ? "instant" : "smooth" });
    // We intentionally do not depend on validationFocusRequest here — its
    // presence is checked at fire time, and adding it would re-trigger
    // scroll-to-top whenever a validation focus completes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentStep]);

  useEffect(() => {
    // When a removal is requested the confirm panel mounts above the map, which
    // can be off-screen on mobile. Bring it into view so the athlete sees the
    // confirm action instead of just the card's × turning red.
    if (pendingInjuryRemovalIndex === null) {
      return;
    }
    const node = pendingRemovalRef.current;
    if (!node) {
      return;
    }
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    node.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
  }, [pendingInjuryRemovalIndex]);

  useEffect(() => {
    if (!invalidFieldId) {
      return;
    }
    const parsed = parseRoundsFormat(form.rounds_format);
    const isFieldNowValid = (() => {
      switch (invalidFieldId) {
        case "fullName":
          return form.athlete.full_name.trim().length > 0;
        case "record":
          return isValidRecordFormat(form.athlete.record ?? "");
        case "technicalStyle":
          return form.athlete.technical_style.length > 0;
        case "fightDate":
          return Boolean(form.fight_date) || noScheduledFight;
        case "roundCount":
          return Boolean(parsed.roundCount);
        case "roundDuration":
          return Boolean(parsed.roundDuration);
        case "sessionsPerWeek":
          return Number.isInteger(form.weekly_training_frequency)
            && (form.weekly_training_frequency ?? 0) >= 1
            && (form.weekly_training_frequency ?? 0) <= 7;
        case "trainingAvailabilityGroup":
          return form.training_availability.length > 0;
        case "keyGoalsGroup":
          return !validatePerformanceFocusSelections(
            form.fight_date,
            { keyGoals: form.key_goals, weakAreas: form.weak_areas },
            { timeZone: form.athlete.athlete_timezone },
          ).isOverCap;
        case "hardSparringAck": {
          const warning = getHardSparringWarning(form.hard_sparring_days, form.weekly_training_frequency);
          const ack = acknowledgedHardSparringWarningKey === warning.acknowledgementContextKey;
          return !warning.requiresAcknowledgement || ack;
        }
        case "availabilityConsistencyAlert":
          return !getAvailabilityConsistency(form.training_availability, form.weekly_training_frequency).hardError;
        case "sparringConsistencyAlert":
          return !getSparringConsistency(
            form.training_availability,
            form.hard_sparring_days,
            form.support_work_days,
            !noScheduledFight,
            form.weekly_training_frequency,
          ).hardError;
        default:
          if (invalidFieldId.startsWith("guidedInjuryCard-")) {
            if (guidedInjuries.some((injury) => hasGuidedInjuryDescriptorWithoutArea(injury))) {
              return false;
            }
            if (noRestrictions) {
              return true;
            }
            return guidedInjuries.some(
              (injury) => Boolean(injury.injury_type) || Boolean(injury.notes.trim()),
            );
          }
          return false;
      }
    })();
    if (isFieldNowValid) {
      setInvalidFieldId(null);
      setError(null);
    }
  }, [
    invalidFieldId,
    form,
    noScheduledFight,
    acknowledgedHardSparringWarningKey,
    guidedInjuries,
    noRestrictions,
  ]);

  useEffect(() => {
    if (!validationFocusRequest) {
      return;
    }
    const { fieldId } = validationFocusRequest;
    let cancelled = false;
    let rafHandle: number | null = null;
    let timeoutHandle: number | null = null;

    function tryFocus(attemptsLeft: number) {
      if (cancelled) return;
      const el = document.getElementById(fieldId);
      if (!el) {
        // The target step may still be mounting (e.g. on cross-step navigation).
        // Retry a few frames before giving up so we never silently no-op.
        if (attemptsLeft > 0) {
          timeoutHandle = window.setTimeout(() => tryFocus(attemptsLeft - 1), 40);
        }
        return;
      }

      // Expand any ancestor <details> blocks (e.g. "Add more detail") so the field is visible.
      let cursor: HTMLElement | null = el.parentElement;
      while (cursor) {
        if (cursor instanceof HTMLDetailsElement && !cursor.open) {
          cursor.open = true;
        }
        cursor = cursor.parentElement;
      }

      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      el.scrollIntoView({ behavior: reducedMotion ? "instant" : "smooth", block: "start" });

      const isMobile = window.matchMedia("(max-width: 720px)").matches;
      const isTextInput = el instanceof HTMLInputElement && /^(text|email|number|tel|search|url)$/i.test(el.type);
      const isTextarea = el instanceof HTMLTextAreaElement;
      // Skip auto-focus on mobile for text inputs to avoid the keyboard popping up
      // unexpectedly; visual highlight + scroll still leads the user to the field.
      const skipFocus = isMobile && (isTextInput || isTextarea);
      const focusable = el instanceof HTMLInputElement
        || el instanceof HTMLTextAreaElement
        || el instanceof HTMLSelectElement
        || el instanceof HTMLButtonElement
        || el.hasAttribute("tabindex");
      if (!skipFocus && focusable) {
        el.focus({ preventScroll: true });
      }
    }

    // Wait one frame so React has committed the latest render (step change,
    // conditional rendering of the warning panel) before we look for the
    // element. Up to 5 retries handle slower mounts on cross-step nav.
    rafHandle = window.requestAnimationFrame(() => tryFocus(5));

    return () => {
      cancelled = true;
      if (rafHandle !== null) window.cancelAnimationFrame(rafHandle);
      if (timeoutHandle !== null) window.clearTimeout(timeoutHandle);
    };
  }, [validationFocusRequest]);

  const performanceFocusValidation = validatePerformanceFocusSelections(
    form.fight_date,
    {
      keyGoals: form.key_goals,
      weakAreas: form.weak_areas,
    },
    {
      timeZone: form.athlete.athlete_timezone,
    },
  );

  useEffect(() => {
    if (!hydrated || daysUntilFight === null) {
      return;
    }

    const currentDaysOutCtx = buildDaysOutContext(daysUntilFight, { hasHardSparring });
    const nextKeyGoals = filterAvailablePerformanceFocusValues(currentDaysOutCtx, "key_goals", form.key_goals);
    const nextWeakAreas = filterAvailablePerformanceFocusValues(currentDaysOutCtx, "weak_areas", form.weak_areas);
    if (nextKeyGoals.length === form.key_goals.length && nextWeakAreas.length === form.weak_areas.length) {
      return;
    }

    const removedStrengthForHardSparring =
      currentDaysOutCtx.hasHardSparring &&
      currentDaysOutCtx.daysOut !== null &&
      currentDaysOutCtx.daysOut <= 20 &&
      (form.key_goals.includes("strength") || form.weak_areas.includes("strength"));

    setForm((current) => {
      const nextDaysOutCtx = buildDaysOutContext(daysUntilFight, {
        hasHardSparring: current.hard_sparring_days.length > 0,
      });
      const filteredKeyGoals = filterAvailablePerformanceFocusValues(nextDaysOutCtx, "key_goals", current.key_goals);
      const filteredWeakAreas = filterAvailablePerformanceFocusValues(nextDaysOutCtx, "weak_areas", current.weak_areas);

      return {
        ...current,
        key_goals: filteredKeyGoals,
        primary_goal: current.primary_goal && !filteredKeyGoals.includes(current.primary_goal) ? "" : current.primary_goal,
        weak_areas: filteredWeakAreas,
        primary_weak_area: current.primary_weak_area && !filteredWeakAreas.includes(current.primary_weak_area) ? "" : current.primary_weak_area,
      };
    });
    setMessage(
      removedStrengthForHardSparring
        ? HARD_SPARRING_STRENGTH_REMOVAL_MESSAGE
        : "Some picks were removed: not available this close to fight day.",
    );
    setError(null);
  }, [daysUntilFight, form.key_goals, form.weak_areas, hasHardSparring, hydrated]);

  useEffect(() => {
    if (!hydrated) return;
    setForm((current) => {
      const next: PlanRequest = { ...current };
      let changed = false;
      const selectedGoals = current.key_goals;
      const selectedWeakAreas = current.weak_areas;

      if (selectedGoals.length === 1) {
        const onlyGoal = selectedGoals[0];
        if (current.primary_goal !== onlyGoal) {
          next.primary_goal = onlyGoal;
          changed = true;
        }
      } else if (current.primary_goal && !selectedGoals.includes(current.primary_goal)) {
        next.primary_goal = "";
        changed = true;
      }

      if (selectedWeakAreas.length === 1) {
        const onlyWeakArea = selectedWeakAreas[0];
        if (current.primary_weak_area !== onlyWeakArea) {
          next.primary_weak_area = onlyWeakArea;
          changed = true;
        }
      } else if (current.primary_weak_area && !selectedWeakAreas.includes(current.primary_weak_area)) {
        next.primary_weak_area = "";
        changed = true;
      }

      return changed ? next : current;
    });
  }, [form.key_goals, form.primary_goal, form.primary_weak_area, form.weak_areas, hydrated]);

  useEffect(() => {
    if (!hydrated) return;
    setForm(withoutComingSoonFields);
  }, [hydrated]);

  useEffect(() => {
    if (!hydrated) return;
    setForm((current) => {
      const collisionMetadata = sanitizeCollisionMetadata(current);
      const currentTags = current.goal_weakness_collision_tags ?? [];
      const nextTags = collisionMetadata.goal_weakness_collision_tags ?? [];
      const tagsChanged = currentTags.join("|") !== nextTags.join("|");
      const detailChanged = (current.goal_weakness_collision_detail ?? "") !== collisionMetadata.goal_weakness_collision_detail;
      const detailsChanged = JSON.stringify(current.goal_weakness_collision_details ?? []) !== JSON.stringify(collisionMetadata.goal_weakness_collision_details ?? []);

      if (!tagsChanged && !detailChanged && !detailsChanged) {
        return current;
      }

      return {
        ...current,
        ...collisionMetadata,
      };
    });
  }, [
    form.athlete.tactical_style,
    form.athlete.technical_style,
    form.goal_weakness_collision_detail,
    form.goal_weakness_collision_tags,
    form.goal_weakness_collision_details,
    form.key_goals,
    form.weak_areas,
    hydrated,
  ]);


  useEffect(() => {
    if (!hydrated || issueRedirectConsumedRef.current) {
      return;
    }

    const params = new URLSearchParams(window.location.search);
    if (params.get("issue") !== "focus-cap") {
      return;
    }

    issueRedirectConsumedRef.current = true;
    setMessage(null);
    setError(
      performanceFocusValidation.errorMessage
        ?? "This intake has too many focus picks. Remove some goals or weak areas before generating.",
    );
    setInvalidFieldId("keyGoalsGroup");
    setValidationFocusRequest({ fieldId: "keyGoalsGroup", nonce: Date.now() });
    setCurrentStep(PERFORMANCE_STEP_INDEX);
    setIsMobileProgressOpen(true);
    router.replace("/onboarding", { scroll: false });
  }, [hydrated, performanceFocusValidation.errorMessage, router]);

  function buildFormSnapshot(
    currentForm: PlanRequest = form,
    currentGuidedInjuries: GuidedInjuryState[] = guidedInjuries,
    currentNoRestrictions: boolean = noRestrictions,
  ): PlanRequest {
    const nextGuidedInjuryFields = buildGuidedInjuryFields(currentGuidedInjuries, {
      noRestrictions: currentNoRestrictions,
    });
    const withGuidedAndCollision = {
      ...withoutComingSoonFields(currentForm),
      ...nextGuidedInjuryFields,
      ...sanitizeCollisionMetadata({
        ...currentForm,
        ...nextGuidedInjuryFields,
      }),
    };
    const safeForm = healthConsentGranted ? withGuidedAndCollision : withoutIntakeHealthData(withGuidedAndCollision);
    return canonicalizePerformanceFocus(syncDeviceFields(applyNoScheduledFightSnapshot(safeForm, noScheduledFight), me?.profile.athlete_timezone));
  }

  function syncGuidedInjuryFields(nextGuidedInjuries: GuidedInjuryState[], nextNoRestrictions: boolean) {
    const nextGuidedInjuryFields = buildGuidedInjuryFields(nextGuidedInjuries, {
      noRestrictions: nextNoRestrictions,
    });
    setNoRestrictions(nextNoRestrictions);
    setGuidedInjuries(nextGuidedInjuries);
    setForm((currentForm) => ({
      ...currentForm,
      ...nextGuidedInjuryFields,
    }));
  }

  function updateAthlete<K extends keyof PlanRequest["athlete"]>(key: K, value: PlanRequest["athlete"][K]) {
    setForm((current) => ({
      ...current,
      athlete: {
        ...current.athlete,
        [key]: value,
      },
    }));
  }

  function updateField<K extends keyof PlanRequest>(key: K, value: PlanRequest[K]) {
    setForm((current) => ({
      ...current,
      [key]: value,
    }));
  }

  function updateRoundsField(key: "roundCount" | "roundDuration", value: string) {
    const parsed = parseRoundsFormat(form.rounds_format);
    const nextRounds = key === "roundCount"
      ? buildRoundsFormat(value, parsed.roundDuration)
      : buildRoundsFormat(parsed.roundCount, value);
    updateField("rounds_format", nextRounds);
  }

  function updateGuidedInjury<K extends keyof GuidedInjuryState>(index: number, key: K, value: GuidedInjuryState[K]) {
    setGuidedInjuries((currentGuidedInjuries) => {
      const nextGuidedInjuries = [...currentGuidedInjuries];
      nextGuidedInjuries[index] = coerceGuidedInjuryEditState({
        ...(nextGuidedInjuries[index] ?? EMPTY_GUIDED_INJURY),
        [key]: value,
      });

      const nextGuidedInjuryFields = buildGuidedInjuryFields(nextGuidedInjuries, {
        noRestrictions: false,
      });

      setNoRestrictions(false);
      setForm((currentForm) => ({
        ...currentForm,
        ...nextGuidedInjuryFields,
      }));

      return nextGuidedInjuries;
    });
  }

  function handleEditGuidedInjury(index: number) {
    setActiveGuidedInjuryIndex(index);
    setIsInjuryMapOpen(false);
    setChangingInjuryAreaIndex(null);
  }

  function handleNoRestrictionsChange(checked: boolean) {
    if (!checked) {
      setIsInjuryMapOpen(true);
      setShowClearInjuriesConfirm(false);
      const nextGuidedInjuries = guidedInjuries.length ? guidedInjuries : [{ ...EMPTY_GUIDED_INJURY }];
      syncGuidedInjuryFields(nextGuidedInjuries, false);
      setActiveGuidedInjuryIndex(nextGuidedInjuries.length - 1);
      return;
    }
    if (guidedInjuries.length > 0) {
      setShowClearInjuriesConfirm(true);
      return;
    }
    syncGuidedInjuryFields([], true);
    setActiveGuidedInjuryIndex(null);
  }

  function handleAddGuidedInjury() {
    setIsInjuryMapOpen(true);
    const nextGuidedInjuries = [...guidedInjuries, { ...EMPTY_GUIDED_INJURY }];
    syncGuidedInjuryFields(nextGuidedInjuries, false);
    setActiveGuidedInjuryIndex(nextGuidedInjuries.length - 1);
  }

  function handleConfirmClearInjuries() {
    setShowClearInjuriesConfirm(false);
    syncGuidedInjuryFields([], true);
    setActiveGuidedInjuryIndex(null);
  }

  const [changingInjuryAreaIndex, setChangingInjuryAreaIndex] = useState<number | null>(null);
  const injuryMapRef = useRef<HTMLDivElement>(null);

  function handleBodyMapZoneSelect(zoneKey: string, label: string) {
    setIsInjuryMapOpen(false);
    if (changingInjuryAreaIndex !== null && guidedInjuries[changingInjuryAreaIndex]) {
      const next = [...guidedInjuries];
      next[changingInjuryAreaIndex] = { ...next[changingInjuryAreaIndex], area: label, zone: zoneKey };
      syncGuidedInjuryFields(next, false);
      setActiveGuidedInjuryIndex(changingInjuryAreaIndex);
      setChangingInjuryAreaIndex(null);
      return;
    }
    // Match by the stable zone key first; fall back to a legacy injury whose
    // typed area still equals the zone label and has no zone key yet.
    const existingIndex = guidedInjuries.findIndex(
      (injury) =>
        (injury.zone && injury.zone === zoneKey) ||
        (!injury.zone && injury.area.trim().toLowerCase() === label.toLowerCase()),
    );
    if (existingIndex >= 0) {
      // Reopen the selected injury without changing its functional impact.
      // Backfill the stable zone key for legacy matches.
      const existing = guidedInjuries[existingIndex];
      const nextGuidedInjuries = [...guidedInjuries];
      nextGuidedInjuries[existingIndex] = coerceGuidedInjuryEditState({
        ...existing,
        zone: existing.zone || zoneKey,
      });
      syncGuidedInjuryFields(nextGuidedInjuries, false);
      // Reopen the affected injury.
      setActiveGuidedInjuryIndex(existingIndex);
      return;
    }

    const emptyIndex = guidedInjuries.findIndex((injury) => !injury.area.trim() && !injury.zone);
    if (emptyIndex >= 0) {
      const nextGuidedInjuries = [...guidedInjuries];
      nextGuidedInjuries[emptyIndex] = coerceGuidedInjuryEditState({
        ...nextGuidedInjuries[emptyIndex],
        area: label,
        zone: zoneKey,
      });
      syncGuidedInjuryFields(nextGuidedInjuries, false);
      setActiveGuidedInjuryIndex(emptyIndex);
      return;
    }

    const nextGuidedInjuries = [...guidedInjuries, { ...EMPTY_GUIDED_INJURY, area: label, zone: zoneKey }];
    syncGuidedInjuryFields(nextGuidedInjuries, false);
    setActiveGuidedInjuryIndex(nextGuidedInjuries.length - 1);
  }

  // Removal now only happens from a card's × button. Guard it with the existing
  // confirm panel when the injury carries detail beyond its area/zone, so a
  // single tap can't silently discard filled-in safety answers.
  function handleRequestRemoveGuidedInjury(index: number) {
    const injury = guidedInjuries[index];
    if (injury && hasGuidedInjuryContent({ ...injury, area: "", zone: "" })) {
      setPendingInjuryRemovalIndex(index);
      return;
    }
    handleRemoveGuidedInjury(index);
  }

  function handleConfirmRemovePendingInjury() {
    if (pendingInjuryRemovalIndex === null) {
      return;
    }
    handleRemoveGuidedInjury(pendingInjuryRemovalIndex);
    setPendingInjuryRemovalIndex(null);
  }

  function handleCancelRemovePendingInjury() {
    setPendingInjuryRemovalIndex(null);
  }

  function handleRemoveGuidedInjury(index: number) {
    const nextGuidedInjuries = guidedInjuries.filter((_, currentIndex) => currentIndex !== index);
    if (!nextGuidedInjuries.length) {
      syncGuidedInjuryFields([], true);
      setActiveGuidedInjuryIndex(null);
      return;
    }

    syncGuidedInjuryFields(nextGuidedInjuries, false);
    setActiveGuidedInjuryIndex((currentIndex) => {
      if (currentIndex === null) {
        return null;
      }
      if (currentIndex === index) {
        return Math.min(index, nextGuidedInjuries.length - 1);
      }
      return currentIndex > index ? currentIndex - 1 : currentIndex;
    });
  }

  function toggleFieldValue(
    key: "training_availability" | "equipment_access" | "key_goals" | "weak_areas" | "hard_sparring_days" | "support_work_days",
    value: string,
  ) {
    setForm((current) => {
      const currentValues = key === "equipment_access"
        ? retainKnownOptionValues(
            canonicalizeEquipmentAccessValues(current[key]),
            EQUIPMENT_ACCESS_OPTIONS,
          )
        : current[key];
      const alreadySelected = currentValues.includes(value);
      const performanceFocusGroup = getPerformanceFocusGroupForField(key);
      const performanceFocusCap = performanceFocusGroup
        ? getPerformanceFocusCap(current.fight_date, { timeZone: current.athlete.athlete_timezone })
        : null;
      const totalSelectedPerformanceFocus = current.key_goals.length + current.weak_areas.length;
      const currentDaysOutCtx = buildDaysOutContext(computeDaysUntilFight(current.fight_date), {
        hasHardSparring: current.hard_sparring_days.length > 0,
      });

      if (
        performanceFocusGroup
        && !alreadySelected
        && !getPerformanceFocusOptionAvailability(currentDaysOutCtx, performanceFocusGroup, value).available
      ) {
        return current;
      }

      if (performanceFocusGroup && !alreadySelected && performanceFocusCap && totalSelectedPerformanceFocus >= performanceFocusCap.maxSelections) {
        return current;
      }
      if (key === "weak_areas" && !alreadySelected && current.weak_areas.length >= 2) {
        return current;
      }

      const nextValues = toggleListValue(currentValues, value);

      // When a training-availability day is unchecked, also strip it from the
      // hard sparring / Light Combat pickers so stale picks don't trigger the
      // sparring-consistency hard error.
      if (key === "training_availability" && alreadySelected) {
        return {
          ...current,
          training_availability: nextValues,
          hard_sparring_days: current.hard_sparring_days.filter((day) => day !== value),
          support_work_days: current.support_work_days.filter((day) => day !== value),
        };
      }

      return {
        ...current,
        [key]: nextValues,
      };
    });
  }

  function shouldEvaluateTrainingGate(action: TrainingGateAction, targetStep?: number): boolean {
    if (action === "generate") {
      return true;
    }

    if (action === "save_draft") {
      return currentStep === 2;
    }

    if (action === "next") {
      return currentStep >= 2;
    }

    return targetStep !== undefined && targetStep > currentStep && targetStep > 2;
  }

  function getTrainingGateDecision(
    nextForm: PlanRequest,
    action: TrainingGateAction,
    targetStep?: number,
  ): TrainingGateDecision {
    if (!shouldEvaluateTrainingGate(action, targetStep)) {
      return { kind: "allow" };
    }

    const availabilityConsistency = getAvailabilityConsistency(
      nextForm.training_availability,
      nextForm.weekly_training_frequency,
    );
    if (availabilityConsistency.hardError) {
      return {
        kind: "hard_error",
        source: "availability",
        message: `${availabilityConsistency.hardError} Reduce sessions or add more available days.`,
      };
    }

    const sparringConsistency = getSparringConsistency(
      nextForm.training_availability,
      nextForm.hard_sparring_days,
      nextForm.support_work_days,
      action !== "save_draft" && nextForm.no_scheduled_fight !== true,
      nextForm.weekly_training_frequency,
    );
    if (sparringConsistency.hardError) {
      return {
        kind: "hard_error",
        source: "sparring",
        message: sparringConsistency.hardError,
      };
    }

    const hardSparringWarning = getHardSparringWarning(
      nextForm.hard_sparring_days,
      nextForm.weekly_training_frequency,
    );
    const hardSparringWarningAcknowledged =
      acknowledgedHardSparringWarningKey === hardSparringWarning.acknowledgementContextKey;

    if (action !== "save_draft" && hardSparringWarning.requiresAcknowledgement && !hardSparringWarningAcknowledged) {
      return {
        kind: "warning_ack_required",
        message:
          action === "generate"
            ? "Confirm the hard sparring warning on the Training step before building your plan."
            : "Confirm the hard sparring warning before continuing.",
        shouldRedirectToTraining: action === "generate" || currentStep > 2 || (targetStep !== undefined && targetStep > 2),
      };
    }

    return { kind: "allow" };
  }

  function applyTrainingGate(nextForm: PlanRequest, action: TrainingGateAction, targetStep?: number): boolean {
    const decision = getTrainingGateDecision(nextForm, action, targetStep);
    if (decision.kind === "allow") {
      return true;
    }

    if (decision.kind === "warning_ack_required") {
      return reportInvalidField({
        message: decision.message,
        fieldId: "hardSparringAck",
        step: currentStep !== 2 ? 2 : undefined,
      });
    }

    // hard_error from training gate covers availability/sparring consistency on the Training step.
    // Route to the panel that actually surfaces the failing check.
    const hardErrorFieldId = decision.source === "sparring"
      ? "sparringConsistencyAlert"
      : "availabilityConsistencyAlert";
    return reportInvalidField({
      message: decision.message,
      fieldId: hardErrorFieldId,
      step: currentStep !== 2 ? 2 : undefined,
    });
  }

  function reportInvalidField(args: { message: string; fieldId: string; step?: number }): false {
    const { message, fieldId } = args;
    // Fall back to the field's home step so an invalid target on a different
    // step is never silently dropped when the caller omits `step`.
    const targetStep = args.step ?? resolveFieldStep(fieldId);
    setError(message);
    setInvalidFieldId(fieldId);
    // The nonce guarantees the focus effect re-fires even when validation
    // hits the same field twice in a row (e.g. user re-clicks Continue).
    setValidationFocusRequest({ fieldId, nonce: Date.now() });
    if (targetStep !== undefined && targetStep !== currentStep) {
      setCurrentStep(targetStep);
      setIsMobileProgressOpen(true);
    }
    return false;
  }

  // Sends the athlete to the field a checklist item is about and shows the
  // problem there, so nothing has to be fixed from the checklist itself.
  function handleFixCheck(check: StepValidationCheck) {
    if (!check.fix) return;
    setMessage(null);
    reportInvalidField({ message: check.label, fieldId: check.fix.fieldId, step: check.fix.step });
  }

  function validateCurrentStep(
    nextForm: PlanRequest,
    action: TrainingGateAction = "next",
    targetStep?: number,
  ): boolean {
    if (currentStep === 0) {
      if (!isValidRecordFormat(nextForm.athlete.record ?? "")) {
        return reportInvalidField({
          message: "Use x-x or x-x-x, like 5-1 or 12-2-1.",
          fieldId: "record",
        });
      }
      if (!nextForm.athlete.technical_style.length) {
        return reportInvalidField({
          message: "Select a combat sport before continuing.",
          fieldId: "technicalStyle",
        });
      }
    }
    if (currentStep === 1) {
      if (!nextForm.fight_date && !noScheduledFight) {
        return reportInvalidField({
          message: "Choose your fight date or mark \"No scheduled fight\" before continuing.",
          fieldId: "fightDate",
        });
      }
      if (!noScheduledFight && isFightDateInPast(nextForm.fight_date)) {
        return reportInvalidField({
          message: FIGHT_DATE_IN_PAST_MESSAGE,
          fieldId: "fightDate",
        });
      }
      const parsedRounds = parseRoundsFormat(nextForm.rounds_format);
      if (!parsedRounds.roundCount) {
        return reportInvalidField({
          message: "Choose both round count and round duration before continuing.",
          fieldId: "roundCount",
        });
      }
      if (!parsedRounds.roundDuration) {
        return reportInvalidField({
          message: "Choose both round count and round duration before continuing.",
          fieldId: "roundDuration",
        });
      }
    }
    if (currentStep === 2 && !nextForm.training_availability.length) {
      return reportInvalidField({
        message: "Pick at least one day you can train.",
        fieldId: "trainingAvailabilityGroup",
      });
    }
    if (currentStep === 3 && (nextForm.guided_injuries ?? []).some((injury) => hasGuidedInjuryDescriptorWithoutArea(injury))) {
      const invalidIndex = (nextForm.guided_injuries ?? []).findIndex((injury) => hasGuidedInjuryDescriptorWithoutArea(injury));
      return reportInvalidField({
        message: GUIDED_INJURY_MISSING_AREA_MESSAGE,
        fieldId: invalidIndex >= 0 ? `guidedInjuryCard-${invalidIndex}` : "guidedInjuriesSection",
      });
    }
    if (currentStep === 3 && !noRestrictions) {
      const hasMeaningfulDetail = guidedInjuries.some(
        (injury) => Boolean(injury.injury_type) || Boolean(injury.notes.trim()),
      );
      if (!hasMeaningfulDetail) {
        const targetIndex = guidedInjuries.findIndex(
          (injury) => !injury.injury_type && !injury.notes.trim(),
        );
        return reportInvalidField({
          message: "Add the type of injury or describe it, or tap \"No current injuries or restrictions\" to continue.",
          fieldId: targetIndex >= 0 ? `guidedInjuryCard-${targetIndex}` : "guidedInjuryCard-0",
        });
      }
    }
    if (currentStep === PERFORMANCE_STEP_INDEX) {
      const focusValidation = validatePerformanceFocusSelections(
        nextForm.fight_date,
        { keyGoals: nextForm.key_goals, weakAreas: nextForm.weak_areas },
        { timeZone: nextForm.athlete.athlete_timezone },
      );
      if (focusValidation.isOverCap) {
        return reportInvalidField({
          message: focusValidation.errorMessage ?? "Too many goals and weak areas. Remove some to continue.",
          fieldId: "keyGoalsGroup",
        });
      }
    }
    return applyTrainingGate(nextForm, action, targetStep);
  }

  function validateForGeneration(nextForm: PlanRequest): boolean {
    if (!validateCurrentStep(nextForm, "generate")) {
      return false;
    }
    if (!nextForm.athlete.technical_style.length) {
      return reportInvalidField({
        message: "Select a combat sport before generating your plan.",
        fieldId: "technicalStyle",
        step: 0,
      });
    }
    if (!nextForm.fight_date && !noScheduledFight) {
      return reportInvalidField({
        message: "Choose your fight date or mark \"No scheduled fight\" before generating your plan.",
        fieldId: "fightDate",
        step: 1,
      });
    }
    if (!noScheduledFight && isFightDateInPast(nextForm.fight_date)) {
      return reportInvalidField({
        message: FIGHT_DATE_IN_PAST_MESSAGE,
        fieldId: "fightDate",
        step: 1,
      });
    }
    if (!nextForm.training_availability.length) {
      return reportInvalidField({
        message: "Pick at least one day you can train.",
        fieldId: "trainingAvailabilityGroup",
        step: 2,
      });
    }
    if (!nextForm.weekly_training_frequency || nextForm.weekly_training_frequency < 1) {
      return reportInvalidField({
        message: "Training days per week must be at least 1.",
        fieldId: "sessionsPerWeek",
        step: 1,
      });
    }
    if (!Number.isInteger(nextForm.weekly_training_frequency)) {
      return reportInvalidField({
        message: "Training days per week must be a whole number.",
        fieldId: "sessionsPerWeek",
        step: 1,
      });
    }
    if (nextForm.weekly_training_frequency > 7) {
      return reportInvalidField({
        message: "Training days per week cannot exceed 7.",
        fieldId: "sessionsPerWeek",
        step: 1,
      });
    }
    const parsedRounds = parseRoundsFormat(nextForm.rounds_format);
    if (!parsedRounds.roundCount) {
      return reportInvalidField({
        message: "Choose both round count and round duration before generating your plan.",
        fieldId: "roundCount",
        step: 1,
      });
    }
    if (!parsedRounds.roundDuration) {
      return reportInvalidField({
        message: "Choose both round count and round duration before generating your plan.",
        fieldId: "roundDuration",
        step: 1,
      });
    }
    const focusValidation = validatePerformanceFocusSelections(
      nextForm.fight_date,
      {
        keyGoals: nextForm.key_goals,
        weakAreas: nextForm.weak_areas,
      },
      {
        timeZone: nextForm.athlete.athlete_timezone,
      },
    );
    if (focusValidation.isOverCap) {
      return reportInvalidField({
        message: focusValidation.errorMessage ?? "Too many goals and weak areas. Remove some to continue.",
        fieldId: "keyGoalsGroup",
        step: PERFORMANCE_STEP_INDEX,
      });
    }
    return true;
  }

  async function persistDraft(step = currentStep, snapshot?: PlanRequest) {
    if (!session?.access_token) {
      return;
    }
    const nextForm = snapshot ?? buildFormSnapshot();
    setForm(nextForm);
    setSaveStatus("saving");
    try {
      const nextDraft = {
        ...mergePlanRequestDraft(me?.profile.onboarding_draft as Record<string, unknown> | null | undefined, nextForm, step),
        ...nextForm,
        current_step: step,
        no_scheduled_fight: noScheduledFight,
      };
      await saveOnboardingDraft(session.access_token, {
        full_name: nextForm.athlete.full_name,
        technical_style: nextForm.athlete.technical_style,
        tactical_style: nextForm.athlete.tactical_style,
        stance: nextForm.athlete.stance,
        professional_status: nextForm.athlete.professional_status,
        record: nextForm.athlete.record,
        athlete_timezone: nextForm.athlete.athlete_timezone,
        onboarding_draft: nextDraft,
      });

      const nextMe = mergeSavedOnboardingDraft(me, nextDraft, nextForm.athlete);
      if (nextMe) {
        replaceMe(nextMe);
      }
      lastSavedSnapshotRef.current = JSON.stringify(nextForm);
      setSaveStatus("saved");
      setLastSavedAt(Date.now());
    } catch (persistError) {
      setSaveStatus("error");
      throw persistError;
    }
  }

  function handleSaveDraft() {
    setMessage(null);
    setError(null);
    setInvalidFieldId(null);
    startTransition(async () => {
      const nextForm = buildFormSnapshot();
      if (!validateCurrentStep(nextForm, "save_draft")) {
        return;
      }
      if (!session?.access_token) {
        setError("You must be signed in to save a draft.");
        return;
      }
      try {
        await persistDraft();
        setMessage("Draft saved.");
      } catch (draftError) {
        setError(draftError instanceof Error ? draftError.message : "Unable to save draft.");
      }
    });
  }

  function handleNext() {
    const nextStep = Math.min(currentStep + 1, steps.length - 1);
    setMessage(null);
    setError(null);
    setInvalidFieldId(null);
    startTransition(async () => {
      const nextForm = buildFormSnapshot();
      if (!validateCurrentStep(nextForm, "next", nextStep)) {
        return;
      }
      setCurrentStep(nextStep);
      setIsMobileProgressOpen(false);
      try {
        await persistDraft(nextStep);
      } catch {
        // Draft persistence is best-effort; navigation has already advanced.
      }
    });
  }

  function handleBack() {
    setError(null);
    setInvalidFieldId(null);
    setCurrentStep((step) => Math.max(step - 1, 0));
    setIsMobileProgressOpen(false);
  }

  function handleStepSelect(targetStep: number) {
    setMessage(null);
    setError(null);
    setInvalidFieldId(null);
    let nextForm: PlanRequest | null = null;
    function getNextForm() {
      nextForm ??= buildFormSnapshot();
      return nextForm;
    }
    if (targetStep === steps.length - 1 && targetStep > currentStep) {
      const reviewIssue = getReviewStepBlockingIssue(getNextForm(), {
        hardSparringWarningLocked,
        noScheduledFight,
      });
      if (reviewIssue) {
        reportInvalidField({
          message: reviewIssue.message,
          fieldId: reviewIssue.fieldId,
          step: reviewIssue.step,
        });
        return;
      }
    }
    if (!canSelectWizardStep({
      currentStep,
      targetStep,
      lastSelectableStep: steps.length,
      validateCurrentStep: () => validateCurrentStep(getNextForm(), "step_select", targetStep),
    })) {
      return;
    }
    setCurrentStep(targetStep);
    setIsMobileProgressOpen(false);

    if (!session?.access_token || !isValidRecordFormat(getNextForm().athlete.record ?? "")) {
      return;
    }

    startTransition(async () => {
      try {
        await persistDraft(targetStep);
      } catch {
        // Keep jump navigation responsive even if background draft persistence fails.
      }
    });
  }

  function handleGenerate() {
    setMessage(null);
    setError(null);
    setInvalidFieldId(null);
    startTransition(async () => {
      const nextForm = buildFormSnapshot();
      if (!validateForGeneration(nextForm)) {
        return;
      }
      if (!session?.access_token) {
        setError("You must be signed in to generate a plan.");
        return;
      }
      try {
        await persistDraft(steps.length - 1, nextForm);
        if (!writePendingGenerationPayload(nextForm, "self_serve")) {
          setError("Unable to prepare the generation payload. Reload and try again.");
          return;
        }
        markGenerationIntent();
        fadeNavigate(() => router.push("/generate"));
      } catch (draftError) {
        setError(draftError instanceof Error ? draftError.message : "Unable to prepare plan generation.");
      }
    });
  }

  const technicalStyleLabel = getOptionLabel(TECHNICAL_STYLE_OPTIONS, form.athlete.technical_style[0] ?? "") || "Not provided";
  const tacticalStyleLabel = getOptionLabel(TACTICAL_STYLE_OPTIONS, form.athlete.tactical_style[0] ?? "") || "Not provided";
  const statusLabel = getOptionLabel(PROFESSIONAL_STATUS_OPTIONS, form.athlete.professional_status ?? "") || "Not provided";
  const stanceLabel = getOptionLabel(STANCE_OPTIONS, form.athlete.stance ?? "") || "Not provided";
  const parsedRounds = parseRoundsFormat(form.rounds_format);
  const availabilityConsistency = getAvailabilityConsistency(
    form.training_availability,
    form.weekly_training_frequency,
  );
  const selectedTrainingAvailabilityLabels = getOptionLabels(TRAINING_AVAILABILITY_OPTIONS, form.training_availability);
  const selectedEquipmentAccessLabels = getOptionLabels(EQUIPMENT_ACCESS_OPTIONS, form.equipment_access);
  const selectedHardSparringLabels = getOptionLabels(TRAINING_AVAILABILITY_OPTIONS, form.hard_sparring_days);
  const selectedSupportWorkLabels = getOptionLabels(TRAINING_AVAILABILITY_OPTIONS, form.support_work_days);
  const selectedGoalLabels = getOptionLabels(KEY_GOAL_OPTIONS, form.key_goals);
  const selectedWeakAreaLabels = getOptionLabels(WEAK_AREA_OPTIONS, form.weak_areas);
  const goalWeakAreaOverlaps = getGoalWeakAreaOverlaps(form.key_goals, form.weak_areas);
  const primaryOverlap = goalWeakAreaOverlaps[0] ?? null;
  const overlapClarificationPrompt = goalWeakAreaOverlaps.length > 1
    ? "You selected multiple qualities as both goals and weak areas. Clarify each one if useful."
    : primaryOverlap
      ? `You selected ${primaryOverlap.label} as both a goal and a weak area. What does that mean?`
      : "";
  const overlapReviewLabel = goalWeakAreaOverlaps.length > 1 ? "Multiple qualities" : goalWeakAreaOverlaps[0]?.label ?? "";
  const primaryGoalLabel = getOptionLabel(KEY_GOAL_OPTIONS, form.primary_goal ?? "") || "Not selected";
  const primaryWeakAreaLabel = getOptionLabel(WEAK_AREA_OPTIONS, form.primary_weak_area ?? "") || "Not selected";
  const secondaryGoalLabels = getOptionLabels(KEY_GOAL_OPTIONS, form.key_goals.filter((goal) => goal !== form.primary_goal));
  const secondaryWeakAreaLabels = getOptionLabels(WEAK_AREA_OPTIONS, form.weak_areas.filter((area) => area !== form.primary_weak_area));
  const performanceFocusCap = getPerformanceFocusCap(form.fight_date, {
    timeZone: form.athlete.athlete_timezone,
  });
  const selectedPerformanceFocusCount = performanceFocusValidation.totalSelections;
  const performanceFocusCapValue = performanceFocusCap?.maxSelections ?? null;
  const performanceFocusCapReached = performanceFocusCapValue !== null && selectedPerformanceFocusCount >= performanceFocusCapValue;
  const performanceFocusCapExceeded = performanceFocusValidation.isOverCap;
  const getKeyGoalDisabledReason = (option: IntakeOption) => {
    const availability = getPerformanceFocusOptionAvailability(daysOutCtx, "key_goals", option.value);
    return availability.available ? null : availability.reason ?? "Too close to fight day.";
  };
  const getWeakAreaDisabledReason = (option: IntakeOption) => {
    const availability = getPerformanceFocusOptionAvailability(daysOutCtx, "weak_areas", option.value);
    return availability.available ? null : availability.reason ?? "Too close to fight day.";
  };
  const remainingPerformanceFocusSelections = performanceFocusCapValue === null
    ? null
    : Math.max(performanceFocusCapValue - selectedPerformanceFocusCount, 0);
  const performanceFocusWindowLabel = performanceFocusCap?.windowLabel.toLowerCase() ?? "this camp window";
  const performanceFocusReason = performanceFocusCap?.reason ?? "";
  const selectedTrainingAvailability = formatJoinedLabels(selectedTrainingAvailabilityLabels, "No availability selected");
  const selectedEquipmentAccess = formatJoinedLabels(selectedEquipmentAccessLabels, "No equipment selected");
  const selectedHardSparring = formatJoinedLabels(selectedHardSparringLabels, "No fixed hard sparring days");
  const selectedSupportWorkDays = formatJoinedLabels(selectedSupportWorkLabels, "No Light Combat days selected");
  const fightDayLockReason = `Fight day: ${formatFightDateValue(form.fight_date)}`;
  const selectedGoals = formatJoinedLabels(selectedGoalLabels, "No goals selected");
  const selectedWeakAreas = formatJoinedLabels(selectedWeakAreaLabels, "No weak areas selected");
  const performanceFocusCapTitle = performanceFocusCapValue === null
    ? noScheduledFight
      ? "No limit on picks without a fight date"
      : "Add your fight date to see how many you can pick"
    : `${selectedPerformanceFocusCount} of ${performanceFocusCapValue} picks used`;
  const performanceFocusCapBadge = performanceFocusCapValue === null
    ? "No limit"
    : `${selectedPerformanceFocusCount}/${performanceFocusCapValue}`;
  const performanceFocusCapDetail = performanceFocusCapValue === null
    ? noScheduledFight
      ? "Once you add a fight date, goals and weak areas share a limit so the plan fits the time you have."
      : "Once your fight date is set, goals and weak areas share a limit so the plan fits the time you have."
    : performanceFocusCapExceeded
      ? `Up to ${performanceFocusCapValue} goals and weak areas in total for ${performanceFocusWindowLabel}. ${performanceFocusReason} Remove ${selectedPerformanceFocusCount - performanceFocusCapValue} to continue.`
      : performanceFocusCapReached
        ? `Up to ${performanceFocusCapValue} goals and weak areas in total for ${performanceFocusWindowLabel}. ${performanceFocusReason} Limit reached. Remove one to swap.`
        : `Up to ${performanceFocusCapValue} goals and weak areas in total for ${performanceFocusWindowLabel}. ${performanceFocusReason} ${remainingPerformanceFocusSelections} left.`;
  const performanceFocusCapHint = performanceFocusCapValue === null
    ? noScheduledFight
      ? "No fight date yet, so pick whatever matters most."
      : "Your fight date sets how many you can pick."
    : performanceFocusCapExceeded
      ? `Remove ${selectedPerformanceFocusCount - performanceFocusCapValue} to continue.`
      : performanceFocusCapReached
        ? "Limit reached. Remove one to swap."
        : remainingPerformanceFocusSelections === 1
          ? "1 pick remaining."
          : `${remainingPerformanceFocusSelections} picks remaining.`;
  const keyGoalCapDisabledReason = performanceFocusCapReached ? FOCUS_CAP_DISABLED_REASON : undefined;
  const weakAreaCapDisabledReason = form.weak_areas.length >= 2
    ? "You can pick up to 2 weak areas. Remove one to add another."
    : performanceFocusCapReached
      ? FOCUS_CAP_DISABLED_REASON
      : undefined;
  // A draft written before target-weight suppression can still carry one, so
  // the derived cut status and the review row are suppressed for a minor too —
  // otherwise a hidden field would keep surfacing through the summaries.
  const weightCutStatus = isMinorAthlete
    ? null
    : formatWeightCutStatus(form.athlete.weight_kg, form.athlete.target_weight_kg);
  const equipmentLimitations = formatEquipmentLimitations(form.equipment_access);
  const sparringConsistency = getSparringConsistency(
    form.training_availability,
    form.hard_sparring_days,
    form.support_work_days,
    !noScheduledFight,
    form.weekly_training_frequency,
  );
  const hardSparringWarning = getHardSparringWarning(
    form.hard_sparring_days,
    form.weekly_training_frequency,
  );
  const hardSparringWarningAcknowledged =
    acknowledgedHardSparringWarningKey === hardSparringWarning.acknowledgementContextKey;
  const hardSparringWarningLocked =
    hardSparringWarning.requiresAcknowledgement && !hardSparringWarningAcknowledged;
  const trainingPreferenceText = (form.training_preference || "").trim();
  const mindsetChallengesText = (form.mindset_challenges || "").trim();
  const notesText = (form.notes || "").trim();
  const sparringCollisionRisk = formatSparringCollisionRisk({
    fatigueLevel: form.fatigue_level || "low",
    injuries: form.injuries || "",
    sessionsPerWeek: form.weekly_training_frequency,
    technicalStyle: form.athlete.technical_style[0] ?? "",
    hardSparringDays: selectedHardSparringLabels,
  });
  const highFatigueFlag = (form.fatigue_level || "low") === "high" ? "High fatigue already reported" : null;
  const hasExtraPerformanceNotes = Boolean(mindsetChallengesText || notesText);
  const hasTrainingPreference = Boolean(trainingPreferenceText);
  const restrictionSummary = formatRestrictionSummary(guidedInjuries, form.injuries);
  const sexLabel = form.athlete.sex
    ? SEX_OPTIONS.find((option) => option.value === form.athlete.sex)?.label ?? formatValue(form.athlete.sex)
    : "Not provided";
  const profileReviewItems = [
    { label: "Name", value: formatValue(form.athlete.full_name) },
    ...(hasValue(form.athlete.sex) ? [{ label: "Sex", value: sexLabel }] : []),
    // Same derived value the profile step displays, not the form's copy.
    ...(derivedAge !== null ? [{ label: "Age", value: formatValue(derivedAge) }] : []),
    ...(hasValue(form.athlete.height_cm) ? [{ label: "Height", value: `${form.athlete.height_cm} cm` }] : []),
    ...(hasValue(form.athlete.weight_kg) ? [{ label: "Current weight", value: `${form.athlete.weight_kg} kg` }] : []),
    ...(!isMinorAthlete && hasValue(form.athlete.target_weight_kg)
      ? [{ label: "Target weight", value: `${form.athlete.target_weight_kg} kg` }]
      : []),
    { label: "Stance", value: stanceLabel },
    { label: "Combat sport", value: technicalStyleLabel },
    { label: "Tactical style", value: tacticalStyleLabel },
    { label: "Professional status", value: statusLabel },
    { label: "Record", value: formatValue(form.athlete.record) },
  ];
  const campSetupReviewItems = [
    { label: "Fight date", value: formatFightDateValue(form.fight_date) },
    { label: "Rounds", value: formatValue(form.rounds_format) },
    { label: "Training days per week", value: formatValue(form.weekly_training_frequency) },
    { label: "Fatigue level", value: formatValue(form.fatigue_level || "low") },
  ];
  const trainingReviewItems = [
    { label: "Training availability", value: selectedTrainingAvailability },
    { label: "Hard sparring days", value: selectedHardSparring },
    { label: "Light Combat days", value: selectedSupportWorkDays },
    { label: "Equipment access", value: selectedEquipmentAccess },
    ...(availabilityConsistency.hardError
      ? [{ label: "Schedule issue", value: availabilityConsistency.hardError }]
      : availabilityConsistency.softWarning
        ? [{ label: "Schedule note", value: availabilityConsistency.softWarning }]
        : []),
    ...(sparringConsistency.hardError
      ? [{ label: "Sparring schedule issue", value: sparringConsistency.hardError }]
      : sparringConsistency.softWarning
        ? [{ label: "Sparring schedule note", value: sparringConsistency.softWarning }]
        : []),
    ...(hardSparringWarning.message
      ? [{
          label: "Hard sparring load",
          value: hardSparringWarning.message,
        }]
      : []),
    {
      label: "Session preference",
      value: hasTrainingPreference ? trainingPreferenceText : "No session preference provided.",
    },
  ];
  const constraintsReviewItems = [
    { label: "Injuries / pain areas", value: restrictionSummary },
    ...(weightCutStatus ? [{ label: "Weight-cut status", value: weightCutStatus }] : []),
    ...(highFatigueFlag ? [{ label: "Fatigue flag", value: highFatigueFlag }] : []),
    ...(equipmentLimitations ? [{ label: "Equipment limitations", value: equipmentLimitations }] : []),
    ...(sparringCollisionRisk ? [{ label: "Sparring", value: sparringCollisionRisk }] : []),
  ];
  const performanceReviewItems = [
    { label: "Goals - Primary", value: primaryGoalLabel },
    { label: "Goals - Secondary", value: formatJoinedLabels(secondaryGoalLabels, "None") },
    { label: "Weak areas - Primary", value: primaryWeakAreaLabel },
    { label: "Weak areas - Secondary", value: formatJoinedLabels(secondaryWeakAreaLabels, "None") },
    ...(form.goal_weakness_collision_detail?.trim()
      ? [{
          label: "Priority clarification",
          value: `${overlapReviewLabel} - ${form.goal_weakness_collision_detail.trim()}`,
        }]
      : []),
    ...(mindsetChallengesText ? [{ label: "Mental / confidence issue", value: mindsetChallengesText }] : []),
    ...(notesText ? [{ label: "Anything else we should know?", value: notesText }] : []),
    ...(!hasExtraPerformanceNotes ? [{ label: "Extra context", value: "No extra context provided." }] : []),
  ];
  const reviewChecklistItems: StepValidationCheck[] = [
    {
      label: form.athlete.technical_style.length ? "Combat sport picked." : "Pick your combat sport.",
      status: form.athlete.technical_style.length ? "done" : "pending",
      fix: { fieldId: "technicalStyle", step: 0 },
    },
    {
      label: form.fight_date
        ? "Fight date set."
        : noScheduledFight
          ? "No fight scheduled yet."
          : "Add your fight date, or tick No scheduled fight yet.",
      status: form.fight_date || noScheduledFight ? "done" : "pending",
      fix: { fieldId: "fightDate", step: 1 },
    },
    {
      label: form.training_availability.length
        ? "Training days picked."
        : "Pick at least one day you can train.",
      status: form.training_availability.length ? "done" : "pending",
      fix: { fieldId: "trainingAvailabilityGroup", step: 2 },
    },
    {
      label:
        Number.isInteger(form.weekly_training_frequency) && (form.weekly_training_frequency ?? 0) >= 1 && (form.weekly_training_frequency ?? 0) <= 7
          ? "Training days per week set."
          : "Set training days per week (1 to 7).",
      status:
        Number.isInteger(form.weekly_training_frequency) && (form.weekly_training_frequency ?? 0) >= 1 && (form.weekly_training_frequency ?? 0) <= 7
          ? "done"
          : "pending",
      fix: { fieldId: "sessionsPerWeek", step: 1 },
    },
    {
      label:
        parsedRounds.roundCount && parsedRounds.roundDuration
          ? "Rounds and round length set."
          : "Pick how many rounds and how long each one is.",
      status: parsedRounds.roundCount && parsedRounds.roundDuration ? "done" : "pending",
      fix: { fieldId: parsedRounds.roundCount ? "roundDuration" : "roundCount", step: 1 },
    },
    ...(availabilityConsistency.hardError
      ? [{ label: availabilityConsistency.hardError, status: "warning" as const, fix: { fieldId: "availabilityConsistencyAlert", step: 2 } }]
      : [{ label: "Your planned training days fit your availability.", status: "done" as const }]),
    ...(sparringConsistency.hardError
      ? [{ label: sparringConsistency.hardError, status: "warning" as const, fix: { fieldId: "sparringConsistencyAlert", step: 2 } }]
      : [{ label: "Sparring and light days fit your training days.", status: "done" as const }]),
    ...(hardSparringWarning.message
      ? [{
          label: hardSparringWarningAcknowledged
            ? `${hardSparringWarning.message} Confirmed.`
            : `${hardSparringWarning.message} Confirm it on the Training step.`,
          status: hardSparringWarningAcknowledged ? "done" : "warning",
          fix: { fieldId: "hardSparringAck", step: 2 },
        } as const]
      : []),
  ];
  const currentStepValidation = (() => {
    switch (currentStep) {
      case 0:
        return {
          title: "About you",
          description: "Only your name and combat sport are needed.",
          checks: [
            {
              label: form.athlete.full_name.trim() ? "Name added." : "Add your name.",
              status: form.athlete.full_name.trim() ? "done" : "pending",
              fix: { fieldId: "fullName", step: 0 },
            },
            {
              label: recordHasError ? "Write your record like 5-1 or 12-2-1." : "Record looks good.",
              status: recordHasError ? "pending" : "done",
              fix: { fieldId: "record", step: 0 },
            },
            {
              label: form.athlete.technical_style.length ? "Combat sport picked." : "Pick your combat sport.",
              status: form.athlete.technical_style.length ? "done" : "pending",
              fix: { fieldId: "technicalStyle", step: 0 },
            },
          ] satisfies StepValidationCheck[],
        };
      case 1:
        return {
          title: "Your fight",
          description: "When you fight and how long the rounds are, so the plan fits the time you have.",
          checks: [
            {
              label: form.fight_date
                ? "Fight date set."
                : noScheduledFight
                  ? "No fight scheduled yet. You'll get an open plan."
                  : "Add your fight date, or tick No scheduled fight yet.",
              status: form.fight_date || noScheduledFight ? "done" : "pending",
              fix: { fieldId: "fightDate", step: 1 },
            },
            {
              label:
                parsedRounds.roundCount && parsedRounds.roundDuration
                  ? "Rounds and round length set."
                  : "Pick how many rounds and how long each one is.",
              status: parsedRounds.roundCount && parsedRounds.roundDuration ? "done" : "pending",
              fix: { fieldId: parsedRounds.roundCount ? "roundDuration" : "roundCount", step: 1 },
            },
            {
              label:
                Number.isInteger(form.weekly_training_frequency) && (form.weekly_training_frequency ?? 0) >= 1 && (form.weekly_training_frequency ?? 0) <= 7
                  ? "Training days per week set."
                  : "Set training days per week (1 to 7).",
              status:
                Number.isInteger(form.weekly_training_frequency) && (form.weekly_training_frequency ?? 0) >= 1 && (form.weekly_training_frequency ?? 0) <= 7
                  ? "done"
                  : "pending",
              fix: { fieldId: "sessionsPerWeek", step: 1 },
            },
          ] satisfies StepValidationCheck[],
        };
      case 2:
        return {
          title: "Your training week",
          description: "The days you can train and which ones are sparring.",
          checks: [
            {
              label: form.training_availability.length
                ? "Training days picked."
                : "Pick at least one day you can train.",
              status: form.training_availability.length ? "done" : "pending",
              fix: { fieldId: "trainingAvailabilityGroup", step: 2 },
            },
            {
              label: availabilityConsistency.hardError
                ? availabilityConsistency.hardError
                : "Your planned training days fit your availability.",
              status: availabilityConsistency.hardError ? "warning" : "done",
              fix: { fieldId: "availabilityConsistencyAlert", step: 2 },
            },
            {
              label: sparringConsistency.hardError
                ? sparringConsistency.hardError
                : "Sparring and light days fit your training days.",
              status: sparringConsistency.hardError ? "warning" : "done",
              fix: { fieldId: "sparringConsistencyAlert", step: 2 },
            },
            ...(hardSparringWarning.message
              ? [{
                  label: hardSparringWarningAcknowledged
                    ? "Hard sparring warning confirmed."
                    : "Confirm the hard sparring warning.",
                  status: hardSparringWarningAcknowledged ? "done" : "warning",
                  fix: { fieldId: "hardSparringAck", step: 2 },
                } as const]
              : []),
          ] satisfies StepValidationCheck[],
        };
      case 3: {
        const guidedAreaMismatch = (form.guided_injuries ?? []).some((injury) => hasGuidedInjuryDescriptorWithoutArea(injury));
        const mismatchIndex = (form.guided_injuries ?? []).findIndex((injury) => hasGuidedInjuryDescriptorWithoutArea(injury));
        return {
          title: "Injuries",
          description: "Tell us what hurts so the plan can train around it.",
          checks: [
            {
              label: noRestrictions || !guidedAreaMismatch
                ? "Injury details are complete."
                : GUIDED_INJURY_MISSING_AREA_MESSAGE,
              status: noRestrictions || !guidedAreaMismatch ? "done" : "pending",
              fix: { fieldId: `guidedInjuryCard-${Math.max(mismatchIndex, 0)}`, step: 3 },
            },
          ] satisfies StepValidationCheck[],
        };
      }
      case PERFORMANCE_STEP_INDEX:
        // The fight date is answered on the Fight step, so it is not a check
        // here: this step only lists what can be fixed on it. A missing date is
        // flagged on the goal-limit card instead, with a link back to the field.
        return {
          title: "Goals",
          description: "Pick what to improve. Closer to the fight, you can pick fewer.",
          checks: [
            {
              label: performanceFocusCapExceeded
                ? performanceFocusValidation.errorMessage ?? "Too many goals and weak areas. Remove some to continue."
                : performanceFocusCapTitle,
              status: performanceFocusCapExceeded ? "warning" : "done",
              fix: { fieldId: "keyGoalsGroup", step: PERFORMANCE_STEP_INDEX },
            },
            {
              label: performanceFocusCapDetail,
              status: performanceFocusCapExceeded ? "warning" : "done",
            },
          ] satisfies StepValidationCheck[],
        };
      default:
        return {
          title: "Ready to build",
          description: "Check your answers, fix anything still open, then build your plan.",
          checks: reviewChecklistItems,
        };
    }
  })();
  const unresolvedCurrentChecks = currentStepValidation.checks.filter((check) => check.status !== "done");
  const actionBarTitle = currentStep === steps.length - 1 ? "Generate plan" : `Step ${currentStep + 1}: ${steps[currentStep]}`;
  const actionBarSummary = unresolvedCurrentChecks.length
    ? currentStep === steps.length - 1
      ? `${unresolvedCurrentChecks.length} thing${unresolvedCurrentChecks.length === 1 ? "" : "s"} to fix before you build.`
      : `${unresolvedCurrentChecks.length} thing${unresolvedCurrentChecks.length === 1 ? "" : "s"} to fix before you continue.`
    : currentStep === steps.length - 1
      ? "Everything's ready. Build your plan."
      : "Ready to continue.";
  const formActionPending = isPending;

  return (
    <RequireAuth>
      <section className="panel onboarding-panel">
        <div className="section-heading onboarding-heading-desktop">
          <div className="athlete-motion-slot athlete-motion-header">
            <p className="kicker">{t("eyebrow")}</p>
            <h1>{t("title")}</h1>
            <p className="muted">Your answers save as you go.</p>
            <PlanSetupAlternatives />
          </div>
        </div>

        <MobileOnboardingHeader
          currentStep={currentStep}
          isOpen={isMobileProgressOpen}
          onToggle={() => setIsMobileProgressOpen((current) => !current)}
          onStepSelect={handleStepSelect}
          saveStatus={saveStatus}
          lastSavedAt={lastSavedAt}
          onRetrySave={handleSaveDraft}
          retrySaveDisabled={formActionPending}
        />

        <div className="athlete-motion-slot athlete-motion-status onboarding-progress-desktop">
          <OnboardingProgressStrip currentStep={currentStep} />
          <AutoSaveIndicator
            status={saveStatus}
            lastSavedAt={lastSavedAt}
            onRetry={handleSaveDraft}
            retryDisabled={formActionPending}
          />
          <StepPills currentStep={currentStep} onStepSelect={handleStepSelect} />
        </div>

        {daysOutCtx.uiHints.fight_proximity_banner ? (
          <div className="fight-proximity-banner" role="status">
            {daysOutCtx.uiHints.fight_proximity_banner}
          </div>
        ) : null}

        <StepValidationPanel
          stepLabel={steps[currentStep]}
          title={currentStepValidation.title}
          description={currentStepValidation.description}
          checks={currentStepValidation.checks}
          onFix={handleFixCheck}
          defaultOpen={currentStep === steps.length - 1}
        />

        {currentStep === 0 ? (
          <div className="step-layout onboarding-step-layout onboarding-step-layout-profile">
            <div className="step-main athlete-motion-slot athlete-motion-main onboarding-step-main">
              {refiningFromQuickBuild ? (
                <p className="quick-build-refine-notice" role="status">
                  Refining your Quick Build plan. It stays until you generate again.
                </p>
              ) : null}
              <OnboardingTrustNote />
              <article className="step-card">
                <div className="form-section-header">
                  <p className="kicker">Identity</p>
                  <h2 className="form-section-title">{t("coreDetails")}</h2>
                </div>
                <p className="muted">Only name and combat sport are required.</p>
                <div className="form-grid onboarding-profile-core-grid">
                  <div className={`field${invalidFieldId === "fullName" ? " field-invalid" : ""}`}>
                    <label htmlFor="fullName">Full name</label>
                    <input
                      id="fullName"
                      name="name"
                      autoComplete="name"
                      value={form.athlete.full_name}
                      onChange={(event) => updateAthlete("full_name", event.target.value)}
                      maxLength={ATHLETE_FULL_NAME_MAX}
                      required
                      aria-invalid={invalidFieldId === "fullName" ? true : undefined}
                      aria-describedby={invalidFieldId === "fullName" ? "fullName-error" : undefined}
                    />
                    {invalidFieldId === "fullName" && error ? (
                      <p id="fullName-error" className="error-text" role="alert">{error}</p>
                    ) : null}
                  </div>
                  <div className={`field${invalidFieldId === "technicalStyle" ? " field-invalid" : ""}`}>
                    <label htmlFor="technicalStyle">Combat sport</label>
                    <CustomSelect
                      id="technicalStyle"
                      value={form.athlete.technical_style?.[0] ?? ""}
                      options={TECHNICAL_STYLE_OPTIONS}
                      placeholder="Select combat sport"
                      includeEmptyOption
                      invalid={invalidFieldId === "technicalStyle"}
                      describedBy={invalidFieldId === "technicalStyle" ? "technicalStyle-error" : undefined}
                      onChange={(value) => updateAthlete("technical_style", value ? [value] : [])}
                    />
                    {invalidFieldId === "technicalStyle" && error ? (
                      <p id="technicalStyle-error" className="error-text" role="alert">{error}</p>
                    ) : null}
                  </div>
                  <div className="field">
                    <label htmlFor="tacticalStyle">Tactical Style</label>
                    <CustomSelect
                      id="tacticalStyle"
                      value={form.athlete.tactical_style[0] ?? ""}
                      options={TACTICAL_STYLE_OPTIONS}
                      placeholder="Select tactical style"
                      includeEmptyOption
                      onChange={(value) => updateAthlete("tactical_style", value ? [value] : [])}
                    />
                    <p className="muted">Tactical style = how you usually fight inside that sport.</p>
                  </div>
                </div>
              </article>

              <OptionalDetails
                title="Add more detail"
                hint="Body stats, stance, target weight, status, and record. Not required to generate a plan."
              >
                <div className="form-grid onboarding-profile-detail-grid">
                  <div className="field">
                    <label htmlFor="sex">Sex</label>
                    <CustomSelect
                      id="sex"
                      value={form.athlete.sex ?? ""}
                      options={SEX_OPTIONS}
                      placeholder="Select sex"
                      includeEmptyOption
                      onChange={(value) => updateAthlete("sex", (value || null) as PlanRequest["athlete"]["sex"])}
                    />
                  </div>
                  {/* Read-only on purpose. The date of birth collected at
                      signup is the single source of age truth, so an age typed
                      here could only ever disagree with it — which is how a
                      profile whose date of birth said 15 came to show 25 in
                      Camp Setup while the under-18 safety rules used 15. It is
                      shown, not edited; the date of birth is changed in
                      Settings, and the server re-derives the age from it before
                      every generation regardless of what is sent. */}
                  <div className="field">
                    <label id="ageLabel">Age</label>
                    <div className="readonly-field" aria-labelledby="ageLabel" data-testid="camp-setup-age">
                      {derivedAge ?? "Not provided"}
                    </div>
                    <p className="muted">
                      {derivedAge === null
                        ? "Add your date of birth in Settings to set this."
                        : "From your date of birth"}{" "}
                      · <Link href="/settings#account">Edit in Settings</Link>
                    </p>
                  </div>
                  <div className="field">
                    <label htmlFor="weightKg">Weight (kg)</label>
                    <input id="weightKg" type="number" min="0" step="0.1" inputMode="decimal" disabled={!healthConsentGranted} value={healthConsentGranted ? (form.athlete.weight_kg ?? "") : ""} onChange={(event) => updateAthlete("weight_kg", numberOrNull(event.target.value))} />
                    <p className="muted">{healthConsentGranted ? "Use current walking-around weight." : HEALTH_CONSENT_BLOCKED_MESSAGE}</p>
                  </div>
                  <div className="field">
                    <label htmlFor="heightCm">Height (cm)</label>
                    <input id="heightCm" type="number" min="0" step="1" inputMode="numeric" value={form.athlete.height_cm ?? ""} onChange={(event) => updateAthlete("height_cm", integerOrNull(event.target.value))} />
                  </div>
                  <div className="field">
                    <label htmlFor="stance">Stance</label>
                    <CustomSelect
                      id="stance"
                      value={form.athlete.stance ?? ""}
                      options={STANCE_OPTIONS}
                      placeholder="Select stance"
                      includeEmptyOption
                      onChange={(value) => updateAthlete("stance", value)}
                    />
                  </div>
                  {/* Not shown to under-18s. UNLXCK provides them no weight-cut
                      guidance, so a fight-week target has no feature to serve —
                      and the Children's Code says a child's data is minimised to
                      what the active feature needs. The backend strips the field
                      too, so hiding it here is presentation, not enforcement. */}
                  {isMinorAthlete ? null : (
                    <div className="field">
                      <label htmlFor="targetWeightKg">Target weight (kg)</label>
                      <input id="targetWeightKg" type="number" min="0" step="0.1" inputMode="decimal" disabled={!healthConsentGranted} value={healthConsentGranted ? (form.athlete.target_weight_kg ?? "") : ""} onChange={(event) => updateAthlete("target_weight_kg", numberOrNull(event.target.value))} />
                      <p className="muted">{healthConsentGranted ? "Use realistic fight-week target, not an ideal someday number." : HEALTH_CONSENT_BLOCKED_MESSAGE}</p>
                    </div>
                  )}
                  <div className="field">
                    <label htmlFor="status">Professional Status</label>
                    <CustomSelect
                      id="status"
                      value={form.athlete.professional_status ?? ""}
                      options={PROFESSIONAL_STATUS_OPTIONS}
                      placeholder="Select professional status"
                      includeEmptyOption
                      onChange={(value) => updateAthlete("professional_status", value)}
                    />
                  </div>
                  <div className={`field field-span-full${invalidFieldId === "record" || recordHasError ? " field-invalid" : ""}`}>
                    <label htmlFor="record">Record</label>
                    <input
                      id="record"
                      value={form.athlete.record ?? ""}
                      onChange={(event) => updateAthlete("record", sanitizeRecordInput(event.target.value))}
                      placeholder="5-1 or 12-2-1"
                      inputMode="text"
                      maxLength={RECORD_MAX}
                      aria-invalid={invalidFieldId === "record" || recordHasError ? true : undefined}
                      aria-describedby={invalidFieldId === "record" ? "record-error" : undefined}
                    />
                    <p className="muted">Use only <code>x-x</code> or <code>x-x-x</code>.</p>
                    {invalidFieldId === "record" && error ? (
                      <p id="record-error" className="error-text" role="alert">{error}</p>
                    ) : recordHasError ? (
                      <p className="error-text">{t("recordHint")}</p>
                    ) : null}
                  </div>
                </div>
              </OptionalDetails>
            </div>
          </div>
        ) : null}

        {currentStep === 1 ? (
          <div className="step-layout onboarding-step-layout onboarding-step-layout-restrictions">
            <div className="step-main athlete-motion-slot athlete-motion-main onboarding-step-main">
              <article className="step-card">
                <div className="form-section-header">
                  <p className="kicker">Fight context</p>
                  <h2 className="form-section-title">{t("timingLoad")}</h2>
                </div>
                <div className="form-grid onboarding-fight-grid">
                  <div className={`field${invalidFieldId === "fightDate" ? " field-invalid" : ""}`}>
                    <label htmlFor="fightDate">Fight date</label>
                    <input
                      id="fightDate"
                      type="date"
                      min={getTodayIsoDate()}
                      value={form.fight_date}
                      disabled={noScheduledFight}
                      onChange={(event) => updateField("fight_date", event.target.value)}
                      aria-invalid={invalidFieldId === "fightDate" ? true : undefined}
                      aria-describedby={invalidFieldId === "fightDate" ? "fightDate-error" : undefined}
                    />
                    <label className={`inline-warning-ack inline-warning-ack-subtle ${noScheduledFight ? "inline-warning-ack-checked" : ""}`.trim()}>
                      <input
                        type="checkbox"
                        checked={noScheduledFight}
                        onChange={(event) => {
                          const checked = event.target.checked;
                          setNoScheduledFight(checked);
                          setForm((current) => applyNoScheduledFightSnapshot(current, checked));
                        }}
                      />
                      <span className="inline-warning-ack-copy">No scheduled fight yet</span>
                    </label>
                    {invalidFieldId === "fightDate" && error ? (
                      <p id="fightDate-error" className="error-text" role="alert">{error}</p>
                    ) : null}
                  </div>
                  <div className={`field${invalidFieldId === "roundCount" ? " field-invalid" : ""}`}>
                    <label htmlFor="roundCount">Fight rounds</label>
                    <CustomSelect
                      id="roundCount"
                      value={parsedRounds.roundCount}
                      options={ROUND_COUNT_OPTIONS}
                      placeholder="Select rounds"
                      includeEmptyOption
                      invalid={invalidFieldId === "roundCount"}
                      describedBy={invalidFieldId === "roundCount" ? "roundCount-error" : undefined}
                      onChange={(value) => updateRoundsField("roundCount", value)}
                    />
                    {invalidFieldId === "roundCount" && error ? (
                      <p id="roundCount-error" className="error-text" role="alert">{error}</p>
                    ) : null}
                  </div>
                  <div className={`field${invalidFieldId === "roundDuration" ? " field-invalid" : ""}`}>
                    <label htmlFor="roundDuration">Minutes per fight round</label>
                    <CustomSelect
                      id="roundDuration"
                      value={parsedRounds.roundDuration}
                      options={ROUND_DURATION_OPTIONS}
                      placeholder="Select minutes"
                      includeEmptyOption
                      invalid={invalidFieldId === "roundDuration"}
                      describedBy={invalidFieldId === "roundDuration" ? "roundDuration-error" : undefined}
                      onChange={(value) => updateRoundsField("roundDuration", value)}
                    />
                    {invalidFieldId === "roundDuration" && error ? (
                      <p id="roundDuration-error" className="error-text" role="alert">{error}</p>
                    ) : null}
                  </div>
                  {shouldHideField(daysOutCtx, "weekly_training_frequency") ? (
                    <div className="field field-span-full">
                      <p className="muted" style={{ opacity: 0.5 }}>Training-day count is not used for planning at this stage.</p>
                    </div>
                  ) : (
                    <div
                      className={`field field-span-full${invalidFieldId === "sessionsPerWeek" ? " field-invalid" : ""}`}
                      style={shouldDeEmphasizeField(daysOutCtx, "weekly_training_frequency") ? { opacity: 0.55 } : undefined}
                    >
                      <label htmlFor="sessionsPerWeek">Training days per week</label>
                      <input
                        id="sessionsPerWeek"
                        type="number"
                        min="1"
                        max="7"
                        step="1"
                        inputMode="numeric"
                        disabled={shouldDisableField(daysOutCtx, "weekly_training_frequency")}
                        value={form.weekly_training_frequency ?? ""}
                        onChange={(event) => {
                          const nextValue = numberOrNull(event.target.value);
                          updateField("weekly_training_frequency", nextValue);
                        }}
                        aria-invalid={invalidFieldId === "sessionsPerWeek" ? true : undefined}
                        aria-describedby={invalidFieldId === "sessionsPerWeek" ? "sessionsPerWeek-error" : undefined}
                      />
                      <p className="muted">
                        {getFieldHelperText(daysOutCtx, "weekly_training_frequency") ||
                          "Count each training day once, even if you do multiple sessions that day."}
                      </p>
                      {invalidFieldId === "sessionsPerWeek" && error ? (
                        <p id="sessionsPerWeek-error" className="error-text" role="alert">{error}</p>
                      ) : null}
                    </div>
                  )}
                </div>
              </article>

              <OptionalDetails
                title="Fatigue"
                hint="Defaults to Low. Change it only if you feel tired today."
              >
                {healthConsentGranted ? (
                <div className="field">
                  <label htmlFor="fatigueLevel">Fatigue level</label>
                  <LevelSlider
                    id="fatigueLevel"
                    ariaLabel="Fatigue level"
                    value={(form.fatigue_level ?? "low") as LevelValue}
                    onChange={(value) => updateField("fatigue_level", value)}
                  />
                  <p className="muted">Low = fresh. Moderate = tired. High = very run down.</p>
                </div>
                ) : <p className="muted">{HEALTH_CONSENT_BLOCKED_MESSAGE}</p>}
              </OptionalDetails>
            </div>
          </div>
        ) : null}

        {currentStep === 2 ? (
          <div className="step-layout onboarding-step-layout">
            <div className="step-main athlete-motion-slot athlete-motion-main onboarding-step-main">
              <article className="step-card">
                <div className="form-section-header">
                  <p className="kicker">Schedule</p>
                  <h2 className="form-section-title">{t("trainingAvailability")}</h2>
                </div>
                {shouldHideField(daysOutCtx, "training_availability") ? (
                  <div className="field">
                    <p className="muted" style={{ opacity: 0.5 }}>Training availability is not used for planning at this stage.</p>
                  </div>
                ) : (
                <>
                  <CheckboxGroup
                    id="trainingAvailabilityGroup"
                    label="Training Availability"
                    options={TRAINING_AVAILABILITY_OPTIONS}
                    selectedValues={form.training_availability}
                    onToggle={(value) => toggleFieldValue("training_availability", value)}
                    disableAll={shouldDisableField(daysOutCtx, "training_availability")}
                    invalid={invalidFieldId === "trainingAvailabilityGroup"}
                    describedBy={invalidFieldId === "trainingAvailabilityGroup" ? "trainingAvailabilityGroup-error" : undefined}
                  />
                  {invalidFieldId === "trainingAvailabilityGroup" && error ? (
                    <p id="trainingAvailabilityGroup-error" className="error-text" role="alert">{error}</p>
                  ) : null}
                </>
                )}
                {availabilityConsistency.hardError || availabilityConsistency.softWarning ? (
                  <div
                    id="availabilityConsistencyAlert"
                    className={`support-panel ${availabilityConsistency.hardError ? "support-panel-alert" : ""}${invalidFieldId === "availabilityConsistencyAlert" ? " field-invalid" : ""}`.trim()}
                    tabIndex={invalidFieldId === "availabilityConsistencyAlert" ? -1 : undefined}
                    aria-invalid={invalidFieldId === "availabilityConsistencyAlert" ? true : undefined}
                  >
                    <p className="kicker">Schedule check</p>
                    <p className={availabilityConsistency.hardError ? "error-text" : "muted"}>
                      {availabilityConsistency.hardError ?? availabilityConsistency.softWarning}
                    </p>
                  </div>
                ) : null}
              </article>
              <article className="step-card">
                <div className="form-section-header">
                  <p className="kicker">Combat load</p>
                  <h2 className="form-section-title">{t("combatLoad")}</h2>
                </div>
                <p className="muted">
                  Mark combat days as hard sparring or light/technical. Both count toward weekly sessions.
                </p>
                {shouldHideField(daysOutCtx, "hard_sparring_days") ? (
                  <div className="field">
                    <p className="muted" style={{ opacity: 0.5 }}>Hard sparring day selection is not used for planning at this stage.</p>
                  </div>
                ) : (
                <>
                <CheckboxGroup
                  label="Hard sparring days"
                  options={TRAINING_AVAILABILITY_OPTIONS}
                  selectedValues={form.hard_sparring_days}
                  onToggle={(value) => toggleFieldValue("hard_sparring_days", value)}
                  disableAll={shouldDisableField(daysOutCtx, "hard_sparring_days")}
                  getOptionDisabledReason={(option, checked) =>
                    checked
                      ? null
                      : lockedFightWeekday === option.value
                        ? fightDayLockReason
                        : !form.training_availability.includes(option.value)
                          ? "Add to availability first"
                          : form.support_work_days.includes(option.value)
                            ? "Already tagged as Light Combat"
                            : form.hard_sparring_days.length >= HARD_SPARRING_DAY_CAP
                              ? `Hard sparring cap (${HARD_SPARRING_DAY_CAP}) reached`
                              : null
                  }
                />
                <div className="field">
                  <p className="muted">
                    {getFieldHelperText(daysOutCtx, "hard_sparring_days") ||
                      "Select your usual hard sparring days."}
                  </p>
                </div>
                {hardSparringWarning.message ? (
                  <div
                    id="hardSparringAck"
                    className={`inline-warning-banner ${hardSparringWarningLocked ? "inline-warning-banner-alert" : ""}${invalidFieldId === "hardSparringAck" ? " field-invalid" : ""}`.trim()}
                    tabIndex={invalidFieldId === "hardSparringAck" ? -1 : undefined}
                    aria-invalid={invalidFieldId === "hardSparringAck" ? true : undefined}
                    aria-describedby={invalidFieldId === "hardSparringAck" ? "hardSparringAck-error" : undefined}
                  >
                    <p className="inline-warning-banner-label">High-contact warning</p>
                    <p className={hardSparringWarningLocked ? "error-text" : "muted"}>{hardSparringWarning.message}</p>
                    <label className={`inline-warning-ack ${hardSparringWarningAcknowledged ? "inline-warning-ack-checked" : ""}`.trim()}>
                      <input
                        type="checkbox"
                        checked={hardSparringWarningAcknowledged}
                        onChange={(event) => {
                          setAcknowledgedHardSparringWarningKey(
                            event.target.checked ? hardSparringWarning.acknowledgementContextKey : null,
                          );
                        }}
                      />
                      <span className="inline-warning-ack-copy">I understand this needs deliberate recovery planning.</span>
                    </label>
                    {invalidFieldId === "hardSparringAck" && error ? (
                      <p id="hardSparringAck-error" className="error-text" role="alert">{error}</p>
                    ) : null}
                  </div>
                ) : null}
                </>
                )}
                {shouldHideField(daysOutCtx, "support_work_days") ? (
                  <div className="field">
                    <p className="muted" style={{ opacity: 0.5 }}>Light Combat day selection is not used for planning at this stage.</p>
                  </div>
                ) : (
                <>
                <CheckboxGroup
                  label="Light or technical days"
                  options={TRAINING_AVAILABILITY_OPTIONS}
                  selectedValues={form.support_work_days}
                  onToggle={(value) => toggleFieldValue("support_work_days", value)}
                  disableAll={shouldDisableField(daysOutCtx, "support_work_days")}
                  getOptionDisabledReason={(option, checked) =>
                    checked
                      ? null
                      : lockedFightWeekday === option.value
                        ? fightDayLockReason
                        : !form.training_availability.includes(option.value)
                          ? "Add to availability first"
                          : form.hard_sparring_days.includes(option.value)
                            ? "Already tagged as hard sparring"
                            : null
                  }
                />
                <div className="field">
                  <p className="muted">
                    {getFieldHelperText(daysOutCtx, "support_work_days") ||
                      "Pads, drills, movement or other lower-intensity combat work."
                    }
                  </p>
                </div>
                </>
                )}
                {sparringConsistency.hardError || sparringConsistency.softWarning ? (
                  <div
                    id="sparringConsistencyAlert"
                    className={`support-panel ${sparringConsistency.hardError ? "support-panel-alert" : ""}${invalidFieldId === "sparringConsistencyAlert" ? " field-invalid" : ""}`.trim()}
                    tabIndex={invalidFieldId === "sparringConsistencyAlert" ? -1 : undefined}
                    aria-invalid={invalidFieldId === "sparringConsistencyAlert" ? true : undefined}
                  >
                    <p className="kicker">Sparring check</p>
                    <p className={sparringConsistency.hardError ? "error-text" : "muted"}>
                      {sparringConsistency.hardError ?? sparringConsistency.softWarning}
                    </p>
                  </div>
                ) : null}
                {!noScheduledFight && !form.hard_sparring_days.length && !form.support_work_days.length ? (
                  <div className="field">
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => {
                        setNoScheduledFight(true);
                        setForm((current) => applyNoScheduledFightSnapshot(current, true));
                        setError(null);
                        setInvalidFieldId(null);
                        setValidationFocusRequest(null);
                        setMessage("Open Plan selected. Continue with your available training schedule.");
                      }}
                    >
                      I don&apos;t currently have a scheduled combat session
                    </button>
                  </div>
                ) : null}
              </article>
              <article className="step-card">
                <div className="form-section-header">
                  <p className="kicker">Resources</p>
                  <h2 className="form-section-title">{t("equipment")}</h2>
                </div>
                <EquipmentSelector
                  selectedValues={form.equipment_access}
                  onToggle={(value) => toggleFieldValue("equipment_access", value)}
                />
              </article>
              {shouldHideField(daysOutCtx, "training_preference") ? null : (
              <OptionalDetails
                title="Training preference"
                hint="Tell the planner if you have a specific feel, pace, or format preference for sessions."
              >
                <div className="field" style={shouldDeEmphasizeField(daysOutCtx, "training_preference") ? { opacity: 0.55 } : undefined}>
                  <label htmlFor="trainingPreference">Session preference</label>
                  <textarea
                    id="trainingPreference"
                    disabled={shouldDisableField(daysOutCtx, "training_preference")}
                    value={form.training_preference ?? ""}
                    onChange={(event) => updateField("training_preference", event.target.value)}
                    maxLength={TRAINING_PREFERENCE_MAX}
                    placeholder="Example: shorter hard sessions, less circuit work, more technical warm-ups, avoid long grinders"
                  />
                  <p className="muted">
                    {getFieldHelperText(daysOutCtx, "training_preference") ||
                      "Use this only for session feel, pacing, or format preferences."}
                  </p>
                </div>
              </OptionalDetails>
              )}
            </div>
          </div>
        ) : null}

        {currentStep === 3 ? (
          <div className="step-layout onboarding-step-layout onboarding-step-layout-restrictions">
            <div className="step-main athlete-motion-slot athlete-motion-main onboarding-step-main">
              <article className="step-card">
                <div className="form-section-header">
                  <p className="kicker">Restrictions</p>
                  <h2 className="form-section-title">{t("injuries")}</h2>
                </div>
                {healthConsentGranted ? <SafetyNote showRedFlags>{INJURY_INTAKE_SAFETY}</SafetyNote> : null}
                {!healthConsentGranted ? (
                  <div className="support-panel gi-empty-state compact-gap" aria-disabled="true">
                    <p className="muted">{HEALTH_CONSENT_BLOCKED_MESSAGE}</p>
                  </div>
                ) : noRestrictions ? (
                  // Empty state — a single inline CTA reveals the body map and a
                  // first card, instead of asking the athlete to untick a box first.
                  <div className="support-panel gi-empty-state compact-gap">
                    <p className="kicker">Anything to train around?</p>
                    <p className="muted">Add any injuries, pain, or movement limits the planner should respect. Leave this empty if there&apos;s nothing to work around.</p>
                    <button type="button" className="injury-card-add-btn" onClick={() => handleNoRestrictionsChange(false)}>
                      <span aria-hidden="true">+</span> Add an injury or restriction
                    </button>
                  </div>
                ) : (
                  <>
                    {showClearInjuriesConfirm ? (
                      <div className="gi-clear-confirm-panel" role="alertdialog" aria-live="polite">
                        <p className="gi-clear-confirm-title">Clear injury cards?</p>
                        <p className="muted">This removes current injuries from this intake.</p>
                        <div className="gi-clear-confirm-actions">
                          <button type="button" className="secondary-button" onClick={() => setShowClearInjuriesConfirm(false)}>{t("keepInjuries")}</button>
                          <button type="button" className="danger-button" onClick={handleConfirmClearInjuries}>{t("clearInjuries")}</button>
                        </div>
                      </div>
                    ) : null}
                    {pendingInjuryRemovalIndex !== null ? (
                      <div ref={pendingRemovalRef} className="gi-clear-confirm-panel gi-remove-confirm-panel" role="alertdialog" aria-live="polite">
                        <p className="gi-clear-confirm-title">Remove injury?</p>
                        <p className="muted">Removing this injury deletes its details.</p>
                        <div className="gi-clear-confirm-actions">
                          <button type="button" className="secondary-button" onClick={handleCancelRemovePendingInjury}>{t("keepInjury")}</button>
                          <button type="button" className="danger-button" onClick={handleConfirmRemovePendingInjury}>{t("removeInjury")}</button>
                        </div>
                      </div>
                    ) : null}
                    <div className={`injury-body-map-layout ${isInjuryMapOpen ? "injury-body-map-layout-picking" : "injury-body-map-layout-selected"}`}>
                      {isInjuryMapOpen ? (
                      <div className="injury-body-map-col" ref={injuryMapRef}>
                        <BodyMap
                          side={bodyMapSide}
                          selections={guidedInjuries
                            .filter((injury) => injury.area.trim() || injury.zone)
                            .map((injury) => ({
                              zone: injury.zone || undefined,
                              label: injury.area,
                              severity: normalizeGuidedInjurySeverity(injury.severity) || undefined,
                            }))}
                          onZoneSelect={handleBodyMapZoneSelect}
                          onSideChange={setBodyMapSide}
                        />
                      </div>
                      ) : null}
                      <div className="injury-cards-col">
                        <div className="injury-card-stack">
                          {guidedInjuries.map((injury, index) => {
                            if (activeGuidedInjuryIndex !== index && !hasGuidedInjuryContent(injury)) return null;
                            const cardId = `guidedInjuryCard-${index}`;
                            const isInvalidCard = invalidFieldId === cardId;
                            return (
                              <div
                                key={`guided-injury-${index}`}
                                id={cardId}
                                className={isInvalidCard ? "field-invalid" : undefined}
                                tabIndex={isInvalidCard ? -1 : undefined}
                                aria-invalid={isInvalidCard ? true : undefined}
                                aria-describedby={isInvalidCard ? `${cardId}-error` : undefined}
                              >
                                <GuidedInjuryCard
                                  injury={injury}
                                  index={index}
                                  isActive={activeGuidedInjuryIndex === index}
                                  locationOnly={isInjuryMapOpen && (activeGuidedInjuryIndex === index || !injury.area.trim())}
                                  onManualEntry={() => {
                                    setActiveGuidedInjuryIndex(index);
                                    setIsInjuryMapOpen(false);
                                    setChangingInjuryAreaIndex(null);
                                  }}
                                  onToggleActive={() => {
                                    if (activeGuidedInjuryIndex === index) {
                                      setActiveGuidedInjuryIndex(null);
                                    } else {
                                      handleEditGuidedInjury(index);
                                    }
                                  }}
                                  onUpdate={(key, value) => updateGuidedInjury(index, key, value)}
                                  onChangeArea={() => {
                                    setChangingInjuryAreaIndex(index);
                                    setIsInjuryMapOpen(true);
                                    requestAnimationFrame(() => {
                                      injuryMapRef.current?.scrollIntoView({ behavior: "auto", block: "center" });
                                      injuryMapRef.current?.querySelector<SVGElement>('[role="button"]')?.focus({ preventScroll: true });
                                    });
                                  }}
                                  onRemove={() => { setChangingInjuryAreaIndex(null); handleRequestRemoveGuidedInjury(index); }}
                                />
                                {isInvalidCard && error ? (
                                  <p id={`${cardId}-error`} className="error-text" role="alert">{error}</p>
                                ) : hasGuidedInjuryDescriptorWithoutArea(injury) ? (
                                  <p className="error-text">{GUIDED_INJURY_MISSING_AREA_MESSAGE}</p>
                                ) : null}
                              </div>
                            );
                          })}
                        </div>

                        {!isInjuryMapOpen && activeGuidedInjuryIndex === null ? <div className="injury-card-add-row">
                          <button type="button" className="injury-card-add-btn" onClick={handleAddGuidedInjury}>
                            <span aria-hidden="true">+</span> Add another injury
                          </button>
                        </div> : null}
                      </div>
                    </div>
                    <button type="button" className="gi-notes-toggle gi-no-restrictions-btn" onClick={() => handleNoRestrictionsChange(true)}>
                      No current injuries or restrictions
                    </button>
                  </>
                )}
              </article>
            </div>
          </div>
        ) : null}

        {currentStep === 4 ? (
          <div className="step-layout onboarding-step-layout">
            <div className="step-main athlete-motion-slot athlete-motion-main onboarding-step-main">
              <article
                className={`focus-cap-card ${performanceFocusCapExceeded ? "focus-cap-card-alert" : performanceFocusCapReached ? "focus-cap-card-full" : ""}`.trim()}
                aria-label={performanceFocusCapTitle}
              >
                <span className="focus-cap-badge" aria-hidden="true">
                  {performanceFocusCapBadge}
                </span>
                <div className="focus-cap-copy">
                  <p className="focus-cap-label">Goals and weak areas</p>
                  <p className="focus-cap-hint">{performanceFocusCapHint}</p>
                  {!form.fight_date && !noScheduledFight ? (
                    <button
                      type="button"
                      className="action-link"
                      onClick={() => reportInvalidField({
                        message: "Add your fight date, or tick No scheduled fight yet.",
                        fieldId: "fightDate",
                        step: 1,
                      })}
                    >
                      Add fight date
                    </button>
                  ) : null}
                </div>
              </article>
              {shouldHideField(daysOutCtx, "key_goals") ? (
              <article className="step-card">
                <div className="form-section-header">
                  <p className="kicker">Target outcomes</p>
                  <h2 className="form-section-title">{t("goals")}</h2>
                </div>
                <p className="muted" style={{ opacity: 0.5 }}>Goal selection is not used for planning at this stage.</p>
              </article>
              ) : (
              <article className="step-card" style={shouldDeEmphasizeField(daysOutCtx, "key_goals") ? { opacity: 0.55 } : undefined}>
                <div className="form-section-header">
                  <p className="kicker">Target outcomes</p>
                  <h2 className="form-section-title">{t("goals")}</h2>
                </div>
                <CheckboxGroup
                  id="keyGoalsGroup"
                  label="Key Goals"
                  options={KEY_GOAL_OPTIONS}
                  selectedValues={form.key_goals}
                  onToggle={(value) => toggleFieldValue("key_goals", value)}
                  disableAdditionalSelections={performanceFocusCapReached}
                  capDisabledReason={keyGoalCapDisabledReason}
                  disableAll={shouldDisableField(daysOutCtx, "key_goals")}
                  getOptionDisabledReason={getKeyGoalDisabledReason}
                  invalid={invalidFieldId === "keyGoalsGroup"}
                  describedBy={invalidFieldId === "keyGoalsGroup" ? "keyGoalsGroup-error" : undefined}
                />
                {invalidFieldId === "keyGoalsGroup" && error ? (
                  <p id="keyGoalsGroup-error" className="error-text" role="alert">{error}</p>
                ) : null}
                {getFieldHelperText(daysOutCtx, "key_goals") ? (
                  <p className="muted">{getFieldHelperText(daysOutCtx, "key_goals")}</p>
                ) : null}
                {form.key_goals.length > 1 ? (
                  <div className="field">
                    <label htmlFor="primaryGoal">Primary goal</label>
                    <CustomSelect
                      id="primaryGoal"
                      value={form.primary_goal ?? ""}
                      options={KEY_GOAL_OPTIONS.filter((option) => form.key_goals.includes(option.value))}
                      placeholder="Select primary goal"
                      includeEmptyOption
                      onChange={(value) => updateField("primary_goal", value)}
                    />
                    <p className="muted">Pick which one the plan should be built around.</p>
                  </div>
                ) : null}
              </article>
              )}
              {shouldHideField(daysOutCtx, "weak_areas") ? (
              <article className="step-card">
                <div className="form-section-header">
                  <p className="kicker">Performance gaps</p>
                  <h2 className="form-section-title">{t("weakAreas")}</h2>
                </div>
                <p className="muted" style={{ opacity: 0.5 }}>Weak area selection is not used for planning at this stage.</p>
              </article>
              ) : (
              <article className="step-card" style={shouldDeEmphasizeField(daysOutCtx, "weak_areas") ? { opacity: 0.55 } : undefined}>
                <div className="form-section-header">
                  <p className="kicker">Performance gaps</p>
                  <h2 className="form-section-title">{t("weakAreas")}</h2>
                </div>
                <CheckboxGroup
                  label="Weak Areas"
                  options={WEAK_AREA_OPTIONS}
                  selectedValues={form.weak_areas}
                  onToggle={(value) => toggleFieldValue("weak_areas", value)}
                  disableAdditionalSelections={form.weak_areas.length >= 2 || performanceFocusCapReached}
                  capDisabledReason={weakAreaCapDisabledReason}
                  disableAll={shouldDisableField(daysOutCtx, "weak_areas")}
                  getOptionDisabledReason={getWeakAreaDisabledReason}
                />
                {getFieldHelperText(daysOutCtx, "weak_areas") ? (
                  <p className="muted">{getFieldHelperText(daysOutCtx, "weak_areas")}</p>
                ) : null}
                {form.weak_areas.length > 1 ? (
                  <div className="field">
                    <label htmlFor="primaryWeakArea">Primary weak area</label>
                    <CustomSelect
                      id="primaryWeakArea"
                      value={form.primary_weak_area ?? ""}
                      options={WEAK_AREA_OPTIONS.filter((option) => form.weak_areas.includes(option.value))}
                      placeholder="Select primary weak area"
                      includeEmptyOption
                      onChange={(value) => updateField("primary_weak_area", value)}
                    />
                    <p className="muted">Pick which one the plan must manage first.</p>
                  </div>
                ) : null}
                <p className="muted">Pick up to 2 weak areas.</p>
              </article>
              )}
              {goalWeakAreaOverlaps.length ? (
                <article className="step-card priority-clarification-card">
                  <div className="form-section-header">
                    <p className="kicker">Clarification</p>
                    <h2 className="form-section-title">{t("priorityDetail")} <span className="badge coming-soon-badge">Coming soon</span></h2>
                  </div>
                  <div className="priority-clarification-copy">
                    <p>{overlapClarificationPrompt}</p>
                    <p className="muted">Coming soon: clarifying overlapping goals and weak areas.</p>
                  </div>
                  <fieldset disabled className="priority-clarification-options" role="radiogroup" aria-label="Goal and weak area clarification" aria-disabled="true">
                    {goalWeakAreaOverlaps.map((overlap, overlapIndex) => {
                      const overlapClarificationOptions = getClarificationOptions(overlap.normalizedTag, form.athlete.technical_style, form.athlete.tactical_style);
                      const selectedDetail = form.goal_weakness_collision_details?.[overlapIndex]?.detail ?? "";
                      return (
                        <div key={overlap.tag} className="field">
                          <p><strong>{overlap.label}</strong></p>
                          {overlapClarificationOptions.map((option) => {
                            const checked = selectedDetail === option;
                            return (
                              <label
                                key={`${overlap.tag}-${option}`}
                                className={`priority-clarification-option${checked ? " priority-clarification-option-selected" : ""}`}
                              >
                                <input
                                  type="radio"
                                  name={`goalWeaknessCollisionDetail-${overlap.tag}`}
                                  value={option}
                                  checked={checked}
                                  onChange={() => {
                                    const next = [...(form.goal_weakness_collision_details ?? [])];
                                    const existing = next[overlapIndex] ?? { tag: overlap.tag, label: overlap.label, detail: "" };
                                    next[overlapIndex] = { ...existing, tag: overlap.tag, label: overlap.label, detail: option };
                                    updateField("goal_weakness_collision_details", next);
                                    if (overlapIndex === 0) updateField("goal_weakness_collision_detail", option);
                                  }}
                                />
                                <span>{option}</span>
                              </label>
                            );
                          })}
                        </div>
                      );
                    })}
                  </fieldset>
                </article>
              ) : null}
              <OptionalDetails
                title="Add coach notes"
                hint="Coming soon. Mental or confidence issues, or anything else relevant."
                comingSoon
              >
                <fieldset disabled className="form-grid" aria-disabled="true">
                  <div className="field">
                    <label htmlFor="mindsetChallenges">Mental / confidence issue</label>
                    <textarea
                      id="mindsetChallenges"
                      value={form.mindset_challenges ?? ""}
                      onChange={(event) => updateField("mindset_challenges", event.target.value)}
                      maxLength={MENTAL_BLOCKERS_MAX}
                      placeholder="Optional: anxiety under pressure, low confidence late in camp, trouble switching on"
                    />
                    <p className="muted">Only for a real mental or confidence issue the plan should respect.</p>
                  </div>
                  <div className="field">
                    <label htmlFor="notes">Anything else we should know?</label>
                    <textarea
                      id="notes"
                      value={form.notes ?? ""}
                      onChange={(event) => updateField("notes", event.target.value)}
                      maxLength={PREVIOUS_PLAN_FEEDBACK_MAX}
                      placeholder="Optional: travel, school/work load, sparring schedule, recovery, or anything else relevant"
                    />
                    <p className="muted">Extra coach context that fits nowhere else.</p>
                  </div>
                </fieldset>
              </OptionalDetails>
            </div>
          </div>
        ) : null}

        {currentStep === 5 ? (
          <div className="step-layout onboarding-step-layout">
            <div className="step-main athlete-motion-slot athlete-motion-main onboarding-step-main">
              <article className="step-card">
                <div className="form-section-header">
                  <p className="kicker">Review</p>
                  <h2 className="form-section-title">{t("capturedInput")}</h2>
                </div>
                <div className="review-columns">
                  <div className="review-column">
                    <article className="review-card">
                      <div className="review-card-header">
                        <p className="kicker">Profile</p>
                        <h3 className="review-card-title">{t("athleteProfile")}</h3>
                      </div>
                      <ReviewDetailList items={profileReviewItems} />
                    </article>
                    <article className="review-card">
                      <div className="review-card-header">
                        <p className="kicker">Training</p>
                        <h3 className="review-card-title">{t("availabilityEquipment")}</h3>
                      </div>
                      <ReviewDetailList items={trainingReviewItems} />
                    </article>
                  </div>
                  <div className="review-column">
                    <article className="review-card">
                      <div className="review-card-header">
                        <p className="kicker">Fight context</p>
                        <h3 className="review-card-title">{t("campSetup")}</h3>
                      </div>
                      <ReviewDetailList items={campSetupReviewItems} />
                    </article>
                    <article className="review-card">
                      <div className="review-card-header">
                        <p className="kicker">Performance</p>
                        <h3 className="review-card-title">{t("goalsWeakAreas")}</h3>
                      </div>
                      <ReviewDetailList items={performanceReviewItems} />
                    </article>
                    <article className="review-card">
                      <div className="review-card-header">
                        <p className="kicker">Constraints</p>
                        <h3 className="review-card-title">{t("constraintsRisks")}</h3>
                      </div>
                      <ReviewDetailList items={constraintsReviewItems} />
                    </article>
                  </div>
                </div>
              </article>
            </div>

            <aside className="step-aside athlete-motion-slot athlete-motion-rail onboarding-step-aside">
              <div className="support-panel">
                <p className="kicker">Weight tracker</p>
                <p className="muted">Log weigh-ins and track your trend to fight weight.</p>
                <div className="plan-summary-actions">
                  <Link href="/nutrition" className="ghost-button">
                    Open weight tracker
                  </Link>
                </div>
              </div>
            </aside>
          </div>
        ) : null}

        {message ? <div className="success-banner athlete-motion-slot athlete-motion-status">{message}</div> : null}
        {error ? (
          <div
            id="onboarding-error-banner"
            className="error-banner athlete-motion-slot athlete-motion-status"
            role="alert"
            aria-live="assertive"
          >
            {error}
          </div>
        ) : null}

        <div className="form-actions onboarding-action-bar athlete-motion-slot athlete-motion-rail">
          <div className="onboarding-action-bar-copy">
            <p className="kicker">{actionBarTitle}</p>
            <p className="muted">{actionBarSummary}</p>
          </div>
          <div className="onboarding-action-buttons">
            <button type="button" className="action-link onboarding-action-secondary" onClick={handleSaveDraft} disabled={formActionPending}>
              {isPending ? t("saving") : t("saveDraft")}
            </button>
            {currentStep > 0 ? (
              <button type="button" className="action-link onboarding-action-secondary onboarding-action-back" onClick={handleBack} aria-label={t("back")}>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5m6-6-6 6 6 6" /></svg>
                <span className="onboarding-action-label">{t("back")}</span>
              </button>
            ) : null}
            {currentStep < steps.length - 1 ? (
              <button type="button" className="cta onboarding-action-primary" onClick={handleNext} disabled={formActionPending}>
                {t("next")}
              </button>
            ) : (
              <>
                <button type="button" className="cta onboarding-action-primary" onClick={handleGenerate} disabled={formActionPending}>
                  {t("generate")}
                </button>
              </>
            )}
          </div>
        </div>
      </section>
    </RequireAuth>
  );
}
