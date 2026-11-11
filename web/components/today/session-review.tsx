"use client";

import "./session-review.css";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";

import { OnceHint, useSeenOnce } from "@/components/once-hint";
import type { ExerciseLogging } from "@/components/exercise-log";
import { EffortSlider, FaceScale } from "@/components/rating-controls";
import {
  deriveSessionOutcome,
  logFieldsForBlock,
  plannedSessionRpe,
  type SessionOutcome,
} from "@/lib/exercise-log";
import { cleanText } from "@/lib/structured-plan";
import { completionRequiresReviewFields } from "@/lib/today";
import type { StructuredSession } from "@/lib/types";

export type SessionReviewDetails = {
  sessionRpe: number | null;
  painAfter: number | null;
  modificationReason: string;
  notes: string;
};

/**
 * The one place a session is logged. It is the session's own Today card (the
 * same exercise rows, already ticked from what the timer counted), plus the
 * effort question. The session result is never asked for: it is read off the
 * exercise logs, so each exercise is logged once and the two can never disagree.
 */
export function SessionReview({
  title,
  sessions,
  logging,
  blocks,
  hint,
  extraAction,
  canCollectPain,
  rehabChoice,
  doneBlockedReason,
  isSubmitting,
  onClose,
  onSave,
}: {
  title: string;
  /** The day's sessions, as the card shows them (the day is logged as one). */
  sessions: readonly StructuredSession[];
  logging: ExerciseLogging | null;
  /** The session's exercise rows: the Today card's own blocks. */
  blocks: ReactNode;
  /** One line under the title: where the ticks came from. */
  hint: string;
  /** A shortcut beside the tick-the-rest chip (playing a skipped visualisation). */
  extraAction?: ReactNode;
  canCollectPain: boolean;
  /** The rehab "how much did you do" choice, when the session carries rehab. */
  rehabChoice?: ReactNode;
  /** Why a session that reads as done cannot be saved yet (the rehab choice). */
  doneBlockedReason?: string;
  isSubmitting: boolean;
  onClose: () => void;
  onSave: (status: SessionOutcome["status"], details: SessionReviewDetails) => Promise<boolean>;
}) {
  const [reviewHint, dismissReviewHint] = useSeenOnce("discovery-session-review-seen");
  const fieldId = useId();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  // Starts at the planned effort: a session that went to plan is a tap to confirm.
  const [plannedRpe] = useState(() => plannedSessionRpe(sessions));
  const [sessionRpe, setSessionRpe] = useState<number | null>(plannedRpe);
  const [painAfter, setPainAfter] = useState<number | null>(null);
  // Null until the athlete edits it: until then it follows the exercise logs.
  const [reasonEdit, setReasonEdit] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [tickingRest, setTickingRest] = useState(false);
  const [saving, setSaving] = useState(false);
  const writeInFlight = useRef(false);
  const busy = isSubmitting || tickingRest || saving;

  const logs = logging?.logs ?? {};
  const outcome = deriveSessionOutcome(sessions, logs);
  const canRateLifts = sessions.some(session => session.blocks?.some(block =>
    logFieldsForBlock(block).some(field => field.key === "reps" || field.key === "load")));
  const needsReviewFields = completionRequiresReviewFields(outcome.status);
  const reason = reasonEdit ?? outcome.reason;
  const unloggedCount = outcome.unlogged.length;
  const blockedReason = outcome.status === "done" ? doneBlockedReason : undefined;

  useEffect(() => {
    const releaseScroll = lockOverlayScroll();
    dialogRef.current?.focus({ preventScroll: true });
    return () => {
      releaseScroll();
    };
  }, []);

  async function tickRest() {
    if (!logging?.saveMany || unloggedCount === 0 || writeInFlight.current || isSubmitting) return;
    writeInFlight.current = true;
    setTickingRest(true);
    setError(null);
    try {
      await logging.saveMany(
        outcome.unlogged.map((block) => ({
          block_id: cleanText(block.block_id) ?? "",
          status: "as_prescribed" as const,
        })),
        { keepExisting: true },
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save. Try again.");
    } finally {
      writeInFlight.current = false;
      setTickingRest(false);
    }
  }

  async function save() {
    if (writeInFlight.current || isSubmitting) return;
    setError(null);
    if (blockedReason) {
      setError(blockedReason);
      return;
    }
    if (needsReviewFields && (sessionRpe === null || (canCollectPain && painAfter === null))) {
      setError(canCollectPain ? "Add session effort and pain-after before saving." : "Add session effort before saving.");
      return;
    }
    if (outcome.status !== "done" && !reason.trim()) {
      setError("Say briefly what changed.");
      return;
    }
    writeInFlight.current = true;
    setSaving(true);
    try {
      // Anything still unticked was not done: it is logged as skipped, as the
      // line above the button says, so the exercise record is complete.
      if (unloggedCount > 0 && logging?.saveMany) {
        try {
          await logging.saveMany(
            outcome.unlogged.map((block) => ({ block_id: cleanText(block.block_id) ?? "", status: "skipped" })),
          );
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : "Could not save. Try again.");
          return;
        }
      }
      const saved = await onSave(outcome.status, {
        sessionRpe: needsReviewFields ? sessionRpe : null,
        painAfter: needsReviewFields ? painAfter : null,
        modificationReason: outcome.status === "done" ? "" : reason.trim().slice(0, 2000),
        notes: notes.trim(),
      });
      if (saved) dismissReviewHint();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save. Try again.");
    } finally {
      writeInFlight.current = false;
      setSaving(false);
    }
  }

  const sheet = (
    <div className="today-review-root" role="dialog" aria-modal="true" aria-labelledby={`${fieldId}-title`}>
      <div
        ref={dialogRef}
        className="today-review-sheet"
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Escape" && !busy) onClose();
        }}
      >
        <header className="today-review-head">
          <div>
            <p className="kicker">Session review</p>
            <h2 id={`${fieldId}-title`}>{title}</h2>
            <p className="today-review-count" aria-live="polite">
              {outcome.total === 0
                ? "Nothing to tick. Add how it felt and save."
                : `${outcome.logged} of ${outcome.total} exercises logged`}
            </p>
          </div>
          <button
            type="button"
            className="today-review-close"
            onClick={onClose}
            disabled={busy}
            aria-label="Back to Today"
          >
            ×
          </button>
        </header>

        {reviewHint && logging && outcome.total > 0 ? (
          <OnceHint className="today-review-discovery" onDismiss={dismissReviewHint}
            label={canRateLifts
              ? "Tap an exercise to edit or rate it. Your results guide next time."
              : "Tap an exercise to check or change your results."} />
        ) : <p className="today-review-hint">{hint}</p>}
        {extraAction || (unloggedCount > 0 && logging?.saveMany) ? (
          <div className="today-review-chips">
            {extraAction}
            {unloggedCount > 0 && logging?.saveMany ? (
              <button
                type="button"
                className="today-review-chip"
                onClick={() => void tickRest()}
                disabled={busy}
              >
                {tickingRest ? "Ticking…" : `Tick the other ${unloggedCount} as done`}
              </button>
            ) : null}
          </div>
        ) : null}

        <div className="today-review-blocks">{blocks}</div>

        <div className="today-review-footer today-completion-form">
          {rehabChoice}
          {needsReviewFields ? (
            <div className="today-completion-fields">
              <div className="field">
                <span>How hard was the whole session?</span>
                {plannedRpe !== null ? (
                  <p className="today-review-planned">
                    Set to the effort your plan asked for. Move it if it felt different.
                  </p>
                ) : null}
                <EffortSlider
                  id={`${fieldId}-session-rpe`}
                  ariaLabel="Session effort"
                  value={sessionRpe}
                  onChange={setSessionRpe}
                />
              </div>
              {canCollectPain ? (
                <div className="field">
                  <span>Pain after</span>
                  <FaceScale value={painAfter} onChange={setPainAfter} />
                </div>
              ) : null}
            </div>
          ) : null}
          {outcome.status !== "done" ? (
            <label className="field" htmlFor={`${fieldId}-reason`}>
              <span>{outcome.status === "skipped" ? "Why it was skipped" : "What changed"}</span>
              <textarea
                id={`${fieldId}-reason`}
                value={reason}
                maxLength={2000}
                rows={2}
                onChange={(event) => setReasonEdit(event.target.value)}
              />
            </label>
          ) : null}
          <label className="field" htmlFor={`${fieldId}-notes`}>
            <span>Notes (optional)</span>
            <textarea
              id={`${fieldId}-notes`}
              value={notes}
              maxLength={2000}
              rows={2}
              onChange={(event) => setNotes(event.target.value)}
            />
          </label>
          {unloggedCount > 0 ? (
            <p className="today-review-unlogged">
              {unloggedCount === 1
                ? "1 exercise not ticked will be saved as skipped."
                : `${unloggedCount} exercises not ticked will be saved as skipped.`}
            </p>
          ) : null}
          {error ? (
            <p className="today-inline-error" role="alert">
              {error}
            </p>
          ) : null}
          <button
            type="button"
            className="cta"
            onClick={() => void save()}
            disabled={busy}
          >
            {isSubmitting || saving ? "Saving..." : outcome.status === "skipped" ? "Save as skipped" : "Save session"}
          </button>
        </div>
      </div>
    </div>
  );

  return typeof document === "undefined" ? sheet : createPortal(sheet, document.body);
}
