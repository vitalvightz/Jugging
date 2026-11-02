"use client";

import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { useAppSession } from "./auth-provider";
import { habitTrainingDay } from "@/lib/training-habits";
import { ApiError, editExternalTraining, getTrainingHistory, logExternalTraining, recordCompliance } from "@/lib/api";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import { prefersReducedMotion } from "@/lib/reduced-motion";
import { consentCopyForBand, hasHealthDataConsent } from "@/lib/compliance";
import { PRIVACY_HREF } from "@/lib/legal-documents";
import { formatAppDate } from "@/lib/date-format";
import {
  TRAINING_EFFORTS, canRepeatTraining, latestRepeatableTraining,
  type DistanceUnit, type TrainingActivity, type TrainingEffort, type TrainingHistorySession,
} from "@/lib/training-history";
import { DISTANCE_LIMITS, distanceUnitLabel, formatRunTime, parseRunTime } from "@/lib/run-time";
import {
  athleteSport, fitsPicks, sportLabel, TRAINING_SPORTS, trainingLabel, trainingPicks, type TrainingPick, type TrainingSessionType, type TrainingSport,
} from "@/lib/training-disciplines";
import styles from "./log-training.module.css";
import { TrainingActionContent } from "./training-action-tile";
import tileStyles from "./training-action-tile.module.css";
import { SparringLogPrompt, type SparringDraft } from "./today/sparring-log-prompt";
import { SessionTakeawayPrompt } from "./session-takeaway";
import { Collapse } from "./today/collapse";
import { ManualExerciseLogger } from "./manual-exercise-logger";
import { offerPushNotifications } from "@/lib/push-prompt";
import type { SparringIntensity } from "@/lib/types";

/** Matches sheet-out in log-training.module.css. */
const SHEET_EXIT_MS = 200;

const DURATION_PICKS = [30, 45, 60, 90];
/** Common race distances for roadwork, in km. */
const DISTANCE_PICKS = [{ label: "5k", km: 5 }, { label: "10k", km: 10 }, { label: "Half", km: 21.1 }];

const label = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);

// The manual effort is only a starting answer for the follow-up's intensity.
// It never stands in for head contact, rounds or permission to spar.
const SPARRING_INTENSITY_FROM_EFFORT: Record<TrainingEffort, SparringIntensity> = {
  easy: "light",
  moderate: "medium",
  hard: "hard",
};

export function manualSparringDraft(row: TrainingHistorySession): SparringDraft {
  return {
    source: "free", planId: null, sessionId: null, plannedIntensity: null,
    title: row.title, rounds: null, roundSeconds: null, externalSessionId: row.id,
    suggestedIntensity: row.effort ? SPARRING_INTENSITY_FROM_EFFORT[row.effort] : undefined,
  };
}

type Props = { accessToken: string | null; entry?: TrainingHistorySession; repeatEntry?: TrainingHistorySession; recent?: boolean; history?: TrainingHistorySession[]; quickSession?: { activity: TrainingActivity; sport?: TrainingSport | null; session_type?: TrainingSessionType | null; duration_minutes: number; title: string; effort?: TrainingEffort | null }; onSaved?: (row: TrainingHistorySession) => void; onSparringReported?: () => void; variant?: "compact" | "tile" | "primary" };

export function LogTraining(props: Props) {
  return props.accessToken ? <LogTrainingContent key={props.accessToken} {...props} accessToken={props.accessToken} /> : null;
}

