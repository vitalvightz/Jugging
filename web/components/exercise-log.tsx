"use client";

import { createContext, useContext, useId, useMemo, useState, type ReactNode } from "react";

import {
  EXERCISE_LOG_REASONS,
  EXERCISE_LOG_REASON_LABELS,
  EXERCISE_LOG_STATUS_LABELS,
  amendedLog,
  asksForFirstLoad,
  buildLogEntry,
  buildSetLogEntry,
  setDraftsForFields,
  draftFromLog,
  hasLoggableNumbers,
  isLoggableBlock,
  lastPerformanceFor,
  exerciseIdentity,
  needsFirstLoad,
  nextStepLabel,
  logFieldsForBlock,
  logSummary,
  loggedValues,
  type ExerciseFeel,
  type LogDraft,
  type NextStep,
} from "@/lib/exercise-log";
import { cleanText } from "@/lib/structured-plan";
import { draftsFromActual, type SetDraft } from "@/lib/set-entries";
import { SetRows, SetResults } from "./set-rows";
import { weightRecommendation } from "@/lib/weight-recommendation";
import { WeightRecommendation } from "./weight-recommendation";
import type {
  ExerciseLogReason,
  ExerciseLogRecord,
  ExerciseLogRequest,
  ExerciseRecentLoad,
  StructuredBlock,
} from "@/lib/types";

/**
 * In-session logging for the exercise rows. The surface that owns the session
 * (Today) provides this once the session is started; everywhere else there is
 * no provider and the rows render exactly as before.
 */
export type ExerciseLogging = {
  canWrite?: boolean;
  /** Review surfaces prioritise completed measurements, including unreported sets. */
  hideWeightGuidance?: boolean;
  lowerLoads?: boolean;
  /** Today's saved logs, keyed by block id. */
  logs: Readonly<Record<string, ExerciseLogRecord>>;
  /** Saves (or corrects) one block's log. Rejects with a message to show. */
  save: (request: Omit<ExerciseLogRequest, "plan_id">) => Promise<void>;
  /** Saves several blocks at once (all or none), e.g. what the timer recorded. */
  saveMany?: (
    requests: Omit<ExerciseLogRequest, "plan_id">[],
    options?: { keepExisting?: boolean },
  ) => Promise<void>;
  /** Pain is health data: the reason is only offered with health consent. */
  painReasonAllowed: boolean;
  /** Last measured weights, offered as unconfirmed drafts. */
  recentLoads?: readonly ExerciseRecentLoad[];
  recentPerformances?: readonly ExerciseLogRecord[];
  /** Whether a raise may be suggested today (see `progressionOpen`). */
  allowProgression?: boolean;
  /** Where a failed one-tap save on a collapsed row is reported. */
  onError?: (message: string) => void;
};

const ExerciseLogContext = createContext<ExerciseLogging | null>(null);

export function ExerciseLogProvider({
  logging,
  children,
}: {
  logging: ExerciseLogging | null;
  children: ReactNode;
}) {
  return <ExerciseLogContext.Provider value={logging}>{children}</ExerciseLogContext.Provider>;
}

/** True while the owning surface (Today) has the session running and provides
 * logging; false in a preview or anywhere without a provider. */
export function useExerciseLoggingActive(): boolean {
  const logging = useContext(ExerciseLogContext);
  return logging !== null && logging.canWrite !== false;
}

function useBlockLogging(block: StructuredBlock) {
  const logging = useContext(ExerciseLogContext);
  const blockId = cleanText(block.block_id);
  const fields = useMemo(() => logFieldsForBlock(block), [block]);
  if (!logging || logging.canWrite === false || !blockId || !isLoggableBlock(block)) {
    return null;
  }
  return { logging, blockId, fields, log: logging.logs[blockId] ?? null };
}

// Exercises the athlete chose not to give a first weight for (bodyweight work,
// or simply not now), so the question is asked once, not every session. Kept on
// the device: it is a convenience, and "Log numbers" still records a weight.
const FIRST_LOAD_DECLINED_KEY = "unlxck:first-load-declined";

