"use client";

import { useEffect, useId, useRef, useState } from "react";

import sheet from "@/components/log-training.module.css";

import { useAppSession } from "@/components/auth-provider";
import { useToast } from "@/components/toast-provider";
import { submitSparringLog, trackProductEvent } from "@/lib/api";
import { formatAppDate } from "@/lib/date-format";
import { hasHealthDataConsent } from "@/lib/compliance";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import type {
  SparringHeadContact,
  SparringIntensity,
  SparringLogRecord,
  SparringLogSource,
  SparringPlannedIntensity,
} from "@/lib/types";
import { XP_TOAST_KEYS } from "@/lib/xp-events";
import { announceTrainingFocusChanged } from "@/lib/training-focus";

import styles from "./sparring-log-prompt.module.css";
import { useSheetExit } from "@/lib/use-sheet-exit";

/** What the finished timer, or a manual sparring log, knows about the sparring. */
export type SparringDraft = {
  source: SparringLogSource;
  planId: string | null;
  sessionId: string | null;
  plannedIntensity: SparringPlannedIntensity | null;
  title: string;
  /** Counted by the timer. A manual log has no count, so the athlete sets it. */
  rounds: number | null;
  roundSeconds: number | null;
  /** The manual training record this report details. */
  externalSessionId?: string | null;
  /** A starting answer from the manual log's effort, never sent as planned. */
  suggestedIntensity?: SparringIntensity;
  /** A saved report being reopened to correct it. */
  existing?: SparringLogRecord;
};

/** Shown the moment "Yes" is picked, before saving; the server returns the same. */
export const ROCKED_SAFETY_COPY =
  "No more contact today. If you get a headache, dizziness, nausea, confusion, memory problems " +
  "or blurred vision, stop training and get checked by a medical professional. Tell your coach.";

const INTENSITY_LABELS: Record<SparringIntensity, string> = {
  light: "Light",
  medium: "Medium",
  hard: "Hard",
};

const HEAD_CONTACT_LABELS: Record<SparringHeadContact, string> = {
  none: "None",
  light: "Light",
  heavy: "A lot",
};

const HEAD_CONTACT_ORDER: readonly SparringHeadContact[] = ["none", "light", "heavy"];

/** Matches the API's sparring focus cap. */
const FOCUS_MAX_CHARS = 140;

/** Shown when a reopened report holds head-impact evidence a correction cannot lower. */
export const PROTECTED_EVIDENCE_COPY = "Head-impact reports stay on your record for safety. You can add to them, not reduce them.";

/** The plan's intent as the starting answer; the athlete corrects it if needed. */
export function defaultIntensity(planned: SparringPlannedIntensity | null): SparringIntensity | undefined {
  if (planned === "hard") return "hard";
  if (planned === "light" || planned === "technical") return "light";
  if (planned === "contact") return "medium";
  return undefined;
}

function Choice<Value extends string>({
  legend,
  options,
  labels,
  selected,
  onSelect,
  locked,
}: Readonly<{
  legend: string;
  options: readonly Value[];
  labels: Record<Value, string>;
  selected: Value | undefined;
  onSelect: (value: Value) => void;
  /** Answers a correction may not choose. */
  locked?: (value: Value) => boolean;
}>) {
  return (
    <fieldset className={sheet.fieldset}>
      <legend>{legend}</legend>
      <div className={options.length === 2 ? styles.twoUp : sheet.effortGrid}>
        {options.map((value) => (
          <button key={value} type="button" aria-pressed={selected === value} disabled={locked?.(value)} onClick={() => onSelect(value)}>
            {labels[value]}
          </button>
        ))}
      </div>
    </fieldset>
  );
}

/**
 * The 15-second post-sparring entry: how hard it really was, rounds (from the
 * timer), head contact, and whether the athlete got rocked or dropped. A "yes"
 * shows the safety message straight away and, once saved, opens an admin
 * review server-side. Health data, so it only appears with health consent.
 *
 * It opens as the same bottom sheet as Log training, so it never lengthens
 * Today itself.
 */