function LogTrainingContent({ accessToken, entry, repeatEntry, recent = false, history, quickSession, onSaved, onSparringReported, variant = "compact" }: Props & { accessToken: string }) {
  const [open, setOpen] = useState(false);
  // The sheet slides away before it unmounts, rather than vanishing in a frame.
  const [closing, setClosing] = useState(false);
  const closeTimer = useRef<number | null>(null);
  useEffect(() => () => {
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
  }, []);
  const editing = entry?.id;
  const [lastSession, setLastSession] = useState<TrainingHistorySession>();
  const repeatSource = repeatEntry && canRepeatTraining(repeatEntry) ? repeatEntry : recent ? history ? latestRepeatableTraining(history) : lastSession : undefined;
  const [repeating, setRepeating] = useState(false);
  const { me, replaceMe } = useAppSession();
  const consentCopy = consentCopyForBand(me?.profile.age_band);
  const [serverConsentRequired, setServerConsentRequired] = useState(false);
  const requiresConsent = !me || (me.profile.role === "athlete" && !hasHealthDataConsent(me)) || serverConsentRequired;
  const [now, setNow] = useState(() => new Date());
  const today = habitTrainingDay(now, me?.profile.athlete_timezone);
  const [sourceLabel, setSourceLabel] = useState("");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [customDuration, setCustomDuration] = useState(false);
  const durationInput = useRef<HTMLInputElement>(null);
  const saving = useRef(false);

  useEffect(() => {
    if (!recent || history) return;
    let cancelled = false;
    void getTrainingHistory(accessToken).then(({ sessions }) => {
      if (!cancelled) {
        const latest = latestRepeatableTraining(sessions);
        setLastSession(current => current ?? latest);
      }
    }).catch(() => { /* The simple action remains available if history cannot load. */ });
    return () => { cancelled = true; };
  }, [accessToken, recent, history]);
  const [activity, setActivity] = useState<TrainingActivity | "">("");
  // Optional detail from the athlete's sport; activity always follows from the pair.
  const [sport, setSport] = useState<TrainingSport | null>(null);
  const [sessionType, setSessionType] = useState<TrainingSessionType | null>(null);
  // Whose session types the Activity grid shows; fixed when the sheet opens.
  const mySport = athleteSport(me?.profile.technical_style);
  const [gridSport, setGridSport] = useState<TrainingSport | null>(null);
  // A per-session switch; the saved profile sport never changes here.
  const [sportMenu, setSportMenu] = useState(false);
  const [duration, setDuration] = useState("");
  // Optional roadwork detail: distance and an exact time ("24:35").
  const [distance, setDistance] = useState("");
  const [distanceUnit, setDistanceUnit] = useState<DistanceUnit>("km");
  const [customDistance, setCustomDistance] = useState(false);
  const [runTime, setRunTime] = useState("");
  const [effort, setEffort] = useState<TrainingEffort | "">("");
  const [trainingDay, setTrainingDay] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [saved, setSaved] = useState<string | null>(null);
  // A newly saved sparring session offers the shared post-sparring follow-up.
  const [sparringDraft, setSparringDraft] = useState<SparringDraft | null>(null);
  // Any other new session offers an optional takeaway in place of the saved line.
  const [takeawayRow, setTakeawayRow] = useState<TrainingHistorySession | null>(null);
  // ...and, beside it, optional exercises. A repeated session may reuse its source's names.
  const [exercisesOpen, setExercisesOpen] = useState(false);
  const [exerciseCount, setExerciseCount] = useState(0);
  const [exerciseReference, setExerciseReference] = useState<TrainingHistorySession | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const requestId = useRef<string | null>(null);
  const currentToken = useRef<string | null>(accessToken);
  const fieldId = useId();

  useEffect(() => {
    currentToken.current = accessToken;
    return () => { currentToken.current = null; };
  }, [accessToken]);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const release = lockOverlayScroll();
    dialog.current?.showModal();
    dialog.current?.querySelector<HTMLButtonElement>(repeating ? '[data-effort]' : '[data-activity][aria-pressed="true"], [data-activity]')?.focus();
    const update = () => {
      setNow(new Date());
      const viewport = window.visualViewport;
      dialog.current?.style.setProperty("--sheet-height", `${viewport?.height ?? window.innerHeight}px`);
      dialog.current?.style.setProperty("--sheet-bottom", `${viewport ? Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop) : 0}px`);
    };
    update();
    const timer = window.setInterval(update, 60_000);
    window.addEventListener("focus", update);
    window.visualViewport?.addEventListener("resize", update);
    window.visualViewport?.addEventListener("scroll", update);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", update);
      window.visualViewport?.removeEventListener("resize", update);
      window.visualViewport?.removeEventListener("scroll", update);
      release(); previous?.focus();
    };
  }, [open, repeating, fieldId]);

  useEffect(() => {
    if (!saved) return;
    const timer = setTimeout(() => setSaved(null), 5000);
    return () => clearTimeout(timer);
  }, [saved]);

  useEffect(() => {
    if (open) dialog.current?.querySelector<HTMLButtonElement>(requiresConsent ? "[data-consent-action]" : repeating ? "[data-effort]" : "[data-activity]")?.focus();
  }, [open, requiresConsent, repeating]);

  async function grantConsent() {
    if (!me || saving.current) return;
    const token = accessToken;
    saving.current = true; setBusy(true); setError(null);
    try {
      const next = await recordCompliance(token, { health_data_consent: true });
      if (currentToken.current !== token) return;
      if (!hasHealthDataConsent(next)) throw new Error("Consent was not recorded.");
      replaceMe(next);
      setServerConsentRequired(false);
    } catch {
      if (currentToken.current === token) setError("Your consent could not be saved. Try again.");
    } finally { saving.current = false; if (currentToken.current === token) setBusy(false); }
  }

  function closeSheet() {
    if (prefersReducedMotion()) {
      setOpen(false);
      return;
    }
    setClosing(true);
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null;
      setClosing(false);
      setOpen(false);
    }, SHEET_EXIT_MS);
  }

  function startEdit(row?: Partial<TrainingHistorySession>, repeat = false, source = "From previous session") {
    setServerConsentRequired(false);
    setRepeating(repeat);
    setSourceLabel(source);
    setDetailsOpen(false);
    setSportMenu(false);
    setCustomDuration(row?.duration_minutes != null && !DURATION_PICKS.includes(row.duration_minutes));
    // Records keep exactly what was stored until the athlete changes it. An
    // older Boxing or Sparring record without a sport opens in the original grid.
    setActivity(row?.activity ?? "");
    setSport(row?.sport ?? null);
    setSessionType(row?.sport ? row.session_type ?? null : null);
    setGridSport(row?.sport ?? (row?.activity && !fitsPicks(row, mySport) ? null : mySport));
    setDuration(row?.duration_minutes?.toString() ?? "");
    const storedDistance = row?.distance_value != null && row.distance_unit ? row.distance_value : null;
    setDistance(storedDistance != null ? String(storedDistance) : "");
    setDistanceUnit(row?.distance_unit ?? "km");
    setCustomDistance(storedDistance != null && !(row?.distance_unit === "km" && DISTANCE_PICKS.some(pick => pick.km === storedDistance)));
    // A repeated run keeps its distance; the time is today's to enter.
    setRunTime(!repeat && row?.duration_seconds ? formatRunTime(row.duration_seconds) : "");
    setEffort(repeat ? "" : row?.effort ?? "");
    setTrainingDay("");
    requestId.current = null;
    setError(null);
    setSaved(null);
    setTakeawayRow(null);
    setExercisesOpen(false);
    setExerciseCount(0);
    setExerciseReference(repeat && row?.id && row.exercise_count ? row as TrainingHistorySession : null);
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    closeTimer.current = null;
    setClosing(false);
    setOpen(true);
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!accessToken || saving.current) return;
    if (requiresConsent) return;
    if (!activity || !effort) {
      setError("Select an activity and effort.");
      dialog.current?.querySelector<HTMLButtonElement>(!activity ? '[data-activity]' : '[data-effort]')?.focus();
      return;
    }
    const isRun = activity === "roadwork";
    const runSeconds = isRun && runTime.trim() ? parseRunTime(runTime) : null;
    if (isRun && runTime.trim() && runSeconds === null) {
      setError("Enter the time as mm:ss or h:mm:ss, up to 10 hours."); return;
    }
    const distanceValue = isRun && distance.trim() ? Number(distance) : null;
    if (distanceValue !== null && !(distanceValue > 0 && distanceValue <= DISTANCE_LIMITS[distanceUnit])) {
      setError(`Enter a distance above 0 and up to ${DISTANCE_LIMITS[distanceUnit]} ${distanceUnitLabel(distanceUnit)}.`); return;
    }
    // An exact time sets the minutes, so workload always reads whole minutes.
    const minutes = runSeconds !== null ? Math.ceil(runSeconds / 60) : Number(duration);
    if (!Number.isInteger(minutes) || minutes < 1 || minutes > 600) {
      setError("Enter a whole number from 1 to 600 minutes."); return;
    }
    const token = accessToken;
    saving.current = true;
    setBusy(true);
    setError(null);
    try {
      const body = { activity, duration_minutes: minutes, effort, ...(sport ? { sport } : {}), ...(sport && sessionType ? { session_type: sessionType } : {}),
        ...(distanceValue !== null ? { distance_value: distanceValue, distance_unit: distanceUnit } : {}),
        ...(runSeconds !== null ? { duration_seconds: runSeconds } : {}) };
      const row = editing
        ? await editExternalTraining(token, editing, body)
        : await logExternalTraining(token, { ...body, ...(trainingDay ? { training_day: trainingDay } : {}), client_request_id: requestId.current ??= crypto.randomUUID() });
      if (currentToken.current !== token) return;
      if (recent) setLastSession(current => latestRepeatableTraining([row, ...(current ? [current] : [])]));
      onSaved?.(row);
      requestId.current = null;
      closeSheet();
      if (!editing) offerPushNotifications(token);
      if (!entry && !repeatEntry && row.source === "external" && row.activity !== "sparring") setTakeawayRow(row);
      else setSaved(`${row.title} logged successfully. Saved to Training History.`);
      if (!editing && row.source === "external" && row.activity === "sparring") setSparringDraft(manualSparringDraft(row));
    } catch (reason) {
      if (currentToken.current === token && reason instanceof ApiError && reason.code === "health_data_consent_required") {
        setServerConsentRequired(true); setError(null);
      } else if (currentToken.current === token) setError(reason instanceof ApiError && reason.status === 401 ? "Your session expired. Sign in again to log training." : reason instanceof Error ? reason.message.replace("The plan service", "The training service") : "Unable to save session. Try again.");
    } finally { saving.current = false; if (currentToken.current === token) setBusy(false); }
  }

  function choose(pick: TrainingPick) {
    if (pick.activity !== activity || pick.sport !== sport || pick.session_type !== sessionType) requestId.current = null;
    setActivity(pick.activity); setSport(pick.sport); setSessionType(pick.session_type);
  }
  function switchSport(next: TrainingSport | null) {
    setSportMenu(false);
    if (next === gridSport) return;
    setGridSport(next);
    // A session type of the old sport no longer fits; strength, roadwork and the rest carry over.
    if (sport) { setActivity(""); setSport(null); setSessionType(null); requestId.current = null; }
    requestAnimationFrame(() => dialog.current?.querySelector<HTMLButtonElement>("[data-activity]")?.focus());
  }
  const activityAndDuration = <>
    <fieldset className={styles.fieldset} disabled={busy} aria-label="Activity">
      <legend className={styles.activityLegend}>Activity
        <button type="button" className={styles.sportSwitch} aria-expanded={sportMenu} aria-controls={`${fieldId}-sports`}
          aria-label={`Change sport for this session. Currently ${gridSport ? sportLabel(gridSport) : "no sport"}`}
          onClick={() => setSportMenu(value => !value)}>{gridSport ? sportLabel(gridSport) : "No sport"} · Change</button>
      </legend>
      {sportMenu ? <div className={styles.sportMenu} id={`${fieldId}-sports`} role="group" aria-label="Sport for this session">
        {[...TRAINING_SPORTS, null].map(value => <button key={value ?? "none"} type="button" aria-pressed={gridSport === value}
          onClick={() => switchSport(value)}>{value ? sportLabel(value) : "No sport"}</button>)}
      </div> : null}
      <div className={styles.activityGrid}>
        {trainingPicks(gridSport).map(pick => <button key={pick.label} type="button" data-activity
          aria-pressed={activity === pick.activity && sport === pick.sport && sessionType === pick.session_type}
          onClick={() => choose(pick)}>{pick.label}</button>)}
      </div>
    </fieldset>
    {activity === "roadwork" ? <fieldset className={styles.fieldset} disabled={busy} aria-label="Run distance and time">
      <legend>Distance <span className={styles.unit}>Optional</span></legend>
      <div className={styles.durationGrid}>
        {DISTANCE_PICKS.map(pick => <button key={pick.label} type="button" aria-pressed={!customDistance && distanceUnit === "km" && distance === String(pick.km)}
          onClick={() => { setCustomDistance(false); setDistanceUnit("km"); setDistance(current => current === String(pick.km) && !customDistance ? "" : String(pick.km)); requestId.current = null; }}>{pick.label}</button>)}
        <button type="button" aria-pressed={customDistance} aria-expanded={customDistance} aria-controls={`${fieldId}-distance`}
          onClick={() => { setCustomDistance(true); requestId.current = null; }}>Custom</button>
      </div>
      {customDistance ? <div className={styles.runCustom}>
        <label className={styles.customDuration}>Distance ({distanceUnitLabel(distanceUnit)})
          <input id={`${fieldId}-distance`} type="number" inputMode="decimal" min={0.01} max={DISTANCE_LIMITS[distanceUnit]} step={0.01} value={distance}
            onChange={event => { setDistance(event.target.value); requestId.current = null; }} />
        </label>
        <div className={styles.unitToggle} role="group" aria-label="Distance unit">
          {(["km", "miles"] as const).map(unit => <button key={unit} type="button" aria-pressed={distanceUnit === unit}
            onClick={() => { setDistanceUnit(unit); requestId.current = null; }}>{distanceUnitLabel(unit)}</button>)}
        </div>
      </div> : null}
      <label className={styles.customDuration}>Time (mm:ss or h:mm:ss, optional)
        <input type="text" inputMode="decimal" placeholder="24:35" autoComplete="off" value={runTime}
          onChange={event => { setRunTime(event.target.value); requestId.current = null; }} />
      </label>
    </fieldset> : null}
    {activity === "roadwork" && parseRunTime(runTime) !== null ? <p className={styles.runMinutes}>Duration: {Math.ceil(parseRunTime(runTime)! / 60)} min from your time</p> :
    <fieldset className={styles.fieldset} disabled={busy} aria-label="Duration">
      <legend>Duration <span className={styles.unit}>min</span></legend>
      <div className={styles.durationGrid}>
        {DURATION_PICKS.map(value => <button key={value} type="button" aria-pressed={!customDuration && duration === String(value)}
          onClick={() => { setDuration(String(value)); setCustomDuration(false); if (Number(duration) !== value) requestId.current = null; }}>{value}</button>)}
        <button type="button" aria-pressed={customDuration} aria-expanded={customDuration} aria-controls={`${fieldId}-duration`}
          onClick={() => { setCustomDuration(true); requestAnimationFrame(() => durationInput.current?.focus()); }}>Custom</button>
      </div>
      {customDuration ? <label className={styles.customDuration}>Duration (minutes)
        <input id={`${fieldId}-duration`} ref={durationInput} type="number" inputMode="numeric" required min={1} max={600} step={1} value={duration}
          onChange={event => { setDuration(event.target.value); requestId.current = null; }} />
      </label> : null}
    </fieldset>}
  </>;
  const dateChoice = !editing ? <details className={styles.dateChoice}>
    <summary>Training date · {formatAppDate(trainingDay || today)}</summary>
    <label className={styles.customDuration}>Training date (optional)
      <input type="date" max={today} value={trainingDay} disabled={busy}
        onChange={event => { setTrainingDay(event.target.value); requestId.current = null; }} />
    </label>
    {trainingDay ? <button className={styles.resetDate} type="button" disabled={busy}
      onClick={() => { setTrainingDay(""); requestId.current = null; }}>Use current training day</button> : null}
  </details> : null;

  return (
    <div className={entry || repeatEntry ? styles.editControl : styles.training} role={entry || repeatEntry ? undefined : "region"} aria-label={entry ? `Manage ${entry.title}` : "Log session"}>
      {/* Appears once a session is logged (or history arrives): eased, not popped. */}
      <Collapse show={Boolean(quickSession || repeatSource)}>
      {quickSession ? <button className={styles.actionRow} type="button" onClick={() => startEdit(quickSession, true, quickSession.title)} aria-haspopup="dialog" aria-label={quickSession.title} aria-describedby={`${fieldId}-quick`}>
        <span><strong>{quickSession.title}</strong><span className="muted" id={`${fieldId}-quick`}>{trainingLabel(quickSession)} · {quickSession.duration_minutes} min</span></span>
        <svg className={styles.chevron} viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m9 5 7 7-7 7" /></svg>
      </button> : repeatSource ? <button className={repeatEntry ? styles.repeatButton : styles.actionRow} type="button" onClick={() => startEdit(repeatSource, true)} aria-haspopup="dialog" aria-label={repeatEntry ? `Repeat ${repeatSource.title}` : "Repeat last session"} aria-describedby={!repeatEntry ? `${fieldId}-recent` : undefined}>
        {repeatEntry ? "Repeat" : <><span><strong>Repeat last session</strong><span className="muted" id={`${fieldId}-recent`}>{trainingLabel(repeatSource)} · {repeatSource.duration_minutes} min</span></span><svg className={styles.chevron} viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m9 5 7 7-7 7" /></svg></>}
      </button> : null}
      </Collapse>
      {repeatEntry ? null : entry ? <button className={styles.editButton} type="button" onClick={() => startEdit(entry)} aria-haspopup="dialog" aria-label={`Edit ${entry.title}`} title="Edit session">
        <svg className="ex-log-edit-icon" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false"><path d="M4 20h4L18.5 9.5a2.12 2.12 0 0 0-3-3L5 17v3Z" /><path d="m14.5 7.5 3 3" /></svg>
      </button> :
        <button className={variant === "primary" ? `cta ${styles.primary}` : variant === "tile" ? `${tileStyles.tile} ${tileStyles.log}` : styles.actionRow} type="button" onClick={() => startEdit()} aria-haspopup="dialog" aria-label="Log session">
          {variant === "primary" ? <><span aria-hidden="true">+</span> Log session</> : variant === "tile" ? <TrainingActionContent kind="log" /> : <><span><strong>{recent && (repeatSource || quickSession) ? "Log another session" : "Log session"}</strong><span className="muted">{mySport ? `${sportLabel(mySport)}, strength, conditioning and more.` : "Sparring, boxing, roadwork, strength and more."}</span></span><span aria-hidden="true">+</span></>}
        </button>}
      {open ? (
        <dialog ref={dialog} className={styles.dialog} aria-label={editing ? "Edit session" : "Log session"} data-closing={closing || undefined} onCancel={event => { event.preventDefault(); if (!busy) closeSheet(); }}>
        <header className={styles.header}>
          <div><h2>{editing ? "Edit session" : repeating && activity ? `Log ${trainingLabel({ activity, sport, session_type: sessionType })}` : "Log session"}</h2>
            {repeating ? <p className={styles.source}>{duration} min · {sourceLabel}</p> : null}
          </div>
          <button className={styles.close} type="button" aria-label="Close training log" disabled={busy} onClick={closeSheet}>×</button>
        </header>
        {requiresConsent ? <div className={styles.consent}>
          <p>Training logs include health information, like effort and sparring.</p>
          <p id={`${fieldId}-consent`}>{consentCopy.signupHealthConsentLabel}</p>
          <p className="muted">Optional. You can withdraw in Settings. <Link href={PRIVACY_HREF}>Privacy Notice</Link></p>
          {error ? <p role="alert">{error}</p> : null}
          <button type="button" className="cta" data-consent-action aria-describedby={`${fieldId}-consent`} disabled={busy || !me} onClick={() => void grantConsent()}>{busy ? "Saving…" : "Allow health-data use and continue"}</button>
          <button type="button" className="ghost-button" disabled={busy} onClick={closeSheet}>Not now</button>
        </div> : <form className={styles.form} onSubmit={save} aria-label={editing ? "Edit session" : "Log session"}>
          <div className={styles.body}>
            {!repeating ? activityAndDuration : null}
            <fieldset className={styles.fieldset} disabled={busy} aria-label="Effort">
              <legend>{repeating ? "How hard was it today?" : "Effort"} <span className={styles.unit}>Required</span></legend>
              <div className={styles.effortGrid}>
                {TRAINING_EFFORTS.map(value => <button key={value} type="button" data-effort aria-pressed={effort === value}
                  onClick={() => { setEffort(value); if (effort !== value) requestId.current = null; }}>{label(value)}</button>)}
              </div>
            </fieldset>
            {repeating ? <>
              <button className={styles.detailsToggle} type="button" aria-expanded={detailsOpen} aria-controls={`${fieldId}-details`} disabled={busy}
                onClick={() => setDetailsOpen(value => !value)}>Edit details <span aria-hidden="true">{detailsOpen ? "−" : "+"}</span></button>
              <div id={`${fieldId}-details`} className={styles.expandedDetails} hidden={!detailsOpen}>
                {activityAndDuration}{dateChoice}
              </div>
            </> : dateChoice}
          </div>
          <footer className={styles.actions}>
            {error ? <p className={styles.error} role="alert">{error}</p> : null}
            <button className="cta" type="submit" disabled={busy}>{busy ? "Saving…" : "Save Session"}</button>
          </footer>
        </form>}
        </dialog>
      ) : null}
      {saved && !entry && !repeatEntry ? <p className="muted" role="status">{saved}</p> : null}
      <Collapse show={Boolean(takeawayRow)}>
        {takeawayRow ? <SessionTakeawayPrompt key={takeawayRow.id} token={accessToken} row={takeawayRow} sheet={variant === "tile"}
          onSaved={() => setTakeawayRow(null)} onDismiss={() => setTakeawayRow(null)}
          onAddExercises={() => setExercisesOpen(true)} exerciseCount={exerciseCount} suspended={exercisesOpen} /> : null}
      </Collapse>
      {takeawayRow && exercisesOpen ? <ManualExerciseLogger token={accessToken} session={takeawayRow} reference={exerciseReference}
        onClose={entries => { setExercisesOpen(false); if (entries) setExerciseCount(entries.length); }} /> : null}
      {sparringDraft ? <SparringLogPrompt key={sparringDraft.externalSessionId} token={accessToken} draft={sparringDraft}
        onSaved={onSparringReported} onDismiss={() => setSparringDraft(null)} /> : null}
    </div>
  );
}
