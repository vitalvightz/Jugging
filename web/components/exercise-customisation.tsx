"use client";

import { createContext, useCallback, useContext, useEffect, useId, useMemo, useState, type ReactNode } from "react";
import Link from "next/link";

import { useSeenOnce } from "@/components/once-hint";
import { ExerciseDemo } from "@/components/exercise-demo";
import { ExercisePrescriptionEditor } from "@/components/exercise-prescription-editor";
import { usePastExercises } from "@/components/load-field";
import { withPastDose } from "@/lib/load-entry";
import { doseFromBlock, finishedDose, type DoseDraft } from "@/lib/exercise-dose";
import { useToast } from "@/components/toast-provider";
import { AutoHeight } from "@/components/today/auto-height";
import { Collapse } from "@/components/today/collapse";
import { InjuryDetailSheet } from "@/components/today/injury-detail-sheet";
import {
  ApiError,
  applyExerciseChange,
  getExerciseAdditions,
  getExerciseAlternatives,
  undoExerciseChange,
} from "@/lib/api";
import { demoThumbnailUrl } from "@/lib/exercise-demo";
import {
  canAddToSession,
  isCustomisableBlock,
  isCustomisableSession,
  isEditableDate,
  newClientRequestId,
} from "@/lib/exercise-customisation";
import { cleanText } from "@/lib/structured-plan";
import type {
  ExerciseAlternative,
  ExerciseChangeDose,
  ExerciseChangeRequest,
  ExerciseChangeResult,
  ExerciseOptionsResponse,
  StructuredBlock,
  StructuredSession,
} from "@/lib/types";
import styles from "./exercise-customisation.module.css";

/**
 * Lightweight personalisation of an app-generated workout: swap, add or remove
 * one exercise from the card itself. The surface that owns the plan (Today,
 * Plan Detail) provides this; without a provider the cards render as before.
 */
export type ExerciseCustomisation = {
  token: string;
  planId: string;
  /** The athlete-local training day. Earlier days are never offered. */
  today: string;
  canRepeat?: boolean;
  /** Workouts already started or logged keep their prescription. */
  isLocked?: (date: string, sessionId: string) => boolean;
  /** Apply the server's effective session at once, then reload quietly. */
  onSessionChanged: (date: string, session: StructuredSession | null) => void;
};

const CustomisationContext = createContext<ExerciseCustomisation | null>(null);

export function ExerciseCustomisationProvider({
  value,
  children,
}: {
  value: ExerciseCustomisation | null;
  children: ReactNode;
}) {
  return <CustomisationContext.Provider value={value}>{children}</CustomisationContext.Provider>;
}

type SessionScope = { customisation: ExerciseCustomisation; date: string; session: StructuredSession; sessionId: string };

const SessionScopeContext = createContext<SessionScope | null>(null);

/** Names the dated workout the rows below belong to; inert when not editable. */
export function ExerciseSessionScope({
  date,
  session,
  children,
}: {
  date: string | null | undefined;
  session: StructuredSession;
  children: ReactNode;
}) {
  const customisation = useContext(CustomisationContext);
  const day = cleanText(date)?.slice(0, 10) ?? null;
  const sessionId = cleanText(session.session_id);
  const editable = Boolean(
    customisation && day && sessionId && isCustomisableSession(session)
      && isEditableDate(day, customisation.today)
      && !customisation.isLocked?.(day, sessionId),
  );
  const scope = useMemo(
    () => (editable && customisation && day && sessionId ? { customisation, date: day, session, sessionId } : null),
    [editable, customisation, day, session, sessionId],
  );
  return <SessionScopeContext.Provider value={scope}>{children}</SessionScopeContext.Provider>;
}

function messageOf(caught: unknown, fallback: string): string {
  if (caught instanceof ApiError || caught instanceof Error) {
    return caught.message.replace(/\s*\(request id: [^)]*\)\s*$/, "") || fallback;
  }
  return fallback;
}

// One change confirmation at a time: a new one replaces the last instead of
// stacking toasts down the screen.
let lastChangeToast: number | null = null;

