"use client";

import "./session-review.css";

import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
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
  const rehabRef = useRef<HTMLDivElement | null>(null);
  const painRef = useRef<HTMLDivElement | null>(null);
  const reasonRef = useRef<HTMLTextAreaElement | null>(null);
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
    const dialog = dialogRef.current;
    const previousFocus = document.activeElement;
    const releaseScroll = lockOverlayScroll();
    dialog?.focus({ preventScroll: true });
    return () => {
      releaseScroll();
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected &&
          (dialog?.contains(document.activeElement) || document.activeElement === document.body)) {
        previousFocus.focus({ preventScroll: true });
      }
    };
  }, []);

  function handleReviewKeys(event: KeyboardEvent<HTMLDivElement>) {
    const dialog = event.currentTarget;
    if (event.defaultPrevented || !dialog.contains(event.target as Node)) return;
    if (event.key === "Escape" && !busy) {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(dialog.querySelectorAll<HTMLElement>(
      'button, input, textarea, select, summary, a[href], [tabindex="0"]',
    )).filter((control) => control.tabIndex >= 0 && !control.matches(":disabled") &&
      !control.closest("[inert]") && control.getClientRects().length > 0);
    const first = controls[0];
    const last = controls.at(-1);
    if (document.activeElement === dialog || !first ||
        document.activeElement === (event.shiftKey ? first : last)) {
      event.preventDefault();
      (event.shiftKey ? last : first)?.focus();
    }
  }

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
      rehabRef.current?.querySelector<HTMLElement>("button:not(:disabled), input:not(:disabled)")?.focus();
      return;
    }
    if (needsReviewFields && sessionRpe === null) {
      setError("Add session effort before saving.");
      document.getElementById(`${fieldId}-session-rpe`)?.focus();
      return;
    }
    if (needsReviewFields && canCollectPain && painAfter === null) {
      setError("Add pain after before saving.");
      painRef.current?.querySelector<HTMLElement>('[role="radiogroup"]')?.focus();
      return;
    }
    if (outcome.status !== "done" && !reason.trim()) {
      setError(outcome.status === "skipped" ? "Say briefly why you skipped the session." : "Say briefly what changed.");
      reasonRef.current?.focus();
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
        onKeyDown={handleReviewKeys}
      >
        <div className="today-review-content">
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
                  {tickingRest ? "Marking…" : "Mark remaining exercises done"}
                </button>
              ) : null}
            </div>
          ) : null}

          <div className="today-review-blocks">{blocks}</div>

          <div className="today-review-fields today-completion-form">
            {rehabChoice ? <div ref={rehabRef}>{rehabChoice}</div> : null}
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
                    onChange={(value) => {
                      setSessionRpe(value);
                      setError(null);
                    }}
                  />
                </div>
                {canCollectPain ? (
                  <div className="field" ref={painRef}>
                    <span>Pain after</span>
                    <FaceScale value={painAfter} onChange={(value) => {
                      setPainAfter(value);
                      setError(null);
                    }} />
                  </div>
                ) : null}
              </div>
            ) : null}
            {outcome.status !== "done" ? (
              <label className="field" htmlFor={`${fieldId}-reason`}>
                <span>{outcome.status === "skipped" ? "Why it was skipped" : "What changed"}</span>
                <textarea
                  id={`${fieldId}-reason`}
                  ref={reasonRef}
                  value={reason}
                  maxLength={2000}
                  rows={2}
                  onChange={(event) => {
                    setReasonEdit(event.target.value);
                    setError(null);
                  }}
                />
              </label>
            ) : null}
            <details className="today-review-notes">
              <summary>Add notes</summary>
              <label className="field" htmlFor={`${fieldId}-notes`}>
                <span className="sr-only">Session Notes (optional)</span>
                <textarea
                  id={`${fieldId}-notes`}
                  value={notes}
                  maxLength={2000}
                  rows={2}
                  onChange={(event) => setNotes(event.target.value)}
                />
              </label>
            </details>
          </div>
        </div>

        <footer className="today-review-save">
          <p className="today-review-outcome" aria-live="polite">
            Save as <strong>{outcome.status === "done" ? "completed" : outcome.status}</strong>
          </p>
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
        </footer>
      </div>
    </div>
  );

  return typeof document === "undefined" ? sheet : createPortal(sheet, document.body);
}
