"use client";

import { type FormEvent, useRef, useState } from "react";

import { SafetyNote } from "@/components/safety-note";
import { SegmentGroup } from "@/components/today/segment-group";
import { useToast } from "@/components/toast-provider";
import { offerPushNotifications } from "@/lib/push-prompt";
import { submitTodayCheckin, trackProductEvent } from "@/lib/api";
import { TODAY_RED_FLAG_SAFETY } from "@/lib/safety-copy";
import { buildTodayCheckinPayload, getAthleteWarnings, getRecommendationCopy } from "@/lib/today";
import type {
  TodayActivePlan,
  TodayCheckinBody,
  TodayCheckinPain,
  TodayCheckinSleep,
} from "@/lib/types";
import { XP_TOAST_KEYS } from "@/lib/xp-events";

const SLEEP_OPTIONS: Array<{ value: TodayCheckinSleep; label: string }> = [
  { value: "poor", label: "Poor" },
  { value: "okay", label: "Okay" },
  { value: "good", label: "Good" },
];

const BODY_OPTIONS: Array<{ value: TodayCheckinBody; label: string }> = [
  { value: "flat", label: "Flat" },
  { value: "normal", label: "Normal" },
  { value: "sharp", label: "Sharp" },
];

const PAIN_OPTIONS: Array<{ value: TodayCheckinPain; label: string }> = [
  { value: "none", label: "None" },
  { value: "manageable", label: "Manageable" },
  { value: "high", label: "High" },
];

const SAFETY_FLAGS: Array<{ key: keyof TodaySafetyFlags; label: string }> = [
  { key: "sharp_pain", label: "Sharp pain" },
  { key: "instability", label: "Instability" },
  { key: "swelling", label: "Swelling" },
  { key: "neurological_symptoms", label: "Neurological symptoms" },
  { key: "illness_symptoms", label: "Illness symptoms" },
  { key: "cannot_warm_into_movement", label: "Cannot warm into movement" },
  { key: "worse_next_day_pain", label: "Worse next-day pain" },
];

export type TodaySafetyFlags = {
  sharp_pain: boolean;
  instability: boolean;
  swelling: boolean;
  neurological_symptoms: boolean;
  illness_symptoms: boolean;
  cannot_warm_into_movement: boolean;
  worse_next_day_pain: boolean;
};

/**
 * The fast daily check-in. Submits the categorical readiness inputs and safety
 * flags; the backend computes and persists the recommendation (the client
 * never supplies one). Without a plan it is the same check-in, sent plan-free.
 */
export function TodayReadinessForm({
  plan,
  token,
  warnings,
  onRefresh,
}: {
  /** Omitted when the athlete has no active programme. */
  plan?: TodayActivePlan;
  token: string;
  warnings?: string[];
  onRefresh: () => Promise<void>;
}) {
  const { showToast } = useToast();
  const [sleep, setSleep] = useState<TodayCheckinSleep>("good");
  const [body, setBody] = useState<TodayCheckinBody>("normal");
  const [pain, setPain] = useState<TodayCheckinPain>("none");
  const [safetyFlags, setSafetyFlags] = useState<TodaySafetyFlags>({
    sharp_pain: false,
    instability: false,
    swelling: false,
    neurological_symptoms: false,
    illness_symptoms: false,
    cannot_warm_into_movement: false,
    worse_next_day_pain: false,
  });
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [inlineWarning] = getAthleteWarnings(warnings);
  // Optional, consent-gated: the first answer marks a started check-in so an
  // unsaved one is measurable. No answer values are sent.
  const started = useRef(false);
  const markStarted = () => {
    if (started.current) return;
    started.current = true;
    void trackProductEvent(token, "checkin_started", "recovery");
  };

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if ((plan && !plan.id) || isSubmitting) {
      return;
    }
    markStarted();
    setIsSubmitting(true);
    try {
      const response = await submitTodayCheckin(
        token,
        buildTodayCheckinPayload({
          planId: plan?.id,
          phase: plan?.phase,
          sleep,
          body,
          pain,
          safetyFlags,
        }),
      );
      // "Train as planned" names a programme, so a plan-free check-in just confirms.
      showToast(plan ? `Recommendation: ${getRecommendationCopy(response.recommendation_state).label}.` : "Check-in saved.", {
        tone: response.recommendation_state === "pull_back" ? "info" : "success",
        mergeKeys: XP_TOAST_KEYS.checkin,
      });
      // A saved check-in is a value moment too: tracking-first athletes may
      // never save a workout, and would otherwise never be offered reminders.
      offerPushNotifications(token);
      const [responseWarning] = getAthleteWarnings(response.warnings);
      if (responseWarning) {
        showToast(responseWarning, { tone: "info" });
      }
      await onRefresh();
    } catch (error) {
      showToast(error instanceof Error ? error.message : "Check-in failed.", { tone: "error" });
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <section id="today-checkin" className="today-card today-checkin-card" aria-labelledby="today-checkin-heading">
      <div className="today-card-head">
        <div>
          <p className="kicker">Fast check-in</p>
          <h2 id="today-checkin-heading">Set today&apos;s recommendation</h2>
        </div>
      </div>
      <form className="today-checkin-form" onSubmit={handleSubmit}>
        <SegmentGroup label="Sleep" value={sleep} options={SLEEP_OPTIONS} onChange={(value) => { markStarted(); setSleep(value); }} />
        <SegmentGroup label="Body" value={body} options={BODY_OPTIONS} onChange={(value) => { markStarted(); setBody(value); }} />
        <SegmentGroup label="Pain" value={pain} options={PAIN_OPTIONS} onChange={(value) => { markStarted(); setPain(value); }} />
        {/* Injury and last-session answers come from Today's injuries and logged sessions (server-derived). */}
        {inlineWarning ? (
          <p className="today-inline-warning" role="status">{inlineWarning}</p>
        ) : null}

        <details className="today-red-flags">
          <summary>Any red flags?</summary>
          <SafetyNote tone="warning">{TODAY_RED_FLAG_SAFETY}</SafetyNote>
          <div className="today-flag-grid">
            {SAFETY_FLAGS.map((flag) => (
              <label key={flag.key} className="today-flag-option">
                <input
                  type="checkbox"
                  checked={safetyFlags[flag.key]}
                  onChange={(event) => {
                    markStarted();
                    setSafetyFlags((current) => ({
                      ...current,
                      [flag.key]: event.target.checked,
                    }));
                  }}
                />
                <span>{flag.label}</span>
              </label>
            ))}
          </div>
        </details>

        <button type="submit" className="cta today-primary-action" disabled={isSubmitting}>
          {isSubmitting ? "Submitting..." : "Submit check-in"}
        </button>
      </form>
    </section>
  );
}