/** Shared write path: apply, update the card, offer Undo. */
function useExerciseChange(scope: SessionScope | null) {
  const { showToast: show, dismissToast } = useToast();
  const showToast = useCallback(
    (message: string, options?: Parameters<typeof show>[1]) => {
      if (lastChangeToast !== null) dismissToast(lastChangeToast);
      lastChangeToast = show(message, options);
      return lastChangeToast;
    },
    [show, dismissToast],
  );
  const apply = useCallback(
    async (body: Omit<ExerciseChangeRequest, "client_request_id" | "date" | "session_id">, done: string) => {
      if (!scope) throw new Error("This workout can't be changed.");
      const { customisation, date, sessionId } = scope;
      const result = await applyExerciseChange(customisation.token, customisation.planId, {
        client_request_id: newClientRequestId(),
        date,
        session_id: sessionId,
        ...body,
      });
      customisation.onSessionChanged(date, result.session ?? null);
      showToast(done, {
        tone: "success",
        durationMs: 6000,
        action: {
          label: "Undo",
          onClick: () => {
            void undoExerciseChange(customisation.token, customisation.planId, result.id)
              .then((undone) => customisation.onSessionChanged(date, undone.session ?? null))
              .catch((caught: unknown) => showToast(messageOf(caught, "Couldn't undo. Try again."), { tone: "error" }));
          },
        },
      });
      return result;
    },
    [scope, showToast],
  );
  const restore = useCallback(
    async (changeId: string, name: string, done = `Restored ${name}`) => {
      if (!scope) return;
      const { customisation, date } = scope;
      try {
        const result = await undoExerciseChange(customisation.token, customisation.planId, changeId, date);
        customisation.onSessionChanged(date, result.session ?? null);
        showToast(done, { tone: "success" });
      } catch (caught) {
        showToast(messageOf(caught, "Couldn't restore. Try again."), { tone: "error" });
      }
    },
    [scope, showToast],
  );
  return { apply, restore };
}

export function DotsIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="5" cy="12" r="1.6" />
      <circle cx="12" cy="12" r="1.6" />
      <circle cx="19" cy="12" r="1.6" />
    </svg>
  );
}

type Panel = "menu" | "swap" | "edit" | null;