function declinedFirstLoads(): string[] {
  try {
    const stored: unknown = JSON.parse(window.localStorage.getItem(FIRST_LOAD_DECLINED_KEY) ?? "[]");
    return Array.isArray(stored) ? stored.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function firstLoadDeclined(block: StructuredBlock): boolean {
  return declinedFirstLoads().includes(exerciseIdentity(block));
}

function declineFirstLoad(block: StructuredBlock): void {
  try {
    const next = [...new Set([...declinedFirstLoads(), exerciseIdentity(block)])].slice(-200);
    window.localStorage.setItem(FIRST_LOAD_DECLINED_KEY, JSON.stringify(next));
  } catch {
    // Without storage the question simply returns next session.
  }
}

const FEELS: readonly ExerciseFeel[] = ["easy", "right", "hard"];
const FEEL_LABELS: Record<ExerciseFeel, string> = { easy: "Easy", right: "Right", hard: "Hard" };

function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : "Could not save. Try again.";
}

/**
 * The tick at the start of a row: one tap logs the exercise as written without
 * opening it. Once logged it shows the outcome, and a tap opens the row to
 * change it.
 */
export function ExerciseLogTick({
  block,
  onOpen,
}: {
  block: StructuredBlock;
  /** Opens the row, where a saved log is changed. */
  onOpen: () => void;
}) {
  const state = useBlockLogging(block);
  const [saving, setSaving] = useState(false);
  if (!state) {
    return null;
  }
  const { logging, blockId, log } = state;
  const name = cleanText(block.display_name) || "exercise";

  async function markDone() {
    setSaving(true);
    try {
      await logging.save({
        block_id: blockId,
        status: "as_prescribed", ...(log ? { actual: log.actual } : {}),
      });
      // Saved as written. Open the row only for the optional first weight.
      if (needsFirstLoad(block, logging.recentLoads) && !firstLoadDeclined(block)) onOpen();
    } catch (caught) {
      logging.onError?.(errorMessage(caught));
    } finally {
      setSaving(false);
    }
  }

  return (
    <button
      type="button"
      className="ex-log-tick"
      data-status={log?.status}
      disabled={saving}
      aria-label={
        log ? `${name}: ${EXERCISE_LOG_STATUS_LABELS[log.status].toLowerCase()}. Change` : `Mark ${name} done as written`
      }
      onClick={() => (log ? onOpen() : void markDone())}
    />
  );
}

export function useWeightContext() {
  return useContext(ExerciseLogContext);
}

/** Visible on the workout row before starting; suggestions never change the plan. */
export function ExerciseProgressionPreview({ block }: { block: StructuredBlock }) {
  const logging = useContext(ExerciseLogContext);
  if (!logging || logging.hideWeightGuidance || logging.logs[cleanText(block.block_id) || ""]) return null;
  const recommendation = weightRecommendation(block, logging);
  return <WeightRecommendation value={recommendation} />;
}

/** The numbers behind a saved log on a collapsed row: "80 kg", "Changed · 3 sets". */
export function ExerciseLogBadge({ block }: { block: StructuredBlock }) {
  const state = useBlockLogging(block);
  if (!state?.log) {
    return null;
  }
  const summary = logSummary(state.fields, state.log);
  // The tick already says "done"; a line is only worth it when there is more.
  if (state.log.status === "as_prescribed" && summary === EXERCISE_LOG_STATUS_LABELS.as_prescribed
    && !asksForFirstLoad(block, state.log, state.logging.recentLoads)) {
    return null;
  }
  return (
    <span className="ex-row-log" data-status={state.log.status}>
      {summary}
      {asksForFirstLoad(block, state.log, state.logging.recentLoads) ? " · Weight missing — open to add or skip" : ""}
    </span>
  );
}

function ReasonChips({
  selected,
  painAllowed,
  skipped = false,
  disabled,
  onSelect,
}: {
  selected: ExerciseLogReason | null;
  painAllowed: boolean;
  /** Nobody skips an exercise because they felt strong. */
  skipped?: boolean;
  disabled: boolean;
  onSelect: (reason: ExerciseLogReason | null) => void;
}) {
  const reasons = EXERCISE_LOG_REASONS.filter(
    (reason) => (painAllowed || reason !== "pain") && !(skipped && reason === "felt_strong"),
  );
  return (
    <div className="ex-log-reasons" role="group" aria-label="Why? (optional)">
      {reasons.map((reason) => (
        <button
          key={reason}
          type="button"
          className="ex-log-chip"
          aria-pressed={selected === reason}
          disabled={disabled}
          onClick={() => onSelect(selected === reason ? null : reason)}
        >
          {EXERCISE_LOG_REASON_LABELS[reason]}
        </button>
      ))}
    </div>
  );
}

/**
 * The log controls inside an open exercise row. The prescription above it is
 * read-only; this records what was done against it.
 */
export function ExerciseLogPanel({ block }: { block: StructuredBlock }) {
  const state = useBlockLogging(block);
  const fieldId = useId();
  const [editing, setEditing] = useState(false);
  // Re-opens the choices over a saved log, to change its outcome.
  const [choosing, setChoosing] = useState(false);
  const [draft, setDraft] = useState<LogDraft>({});
  const [setRows, setSetRows] = useState<SetDraft[] | null>(null);
  const [setUnit, setSetUnit] = useState<string | null>(null);
  const [reason, setReason] = useState<ExerciseLogReason | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Entries far from the prescription that the athlete has been asked to confirm.
  const [confirming, setConfirming] = useState<string[] | null>(null);
  const [firstLoad, setFirstLoad] = useState("");
  const [firstLoadSkipped, setFirstLoadSkipped] = useState(false);

  if (!state) {
    return null;
  }
  const { logging, blockId, fields, log } = state;
  const recommendation = weightRecommendation(block, logging);
  const suggested = recommendation.suggested;
  const previous = lastPerformanceFor(block, logging.recentPerformances);
  // The shared recommendation, as a target the athlete can set up and confirm.
  const nextStep: NextStep | null = recommendation.tryReps
    ? { kind: "reps", reps: recommendation.tryReps }
    : recommendation.calculated && suggested && recommendation.last && suggested.unit === recommendation.last.unit && suggested.value > recommendation.last.value
      ? { kind: "load", load: suggested }
      : null;
  const loadField = fields.find(field => field.key === "load");
  const askFirstLoad = !firstLoadSkipped && asksForFirstLoad(block, log, logging.recentLoads) && !firstLoadDeclined(block);
  const loadUnit = setUnit ?? fields.find(field => field.key === "load")?.apiUnit ?? "kg";
  const entry = setRows ? buildSetLogEntry(fields, setRows, loadUnit, draft) : buildLogEntry(fields, draft);
  const supportsSets = fields.some(field => field.key === "sets") && fields.some(field => field.key === "reps") && !fields.some(field => field.key === "rounds");

  function closeEditor() {
    setEditing(false);
    setChoosing(false);
    setError(null);
    setConfirming(null);
  }

  /** Opens the numbers, optionally set up with a suggested target to confirm once it is done. */
  function openEditor(target?: NextStep) {
    const individualHistory = !log?.actual.load && !log?.actual.set_entries?.length && supportsSets && previous?.actual.set_entries?.length
      && !suggested && recommendation.sets.some(row => row.suggested);
    setSetRows(log?.actual?.set_entries?.length ? draftsFromActual(log.actual)
      : individualHistory ? setDraftsForFields(fields, log ? draftFromLog(fields, log) : {}).map((row, at) => ({ ...row,
        load: recommendation.sets[at]?.suggested ? String(recommendation.sets[at].suggested!.value) : "", rpe: log?.actual.effort ? String(log.actual.effort.value) : "",
        ...(target?.kind === "reps" ? { reps: String(target.reps) } : {}),
      })) : null);
    setSetUnit(log?.actual?.set_entries?.find(row => row.load)?.load?.unit ?? null);
    // A fresh entry starts from last time's weight, so only a change is typed.
    setDraft({
      ...(log ? draftFromLog(fields, log) : draftFromLog(fields,
        suggested ? { actual: { load: suggested } } as ExerciseLogRecord : null)),
      // Nothing is recorded yet: the athlete saves it after doing the work.
      ...(target?.kind === "reps" ? { reps: String(target.reps) } : {}),
    });
    setReason(log?.reason ?? null);
    setError(null);
    setConfirming(null);
    setEditing(true);
  }

  async function save(request: Omit<ExerciseLogRequest, "plan_id" | "block_id">) {
    setSaving(true);
    setError(null);
    try {
      await logging.save({ block_id: blockId, ...request });
      closeEditor();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSaving(false);
    }
  }

  function saveNumbers() {
    if (entry.invalid.length > 0) {
      const labels = fields.filter((field) => entry.invalid.includes(field.key)).map((field) => field.label);
      setError(`Check ${labels.join(", ")}.`);
      return;
    }
    if (entry.outliers.length > 0 && confirming?.join("|") !== entry.outliers.join("|")) {
      setError(null);
      setConfirming(entry.outliers);
      return;
    }
    void save({
      status: entry.status,
      actual: entry.actual,
      reason: entry.status === "modified" ? reason : null,
    });
  }

  function saveFirstLoad() {
    if (!log || !loadField) return;
    const typed = buildLogEntry([loadField], { load: firstLoad });
    if (!typed.actual.load || typed.invalid.length > 0) {
      setError("Enter the weight as a number.");
      return;
    }
    if (typed.outliers.length > 0 && confirming?.join("|") !== typed.outliers.join("|")) {
      setError(null);
      setConfirming(typed.outliers);
      return;
    }
    const actual = log.actual.set_entries?.length
      ? { ...log.actual, set_entries: log.actual.set_entries.map(row =>
          row.load == null ? { ...row, load: typed.actual.load } : row) }
      : { ...log.actual, load: typed.actual.load };
    void save({ status: log.status, reason: log.reason, notes: log.notes, actual });
  }

  const alert = error ? (
    <p className="form-error ex-log-error" role="alert">
      {error}
    </p>
  ) : null;

  if (editing) {
    return (
      <div className="ex-log" data-mode="numbers">
        {setRows ? <SetRows rows={setRows} unit={loadUnit} showLoad={fields.some(field => field.key === "load")}
          disabled={saving} invalid={entry.invalid.includes("reps") ? setRows.map((_, index) => index) : []}
          repsHint={fields.find(field => field.key === "reps")?.hint} onChange={rows => { setSetRows(rows); setConfirming(null); }} /> : null}
        {(!setRows || fields.some(field => !["sets", "reps", "load", "effort"].includes(field.key))) ? <details className="ex-log-details" open={!setRows || undefined}>
        <summary hidden={!setRows}>More details</summary>
        <div className="ex-log-fields" data-count={fields.length}>
          {fields.filter(field => !setRows || !["sets", "reps", "load", "effort"].includes(field.key)).map((field) => (
            <label key={field.key} className="ex-log-field" htmlFor={`${fieldId}-${field.key}`}>
              <span className="ex-log-field-label">
                {field.label}
                {field.unit ? <span className="ex-log-field-unit"> {field.unit}</span> : null}
              </span>
              <input
                id={`${fieldId}-${field.key}`}
                type="text"
                inputMode="decimal"
                autoComplete="off"
                enterKeyHint="done"
                maxLength={7}
                value={draft[field.key] ?? ""}
                placeholder={field.hint ?? "–"}
                aria-invalid={entry.invalid.includes(field.key) || undefined}
                disabled={saving}
                onChange={(event) => {
                  setDraft((current) => ({ ...current, [field.key]: event.target.value }));
                  setConfirming(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    saveNumbers();
                  }
                }}
              />
            </label>
          ))}
        </div>
        </details> : null}
        {supportsSets && !setRows ? <button type="button" className="ex-log-link" disabled={saving} onClick={() => setSetRows(setDraftsForFields(fields, draft))}>Edit sets individually</button> : null}
        {entry.status === "modified" ? (
          <ReasonChips
            selected={reason}
            painAllowed={logging.painReasonAllowed}
            disabled={saving}
            onSelect={setReason}
          />
        ) : null}
        {confirming ? (
          <p className="ex-log-confirm" role="alert">
            {confirming.join("; ")}?
          </p>
        ) : null}
        {alert}
        <div className="ex-log-bar">
          <button type="button" className="ex-log-link" disabled={saving} onClick={closeEditor}>
            Cancel
          </button>
          <button type="button" className="ex-log-save" disabled={saving} onClick={saveNumbers}>
            {saving ? "Saving…" : confirming ? "Yes, save" : "Save"}
          </button>
        </div>
      </div>
    );
  }

  if (log && !choosing) {
    const values = log.status === "skipped" || log.actual?.set_entries?.length ? [] : loggedValues(fields, log);
    return (
      <div className="ex-log" data-mode="saved" role="group" aria-label="Today's actual">
        <p className="kicker">Today&apos;s actual</p>
        {/* The whole line reopens the log, so it keeps the row's full width. */}
        <button
          type="button"
          className="ex-log-edit"
          disabled={saving}
          aria-label={`${[
            EXERCISE_LOG_STATUS_LABELS[log.status],
            ...values.map((value) => (value.insteadOf ? `${value.text} not ${value.insteadOf}` : value.text)),
            ...(log.reason && log.status !== "skipped" ? [EXERCISE_LOG_REASON_LABELS[log.reason]] : []),
          ].join(", ")}. Change`}
          onClick={() => setChoosing(true)}
        >
          <span className="ex-log-status" data-status={log.status}>
            <span className="ex-log-status-label">{EXERCISE_LOG_STATUS_LABELS[log.status]}</span>
            {values.map((value) => (
              <span key={value.key} className="ex-log-status-value">
                {value.text}
                {value.insteadOf ? <span className="ex-log-instead"> not {value.insteadOf}</span> : null}
              </span>
            ))}
            {log.reason && log.status !== "skipped" ? (
              <span className="ex-log-status-value">{EXERCISE_LOG_REASON_LABELS[log.reason]}</span>
            ) : null}
          </span>
          <svg className="ex-log-edit-icon" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false">
            <path d="M4 20h4L18.5 9.5a2.12 2.12 0 0 0-3-3L5 17v3Z" />
            <path d="m14.5 7.5 3 3" />
          </svg>
        </button>
        {log.actual?.set_entries?.length ? <SetResults entries={log.actual.set_entries} completed={log.actual.sets} /> : null}
        {/* Asked once, when there is no earlier weight to carry forward. Never required. */}
        {askFirstLoad && loadField ? (
          <div className="ex-log-first-load">
            <label className="ex-log-field" htmlFor={`${fieldId}-first-load`}>
              <span className="ex-log-field-label">
                {log.actual.set_entries?.length ? "Weight used for sets with missing weights" : "What weight did you use?"}
                <span className="ex-log-field-unit"> {loadField.unit}</span>
              </span>
              <input
                id={`${fieldId}-first-load`}
                type="text"
                inputMode="decimal"
                autoComplete="off"
                enterKeyHint="done"
                maxLength={7}
                value={firstLoad}
                placeholder="–"
                disabled={saving}
                onChange={(event) => {
                  setFirstLoad(event.target.value);
                  setConfirming(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    saveFirstLoad();
                  }
                }}
              />
            </label>
            {confirming ? (
              <p className="ex-log-confirm" role="alert">
                {confirming.join("; ")}?
              </p>
            ) : null}
            <div className="ex-log-bar">
              <button
                type="button"
                className="ex-log-link"
                disabled={saving}
                onClick={() => {
                  declineFirstLoad(block);
                  setFirstLoadSkipped(true);
                  setError(null);
                  setConfirming(null);
                }}
              >
                Skip
              </button>
              <button type="button" className="ex-log-save" disabled={saving || !firstLoad.trim()} onClick={saveFirstLoad}>
                {saving ? "Saving…" : confirming ? "Yes, save" : "Save weight"}
              </button>
            </div>
          </div>
        ) : null}
        {/* Optional: read beside the reps done, it shapes next time's suggestion. */}
        {log.status !== "skipped" && fields.some(field => field.key === "reps" || field.key === "load") ? (
          <>
          <p className="ex-log-suggest-hint">Compared with the plan</p>
          <div className="ex-log-reasons" role="group" aria-label="Compared with the plan, how did it feel? (optional)">
            {FEELS.map((feel) => (
              <button
                key={feel}
                type="button"
                className="ex-log-chip"
                aria-pressed={log.actual?.feel === feel}
                disabled={saving}
                onClick={() => void save(amendedLog(log, { feel: log.actual?.feel === feel ? null : feel }))}
              >
                {FEEL_LABELS[feel]}
              </button>
            ))}
          </div>
          </>
        ) : null}
        {/* A skip saves in one tap; why is a second, optional tap. */}
        {log.status === "skipped" ? (
          <ReasonChips
            selected={log.reason}
            painAllowed={logging.painReasonAllowed}
            skipped
            disabled={saving}
            onSelect={(next) => void save({ status: "skipped", reason: next })}
          />
        ) : null}
        {alert}
      </div>
    );
  }

  return (
    <div className="ex-log" data-mode="idle">
      {/* Sets the numbers up with the target; it is saved only once the athlete has done it. */}
      {nextStep ? (
        <button type="button" className="ex-log-suggest" disabled={saving} onClick={() => openEditor(nextStep)}>
          {nextStepLabel(nextStep)}
          <span className="ex-log-suggest-hint">Confirm when done</span>
        </button>
      ) : null}
      <div className="ex-log-choices">
        <button
          type="button"
          className="ex-log-choice ex-log-choice-primary"
          disabled={saving}
          onClick={() => void save({ status: "as_prescribed" })}
        >
          Done
        </button>
        {hasLoggableNumbers(fields) ? (
          <button type="button" className="ex-log-choice" disabled={saving} onClick={() => openEditor()}>
            Log numbers
          </button>
        ) : null}
        <button
          type="button"
          className="ex-log-choice"
          disabled={saving}
          onClick={() => void save({ status: "skipped" })}
        >
          Skip
        </button>
        {log ? (
          <button type="button" className="ex-log-link" disabled={saving} onClick={closeEditor}>
            Cancel
          </button>
        ) : null}
      </div>
      {alert}
    </div>
  );
}
