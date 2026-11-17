"use client";

import { type FormEvent, useId, useState } from "react";

import { useAppSession } from "@/components/auth-provider";
import { EffortSlider, FaceScale } from "@/components/rating-controls";
import { hasHealthDataConsent } from "@/lib/compliance";
import { sessionStatusLabel } from "@/lib/history";
import {
  completionRequiresModificationReason,
  completionRequiresReviewFields,
  getCompletionLabel,
  getCompletionReasonError,
  getCompletionReasonLabel,
} from "@/lib/today";
import type { TodayCompletionStatus } from "@/lib/types";

export type CompletionIntent = Extract<
  TodayCompletionStatus,
  "done" | "modified" | "skipped"
> | null;

export type CompletionFormPayload = {
  sessionRpe: number | null;
  painAfter: number | null;
  modificationReason: string;
  notes: string;
};

const PICKER_INTENTS: Exclude<CompletionIntent, null>[] = ["done", "modified", "skipped"];

/**
 * Shared done/modified/skipped logging form: RPE + pain-after for sessions that
 * were trained, and a required reason for modified and skipped sessions.
 *
 * Two modes:
 * - Today flow: the caller owns the intent (Mark done / Mark modified / Mark
 *   skipped buttons) and passes it in; the form renders nothing until set.
 * - Retro-log flow (`showStatusPicker`): a past session has no start/resume
 *   lifecycle, so the terminal status is picked inside the form instead.
 */
export function SessionCompletionForm({
  intent,
  isSubmitting,
  onCancel,
  onSubmit,
  showStatusPicker = false,
  initialNotes = "",
  submissionBlockedReason,
}: {
  intent: CompletionIntent;
  isSubmitting: boolean;
  onCancel: () => void;
  onSubmit: (status: Exclude<CompletionIntent, null>, payload: CompletionFormPayload) => Promise<void>;
  showStatusPicker?: boolean;
  /** Pre-filled, editable notes (e.g. what the session timer recorded). */
  initialNotes?: string;
  submissionBlockedReason?: string;
}) {
  const { me } = useAppSession();
  const canCollectPain = hasHealthDataConsent(me);
  const fieldId = useId();
  const [pickedIntent, setPickedIntent] = useState<CompletionIntent>(intent);
  const [sessionRpe, setSessionRpe] = useState<number | null>(null);
  const [painAfter, setPainAfter] = useState<number | null>(null);
  const [modificationReason, setModificationReason] = useState("");
  const [notes, setNotes] = useState(initialNotes);
  const [error, setError] = useState<string | null>(null);

  const activeIntent = showStatusPicker ? pickedIntent : intent;

  if (!showStatusPicker && !intent) {
    return null;
  }

  const needsReviewFields = activeIntent ? completionRequiresReviewFields(activeIntent) : false;
  const needsReason = activeIntent ? completionRequiresModificationReason(activeIntent) : false;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (submissionBlockedReason) {
      setError(submissionBlockedReason);
      return;
    }
    if (!activeIntent) {
      setError("Choose how the session went first.");
      return;
    }
    if (needsReason && !modificationReason.trim()) {
      setError(getCompletionReasonError(activeIntent));
      return;
    }
    if (needsReviewFields && (sessionRpe === null || (canCollectPain && painAfter === null))) {
      setError(canCollectPain ? "Add session RPE and pain-after before saving." : "Add session RPE before saving.");
      return;
    }
    await onSubmit(activeIntent, {
      sessionRpe,
      painAfter,
      modificationReason: modificationReason.trim(),
      notes: notes.trim(),
    });
  }

  return (
    <form className="today-completion-form" onSubmit={handleSubmit}>
      {showStatusPicker ? (
        <div className="field">
          <span>How did this session go?</span>
          <div className="today-action-row" role="group" aria-label="Session outcome">
            {PICKER_INTENTS.map((option) => (
              <button
                key={option}
                type="button"
                className={option === pickedIntent ? "secondary-button" : "ghost-button"}
                aria-pressed={option === pickedIntent}
                onClick={() => setPickedIntent(option)}
                disabled={isSubmitting}
              >
                {sessionStatusLabel(option)}
              </button>
            ))}
          </div>
        </div>
      ) : null}
      {needsReviewFields ? (
        <div className="today-completion-fields">
          <div className="field">
            <span>Session effort</span>
            <EffortSlider
              id={`${fieldId}-session-rpe`}
              ariaLabel="Session effort"
              value={sessionRpe}
              onChange={setSessionRpe}
            />
          </div>
          {canCollectPain ? <div className="field">
            <span>Pain after</span>
            <FaceScale value={painAfter} onChange={setPainAfter} />
          </div> : null}
        </div>
      ) : null}
      {needsReason && activeIntent ? (
        <label className="field" htmlFor={`${fieldId}-reason`}>
          <span>{getCompletionReasonLabel(activeIntent)}</span>
          <textarea
            id={`${fieldId}-reason`}
            value={modificationReason}
            maxLength={2000}
            rows={2}
            onChange={(event) => setModificationReason(event.target.value)}
          />
        </label>
      ) : null}
      <label className="field" htmlFor={`${fieldId}-notes`}>
        <span>Session Notes (optional)</span>
        <textarea
          id={`${fieldId}-notes`}
          value={notes}
          maxLength={2000}
          rows={2}
          onChange={(event) => setNotes(event.target.value)}
        />
      </label>
      {error ? <p className="today-inline-error" role="alert">{error}</p> : null}
      <div className="today-action-row">
        <button type="submit" className="cta" disabled={isSubmitting || Boolean(submissionBlockedReason)}>
          {isSubmitting
            ? "Saving..."
            : activeIntent
              ? `Save ${getCompletionLabel(activeIntent).toLowerCase()}`
              : "Save"}
        </button>
        <button type="button" className="ghost-button" onClick={onCancel} disabled={isSubmitting}>
          Cancel
        </button>
      </div>
    </form>
  );
}