/** The discreet three-dot control on an exercise row. */
export function ExerciseActions({ block, title }: { block: StructuredBlock; title: string }) {
  const scope = useContext(SessionScopeContext);
  const [panel, setPanel] = useState<Panel>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editScope, setEditScope] = useState<"occurrence" | "future">("occurrence");
  const { apply, restore } = useExerciseChange(scope);
  // Read only once Edit opens, for last time's weight.
  const past = usePastExercises(panel === "edit" ? scope?.customisation.token : null);
  if (!scope || !isCustomisableBlock(block)) return null;
  const change = block.athlete_change;
  const blockId = cleanText(block.block_id)!;

  function close() {
    setPanel(null);
    setConfirm(null);
    setError(null);
  }

  async function remove(confirmed: boolean) {
    setBusy(true);
    setError(null);
    try {
      await apply({ action: "remove", block_id: blockId, confirmed }, `Removed ${title}`);
      close();
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "confirm_remove") {
        setConfirm(messageOf(caught, "Remove it anyway?"));
      } else {
        setError(messageOf(caught, "Couldn't remove it. Try again."));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        className={styles.dots}
        data-exercise-actions=""
        aria-label={`Change ${title}`}
        aria-haspopup="dialog"
        onClick={(event) => {
          event.stopPropagation();
          setPanel("menu");
        }}
      >
        <DotsIcon />
      </button>
      {panel === "menu" ? (
        <InjuryDetailSheet title={title} onClose={close} busy={busy} panelClassName={styles.sheet}>
          <div className={styles.menu}>
            {change?.original_name && change.action === "swap" ? (
              <p className={styles.provenance}>Swapped from {change.original_name}</p>
            ) : null}
            {confirm ? (
              <div className={styles.confirm} role="alert">
                <p>{confirm}</p>
                <div className={styles.inlineActions}>
                  <button type="button" className={styles.secondary} onClick={() => setConfirm(null)} disabled={busy}>
                    Keep
                  </button>
                  <button type="button" className={styles.danger} onClick={() => void remove(true)} disabled={busy}>
                    {busy ? "Removing…" : "Remove"}
                  </button>
                </div>
              </div>
            ) : (
              <div className={styles.actionBar} role="group" aria-label={`Change ${title}`}>
                <button type="button" className={styles.action} onClick={() => setPanel("edit")} disabled={busy}>
                  <span>Edit</span>
                </button>
                <button type="button" className={styles.action} onClick={() => setPanel("swap")} disabled={busy}>
                  <SwapIcon />
                  <span>Swap</span>
                </button>
                {change?.override_id && change.action === "swap" && change.original_name ? (
                  <button
                    type="button"
                    className={styles.action}
                    aria-label={`Restore ${change.original_name}`}
                    disabled={busy}
                    onClick={() => {
                      setBusy(true);
                      void restore(change.override_id!, change.original_name!).finally(() => {
                        setBusy(false);
                        close();
                      });
                    }}
                  >
                    <RestoreIcon />
                    <span>Restore</span>
                  </button>
                ) : null}
                {change?.override_id && change.action === "edit" ? (
                  <button
                    type="button"
                    className={styles.action}
                    aria-label={`Revert edit to ${title}`}
                    disabled={busy}
                    onClick={() => {
                      setBusy(true);
                      void restore(change.override_id!, title, `Reverted ${title}`).finally(() => {
                        setBusy(false);
                        close();
                      });
                    }}
                  >
                    <RestoreIcon />
                    <span>Revert</span>
                  </button>
                ) : null}
                <button
                  type="button"
                  className={styles.action}
                  data-tone="danger"
                  aria-label={`Remove ${title}`}
                  onClick={() => void remove(false)}
                  disabled={busy}
                >
                  <RemoveIcon />
                  <span>{busy ? "Removing…" : "Remove"}</span>
                </button>
              </div>
            )}
            {error ? <p className={styles.error} role="alert">{error}</p> : null}
          </div>
        </InjuryDetailSheet>
      ) : null}
      {panel === "swap" ? (
        <ExerciseOptionsSheet
          mode="swap"
          title={`Swap ${title}`}
          entrance="push"
          blockId={blockId}
          onClose={close}
          onApply={(option, scopeValue, dose) =>
            apply(
              { action: "swap", block_id: blockId, catalogue_id: option.catalogue_id, scope: scopeValue, dose },
              `Swapped to ${option.name}`,
            )
          }
        />
      ) : null}
      {panel === "edit" ? <InjuryDetailSheet title={`Edit ${title}`} onClose={close} busy={busy} panelClassName={styles.sheet} entrance="push">
        <div className={styles.editCard}>
          <div className="ex-row" data-open="true">
            <div className="ex-row-head"><span className="ex-row-text">
              <span className="ex-row-title">{title}</span>
              <span className="ex-row-meta">{block.block_type?.replaceAll("_", " ")}</span>
            </span></div>
            <ExercisePrescriptionEditor initial={doseFromBlock(block)} busy={busy} action="Save changes"
              past={past?.get(cleanText(block.exercise_key) ?? "")}
              coaching={{
                coaching_cues: block.coaching_cues ?? [], regression_options: block.regression_options ?? [],
                progression_rule: block.progression_rule ?? null, stop_rules: block.stop_rules ?? [],
              }}
              onSubmit={(dose, coaching) => {
                setBusy(true); setError(null);
                void apply({ action: "edit", block_id: blockId, dose: JSON.stringify(dose) === JSON.stringify(finishedDose(doseFromBlock(block))) ? undefined : dose, ...coaching, scope: editScope }, `Updated ${title}`)
                  .then(close).catch(caught => setError(messageOf(caught, "Couldn't save. Try again.")))
                  .finally(() => setBusy(false));
              }}>
              {/* Where the edit applies is chosen before saving, not after. */}
              {scope.customisation.canRepeat ? <div className={styles.scopes} role="radiogroup" aria-label="Apply edits to">
                <button type="button" className={styles.scope} role="radio" aria-checked={editScope === "occurrence"} onClick={() => setEditScope("occurrence")}>Just this workout</button>
                <button type="button" className={styles.scope} role="radio" aria-checked={editScope === "future"} onClick={() => setEditScope("future")}>Every {weekdayName(scope.date)}</button>
              </div> : null}
            </ExercisePrescriptionEditor>
          </div>
          {error ? <p className={styles.error} role="alert">{error}</p> : null}
        </div>
      </InjuryDetailSheet> : null}
    </>
  );
}