export function SparringLogPrompt({
  token,
  draft,
  onDismiss,
  onSaved,
}: {
  token: string;
  draft: SparringDraft;
  onDismiss: () => void;
  /** After a confirmed save, so Today can refresh its safety guidance. */
  onSaved?: (log: SparringLogRecord) => void;
}) {
  // Slides away before the parent removes it.
  const { closing, close: dismiss } = useSheetExit(onDismiss);
  const { me } = useAppSession();
  const { showToast } = useToast();
  const notesId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  // Kept across retries of the same answers; any change makes it a new report,
  // so a corrected answer (above all a "Yes, rocked") is never swallowed by
  // an earlier request that landed without its response.
  const requestId = useRef<string | null>(null);
  // Orders this report's sets of answers on the server.
  // A reopened report continues from its latest applied correction.
  const existing = draft.existing;
  const revision = useRef(existing?.detail_revision ?? 0);
  const [intensity, setIntensity] = useState<SparringIntensity | undefined>(
    existing?.intensity ?? defaultIntensity(draft.plannedIntensity) ?? draft.suggestedIntensity,
  );
  const [rounds, setRounds] = useState<number | null>(
    existing ? existing.rounds_completed : draft.rounds === null ? null : Math.min(30, Math.max(0, draft.rounds)),
  );
  const [headContact, setHeadContact] = useState<SparringHeadContact | undefined>(existing?.head_contact);
  const [rocked, setRocked] = useState<"no" | "yes">(existing?.rocked ? "yes" : "no");
  const [notes, setNotes] = useState(existing?.notes ?? "");
  const [focus, setFocus] = useState(existing?.focus ?? "");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedNotice, setSavedNotice] = useState<string | null>(null);
  const [notesOpen, setNotesOpen] = useState(Boolean(existing?.notes));
  // The server keeps recorded head-impact evidence; the form says so up front.
  const contactFloor = existing ? HEAD_CONTACT_ORDER.indexOf(existing.head_contact) : -1;
  const protectedEvidence = Boolean(existing && (existing.rocked || contactFloor > 0));
  const consented = hasHealthDataConsent(me);
  // Optional, consent-gated: a shown new report marks a started log so an
  // unsaved one is measurable. Corrections of a saved report are not starts.
  const isNew = !existing;
  useEffect(() => {
    if (consented && isNew) void trackProductEvent(token, "sparring_log_started", "sparring");
  }, [consented, isNew, token]);

  useEffect(() => {
    const element = dialog.current;
    if (!consented || !element) return;
    const previous = document.activeElement as HTMLElement | null;
    const release = lockOverlayScroll();
    if (!element.open) element.showModal?.();
    const fit = () => {
      const viewport = window.visualViewport;
      element.style.setProperty("--sheet-height", `${viewport?.height ?? window.innerHeight}px`);
      element.style.setProperty("--sheet-bottom", `${viewport ? Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop) : 0}px`);
    };
    fit();
    window.visualViewport?.addEventListener("resize", fit);
    window.visualViewport?.addEventListener("scroll", fit);
    return () => {
      window.visualViewport?.removeEventListener("resize", fit);
      window.visualViewport?.removeEventListener("scroll", fit);
      release();
      previous?.focus();
    };
  }, [consented]);

  const answer = <Value,>(set: (value: Value) => void) => (value: Value) => {
    requestId.current = null;
    set(value);
  };

  if (!consented) {
    return null;
  }

  async function save() {
    if (!intensity || !headContact) {
      setError("Pick how hard it was and how much head contact you took.");
      return;
    }
    if (rounds === null) {
      setError("Add how many rounds you sparred.");
      return;
    }
    if (!requestId.current) {
      requestId.current = crypto.randomUUID();
      revision.current += 1;
    }
    setError(null);
    setIsSubmitting(true);
    try {
      const result = await submitSparringLog(token, {
        plan_id: draft.planId,
        session_id: draft.sessionId,
        source: draft.source,
        external_session_id: draft.externalSessionId ?? null,
        client_request_id: requestId.current,
        detail_revision: revision.current,
        planned_intensity: draft.plannedIntensity,
        intensity,
        rounds_completed: rounds,
        // A correction keeps the saved round length; only the timer measures it.
        round_seconds: draft.roundSeconds ?? existing?.round_seconds ?? null,
        head_contact: headContact,
        rocked: rocked === "yes",
        notes: notes.trim(),
        focus: focus.trim(),
      });
      // A rocked report only counts once its review is queued. The server
      // writes both atomically; this guards against any response without it.
      if (rocked === "yes" && !result.review_created) {
        setError("Your report wasn't sent. Please try again.");
        return;
      }
      onSaved?.(result.log);
      announceTrainingFocusChanged();
      // The focus is written beside the log; a retry reuses the request and writes it again.
      if ((result.log.focus ?? "") !== focus.trim()) {
        setError("Sparring saved, but your focus wasn't. Tap Save to try again.");
        return;
      }
      // A reopened report that was already rocked was acknowledged when first
      // saved; repeating "no more contact today" days later would mislead.
      if (result.safety_notice && !existing?.rocked) {
        setSavedNotice(result.safety_notice);
      } else {
        showToast(existing ? "Sparring report updated." : "Sparring logged.", { tone: "success", mergeKeys: XP_TOAST_KEYS.session });
        dismiss();
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save your sparring log.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <dialog
      ref={dialog}
      className={sheet.dialog}
      data-closing={closing || undefined}
      aria-label={savedNotice ? "Sparring logged" : existing ? "Update sparring report" : "Log your sparring"}
      onCancel={(event) => {
        event.preventDefault();
        if (!isSubmitting) dismiss();
      }}
    >
      <header className={sheet.header}>
        <div>
          <h2>{savedNotice ? "Sparring logged" : existing ? "Update sparring report" : "Log your sparring"}</h2>
          {savedNotice ? null : (
            <p className={sheet.source}>
              {draft.title}
              {existing ? ` · ${formatAppDate(existing.training_day)}`
                : draft.externalSessionId ? " · Session saved. This is optional." : " · Takes 15 seconds."}
            </p>
          )}
        </div>
        <button className={sheet.close} type="button" aria-label="Close sparring log" disabled={isSubmitting} onClick={() => dismiss()}>
          ×
        </button>
      </header>
      {savedNotice ? (
        <div className={sheet.form}>
          <div className={sheet.body}>
            <p className={styles.safety} role="alert">{savedNotice}</p>
          </div>
          <footer className={sheet.actions}>
            <button type="button" className="cta" onClick={() => dismiss()}>Got it</button>
          </footer>
        </div>
      ) : (
        <form
          className={sheet.form}
          aria-label={existing ? "Update sparring report" : "Log your sparring"}
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <fieldset className={styles.questions} disabled={isSubmitting}>
            <div className={sheet.body}>
              <Choice
                legend="How hard was it?"
                options={["light", "medium", "hard"] as const}
                labels={INTENSITY_LABELS}
                selected={intensity}
                onSelect={answer(setIntensity)}
              />
              <div className={styles.rounds} role="group" aria-label="Rounds">
                <span className={styles.roundsLabel}>Rounds</span>
                <button
                  type="button"
                  aria-label="Fewer rounds"
                  disabled={rounds === null || rounds <= 0}
                  onClick={() => answer(setRounds)(Math.max(0, (rounds ?? 0) - 1))}
                >
                  −
                </button>
                <span className={`sparring-log-rounds-value ${styles.roundsValue}`} aria-live="polite" aria-label={rounds === null ? "Rounds not set" : undefined}>
                  {rounds ?? "–"}
                </span>
                <button
                  type="button"
                  aria-label="More rounds"
                  disabled={(rounds ?? 0) >= 30}
                  onClick={() => answer(setRounds)(Math.min(30, (rounds ?? 0) + 1))}
                >
                  +
                </button>
              </div>
              <Choice
                legend="Head contact"
                options={["none", "light", "heavy"] as const}
                labels={HEAD_CONTACT_LABELS}
                selected={headContact}
                onSelect={answer(setHeadContact)}
                locked={(value) => HEAD_CONTACT_ORDER.indexOf(value) < contactFloor}
              />
              <Choice
                legend="Got rocked or dropped?"
                options={["no", "yes"] as const}
                labels={{ no: "No", yes: "Yes" }}
                selected={rocked}
                onSelect={answer(setRocked)}
                locked={(value) => Boolean(existing?.rocked) && value === "no"}
              />
              {protectedEvidence ? <p className={styles.protected} role="note">{PROTECTED_EVIDENCE_COPY}</p> : null}
              {rocked === "yes" && !existing?.rocked ? (
                <p className={styles.safety} role="alert">
                  {ROCKED_SAFETY_COPY}
                </p>
              ) : null}
              <label className={styles.focus}>
                <span>One thing to work on next time <span className={styles.optional}>(optional)</span></span>
                <input
                  type="text"
                  value={focus}
                  maxLength={FOCUS_MAX_CHARS}
                  autoComplete="off"
                  enterKeyHint="done"
                  placeholder="e.g. Keep my right hand up after jabbing"
                  onChange={(event) => answer(setFocus)(event.target.value)}
                />
              </label>
              <button
                className={sheet.detailsToggle}
                type="button"
                aria-expanded={notesOpen}
                aria-controls={notesId}
                onClick={() => setNotesOpen((open) => !open)}
              >
                Add other notes <span aria-hidden="true">{notesOpen ? "−" : "+"}</span>
              </button>
              <label className={styles.notes} hidden={!notesOpen}>
                <span className="sr-only">Notes (optional)</span>
                <textarea
                  id={notesId}
                  value={notes}
                  maxLength={1000}
                  rows={2}
                  placeholder="Who with, what worked, what to fix"
                  onChange={(event) => answer(setNotes)(event.target.value)}
                />
              </label>
            </div>
          </fieldset>
          <footer className={`${sheet.actions} ${styles.actions}`}>
            {error ? (
              <p className={sheet.error} role="alert">
                {error}
              </p>
            ) : null}
            <button type="button" className="ghost-button" disabled={isSubmitting} onClick={() => dismiss()}>
              {existing ? "Cancel" : "Skip"}
            </button>
            <button type="submit" className="cta" disabled={isSubmitting}>
              {isSubmitting ? "Saving…" : existing ? "Save changes" : "Save"}
            </button>
          </footer>
        </form>
      )}
    </dialog>
  );
}