/** The session's foot: "+ Add exercise" and any removed exercises, one line. */
export function SessionChangesFoot({ session, canAdd = true }: { session: StructuredSession; canAdd?: boolean }) {
  const scope = useContext(SessionScopeContext);
  if (!scope) return null;
  const hasRemoved = (session.athlete_removed ?? []).some((item) => item?.override_id && item.display_name);
  if (!hasRemoved && !(canAdd && canAddToSession(scope.session))) return null;
  return (
    <div className={styles.foot}>
      {canAdd ? <AddExerciseButton /> : null}
      <RemovedExercises session={session} />
    </div>
  );
}

/** "+ Add exercise" at the foot of an eligible session. */
export function AddExerciseButton() {
  const scope = useContext(SessionScopeContext);
  const [open, setOpen] = useState(false);
  const { apply } = useExerciseChange(scope);
  if (!scope || !canAddToSession(scope.session)) return null;
  return (
    <>
      <button type="button" className={styles.add} aria-haspopup="dialog" onClick={() => setOpen(true)}>
        <span aria-hidden="true">+</span> Add exercise
      </button>
      {open ? (
        <ExerciseOptionsSheet
          mode="add"
          title="Add exercise"
          onClose={() => setOpen(false)}
          onApply={(option, scopeValue, dose) =>
            apply({ action: "add", catalogue_id: option.catalogue_id, scope: scopeValue, dose }, `Added ${option.name}`)
          }
        />
      ) : null}
    </>
  );
}

/** Exercises removed from this workout, each one tap from coming back. */
export function RemovedExercises({ session }: { session: StructuredSession }) {
  const scope = useContext(SessionScopeContext);
  const { restore } = useExerciseChange(scope);
  const removed = (session.athlete_removed ?? []).filter((item) => item?.override_id && item.display_name);
  if (!scope || removed.length === 0) return null;
  return (
    <ul className={styles.removed} aria-label="Removed exercises">
      {removed.map((item) => (
        <li key={item.override_id!}>
          <button
            type="button"
            aria-label={`Restore ${item.display_name}`}
            title={`Restore ${item.display_name}`}
            onClick={() => void restore(item.override_id!, item.display_name!)}
          >
            <RestoreIcon />
            <span>Restore {item.display_name}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

type Scope = "occurrence" | "future";

/** "Monday" for an ISO date, read as a calendar day (never shifted by timezone). */
export function weekdayName(isoDate: string): string {
  return new Date(`${isoDate.slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-GB", { weekday: "long", timeZone: "UTC" });
}

function ExerciseOptionsSheet({
  mode,
  title,
  blockId,
  entrance = "rise",
  onClose,
  onApply,
}: {
  mode: "swap" | "add";
  title: string;
  blockId?: string;
  /** "push" when it replaces the exercise menu rather than opening fresh. */
  entrance?: "rise" | "push";
  onClose: () => void;
  onApply: (option: ExerciseAlternative, scope: Scope, dose: ExerciseChangeDose | null) => Promise<ExerciseChangeResult>;
}) {
  const scope = useContext(SessionScopeContext);
  const [showAll, setShowAll] = useState(false);
  const [loaded, setLoaded] = useState<{ key: string; response: ExerciseOptionsResponse } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [scopeHint, dismissScopeHint] = useSeenOnce("discovery-exercise-change-scope-seen");
  const [selectedScope, setSelectedScope] = useState<Scope>("occurrence");
  const [pending, setPending] = useState<string | null>(null);
  const [dosing, setDosing] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  // Add searches the whole training library; the debounced term drives the read.
  const [search, setSearch] = useState("");
  const [term, setTerm] = useState("");
  const scopeLabelId = useId();
  // What the athlete did last time fills a prescription the library leaves open.
  const past = usePastExercises(scope?.customisation.token);

  useEffect(() => {
    const timer = window.setTimeout(() => setTerm(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  const requestKey = `${showAll}|${term}|${reload}`;
  // The last results stay in view (dimmed) while the next search loads.
  const data = loaded?.response ?? null;
  const refreshing = Boolean(loaded) && loaded!.key !== requestKey && !loadError;

  useEffect(() => {
    if (!scope) return;
    const controller = new AbortController();
    const { customisation, date, sessionId } = scope;
    const request =
      mode === "swap" && blockId
        ? getExerciseAlternatives(customisation.token, customisation.planId,
          { date, sessionId, blockId, all: showAll }, controller.signal)
        : getExerciseAdditions(customisation.token, customisation.planId,
          { date, sessionId, all: showAll, q: term }, controller.signal);
    request
      .then((response) => {
        if (controller.signal.aborted) return;
        setLoaded({ key: requestKey, response });
        setLoadError(null);
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) setLoadError(messageOf(caught, "Couldn't load options."));
      });
    return () => controller.abort();
  }, [scope, mode, blockId, showAll, reload, term, requestKey]);

  async function choose(option: ExerciseAlternative, dose: ExerciseChangeDose | null) {
    if (pending) return;
    if (option.dose_required && !dose) {
      setDosing((current) => (current === option.catalogue_id ? null : option.catalogue_id));
      return;
    }
    setPending(option.catalogue_id);
    setApplyError(null);
    try {
      await onApply(option, selectedScope, dose);
      dismissScopeHint();
      onClose();
    } catch (caught) {
      setApplyError(messageOf(caught, "Couldn't save that change. Try again."));
    } finally {
      setPending(null);
    }
  }

  const scopes = data?.scopes ?? [];
  const loading = !data && !loadError;
  return (
    <InjuryDetailSheet title={title} onClose={onClose} busy={Boolean(pending)} panelClassName={styles.sheet} entrance={entrance}>
      {/* Results arriving, a search narrowing them or "See all" adding more:
          the sheet glides to its new height instead of jumping. */}
      <AutoHeight>
        <div className={styles.body} aria-busy={loading || refreshing}>
          {mode === "add" ? (
            <div className={styles.search}>
              <SearchIcon />
              <input
                type="search"
                aria-label="Search the exercise library"
                placeholder="Search the library"
                value={search}
                maxLength={100}
                autoComplete="off"
                enterKeyHint="search"
                onChange={(event) => setSearch(event.target.value)}
              />
              {search ? (
                <button type="button" aria-label="Clear search" onClick={() => setSearch("")}>
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17" /></svg>
                </button>
              ) : null}
            </div>
          ) : null}
          {scopes.length > 1 ? (
            <div className={styles.scopes} role="radiogroup" aria-labelledby={scopeLabelId}>
              <span id={scopeLabelId} className="sr-only">Apply to</span>
              {scopes.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={selectedScope === option.value}
                  className={styles.scope}
                  onClick={() => { dismissScopeHint(); setSelectedScope(option.value); }}
                >
                  {option.label}
                </button>
              ))}
            </div>
          ) : null}
          {scopeHint && scopes.length > 1 ? <div className={styles.discoveryHint} role="note">
            <span>Choose whether this change applies to this workout or future sessions.</span>
            <button type="button" onClick={dismissScopeHint} aria-label="Dismiss exercise change scope tip">×</button>
          </div> : null}
          {loading ? (
            <ul className={styles.options} aria-label="Loading options">
              {[0, 1, 2].map((index) => (
                <li key={index} className={styles.skeleton} aria-hidden="true" />
              ))}
            </ul>
          ) : null}
          {loadError ? (
            <div className={styles.notice} role="alert">
              <p>{loadError}</p>
              <button type="button" className={styles.secondary} onClick={() => setReload((value) => value + 1)}>
                Try again
              </button>
            </div>
          ) : null}
          {data && data.alternatives.length > 0 ? (
            <ul className={styles.options} data-refreshing={refreshing}>
              {data.alternatives.map((option) => (
                <li key={option.catalogue_id} className={styles.option} data-pending={pending === option.catalogue_id}>
                  <div className={styles.optionRow}>
                    <button
                      type="button"
                      className={styles.optionMain}
                      onClick={() => void choose(option, null)}
                      disabled={Boolean(pending)}
                      aria-expanded={option.dose_required ? dosing === option.catalogue_id : undefined}
                    >
                      <span className={styles.optionLine}>
                        <span className={styles.optionName}>{option.name}</span>
                        <span className={styles.optionDose}>
                          {pending === option.catalogue_id
                            ? mode === "swap" ? "Swapping…" : "Adding…"
                            : option.prescription ?? (option.dose_required ? "Set dose" : null)}
                        </span>
                      </span>
                      {option.reason || option.equipment?.length ? (
                        <span className={styles.optionLine}>
                          <span className={styles.optionReason}>{option.reason}</span>
                          {option.equipment?.length ? (
                            <span className={styles.optionKit}>{option.equipment.join(", ")}</span>
                          ) : null}
                        </span>
                      ) : null}
                    </button>
                    {option.media?.video_id ? (
                      <button
                        type="button"
                        className={styles.thumb}
                        aria-label={`${preview === option.catalogue_id ? "Hide" : "Watch"} ${option.name} demo`}
                        aria-expanded={preview === option.catalogue_id}
                        onClick={() => setPreview((current) => (current === option.catalogue_id ? null : option.catalogue_id))}
                      >
                        {/* eslint-disable-next-line @next/next/no-img-element -- remote YouTube thumbnail, not a local asset */}
                        <img src={demoThumbnailUrl(option.media.video_id, "mq")} alt="" loading="lazy" />
                        <span className="ex-demo-play-glyph ex-demo-play-glyph-sm" aria-hidden="true" />
                      </button>
                    ) : null}
                  </div>
                  {/* The demo and the dose editor ease open under their option. */}
                  <Collapse show={preview === option.catalogue_id && Boolean(option.media)}>
                    {preview === option.catalogue_id && option.media ? (
                      <div className={styles.preview}>
                        <ExerciseDemo media={option.media} exerciseName={option.name} />
                      </div>
                    ) : null}
                  </Collapse>
                  <Collapse show={dosing === option.catalogue_id}>
                    {dosing === option.catalogue_id ? (
                      <ExercisePrescriptionEditor
                        initial={withPastDose({ ...option.suggested_dose, mode: option.dose_mode ?? "text" } as DoseDraft, past?.get(option.exercise_key))}
                        past={past?.get(option.exercise_key)}
                        busy={pending === option.catalogue_id}
                        action={mode === "swap" ? "Swap" : "Add"}
                        onSubmit={(dose) => void choose(option, dose)}
                      />
                    ) : null}
                  </Collapse>
                </li>
              ))}
            </ul>
          ) : null}
          {applyError ? <p className={styles.error} role="alert">{applyError}</p> : null}
          {data?.notice ? (
            <div className={styles.notice}>
              <p>{data.notice}</p>
              {data.alternatives.length === 0 ? (
                mode === "swap" ? <Link href="/plans/library" className={styles.secondary}>Search the library</Link> : null
              ) : null}
            </div>
          ) : null}
          {data?.more_available && !showAll ? (
            <button type="button" className={styles.more} onClick={() => setShowAll(true)}>
              See all {mode === "swap" ? "alternatives" : "suggestions"}
            </button>
          ) : null}
        </div>
      </AutoHeight>
    </InjuryDetailSheet>
  );
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="11" cy="11" r="6.5" />
      <path d="m16 16 4.5 4.5" />
    </svg>
  );
}

function SwapIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M7 7h11l-3-3M17 17H6l3 3" />
    </svg>
  );
}

function RestoreIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 12a8 8 0 1 0 2.3-5.7M4 4v4h4" />
    </svg>
  );
}

function RemoveIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5 12h14" />
    </svg>
  );
}
