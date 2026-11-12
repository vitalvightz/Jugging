"use client";

import { useEffect, useId, useRef, useState, type ReactNode, type CSSProperties } from "react";
import { getSessionDetails } from "@/lib/api";
import type { SessionDetailBlock, SessionDetailRun, SessionDetails as Details } from "@/lib/api-schema.generated";
import { formatAppDate } from "@/lib/date-format";
import { sessionStatusLabel } from "@/lib/history";
import { EXERCISE_LOG_REASON_LABELS } from "@/lib/exercise-log";
import type { ExerciseLogReason } from "@/lib/types";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import { comparisonRows, detailMeasurements, matchesPrescription, splitLoad, type ComparisonRow } from "@/lib/session-details";
import { recordStatus, StatusIcon, TileIcon, type RecordStatus } from "./record-icons";
import { NO_MEASUREMENTS, summarise } from "@/lib/manual-exercises";
import { sessionTypes, sportLabel, trainingTitle } from "@/lib/training-disciplines";
import type { TrainingHistorySession } from "@/lib/training-history";
import styles from "./session-details.module.css";
import { countOnlySetSummary } from "@/lib/set-entries";
import { SetResults } from "./set-rows";
import type { ExerciseSetResult } from "@/lib/types";
import { useSheetExit } from "@/lib/use-sheet-exit";
import { Skeleton } from "./skeleton";
import { AutoHeight } from "./today/auto-height";
import { formatRunTime } from "@/lib/run-time";
import { describeDistanceDelta, describePaceDelta, fromKm, fromKmPace, isRun, RUN_TYPE_LABELS, runInsights } from "@/lib/run-insights";
import { RunBarChart, RunTrendChart } from "./run-trend-chart";
import progress from "./training-progress.module.css";
import summary from "./training-summary.module.css";

/** The session's own status as an icon tone. */
function sessionTone(status: string): RecordStatus {
  return status === "done" ? "done" : status === "modified" ? "partial" : status === "skipped" ? "missed" : "unknown";
}

export type SessionDetailsCache = Map<string, Promise<Details>>;
const OUTCOMES = { as_prescribed: "Completed as prescribed", modified: "Modified", skipped: "Skipped",
  done_as_shown: "Done as shown", changed: "Changed", stopped: "Stopped" };

/** Short status for a row; the full outcome stays in the expanded record. */
const SHORT_OUTCOMES = { as_prescribed: "Done", modified: "Modified", skipped: "Skipped",
  done_as_shown: "Done", changed: "Changed", stopped: "Stopped" };
const QUIET_STATUSES = new Set(["as_prescribed", "done_as_shown"]);
const hasWeight = (label: string, value?: string) => label === "Load" && !!value && /\d/.test(value);

/** Blocks with something to see start open: a lone block, or any change, skip or unknown outcome. */
export function opensByDefault(block: SessionDetailBlock, count: number): boolean {
  return count === 1 || (block.source !== "manual" && !QUIET_STATUSES.has(block.status ?? ""));
}

/** What a row shows: the weight on its own and the rest of the dose, from what was done or else what was planned. */
export function blockSummary(block: SessionDetailBlock): { load: string | null; dose: string } {
  return detailMeasurements(block.actual).length ? splitLoad(block.actual) : splitLoad(block.prescribed);
}

/** Done beside planned, one measurement per row; the weight is gold, changed values stand out. */
function Comparison({ rows, label, title, bar = false }: { rows: ComparisonRow[]; label: string; title: string; bar?: boolean }) {
  const paired = rows.filter(row => row.did && row.plan);
  const matched = paired.filter(row => row.same).length;
  return <section className={styles.compare} aria-label={label}>
    <div className={styles.compareHead}><h5>{title}</h5>
      {bar && paired.length ? <span className={styles.bar} data-full={matched === paired.length} aria-hidden="true"><span style={{ width: `${Math.max(8, matched / paired.length * 100)}%` }} /></span> : null}</div>
    <dl className={styles.compareRows}>{rows.map(row => <div key={row.label} data-changed={!!(row.did && row.plan && !row.same) || undefined}>
      <dt>{row.label}</dt>
      <dd>{row.did ? <span className={styles.did} data-weight={hasWeight(row.label, row.did) || undefined}>{row.did}</span> : null}
        {row.did && row.plan ? <span className={styles.plan}> / {row.plan}</span> : row.plan ? <span data-weight={hasWeight(row.label, row.plan) || undefined}>{row.plan}</span> : null}</dd>
    </div>)}</dl>
  </section>;
}

function TrainingBlockBody({ block, context }: { block: SessionDetailBlock; context: string }) {
  const entries = Array.isArray(block.actual.set_entries) && block.actual.set_entries.length ? block.actual.set_entries as ExerciseSetResult[] : null;
  const sets = entries ? <SetResults entries={entries} completed={typeof block.actual.sets === "number" ? block.actual.sets : undefined} /> : null;
  const countOnly = countOnlySetSummary(block.actual);
  const notes = <>{block.reason ? <p className={styles.note}>{EXERCISE_LOG_REASON_LABELS[block.reason as ExerciseLogReason] || block.reason}</p> : null}
    {block.notes ? <blockquote className={styles.quote}>{block.notes}</blockquote> : null}</>;
  if (block.source === "manual") return <>
    <p className={styles.outcome}>Logged training · added by you</p>
    {countOnly ? <p className={styles.quiet}>{countOnly}</p> : detailMeasurements(block.actual).length
      ? <Comparison rows={comparisonRows({}, block.actual)} label={`Recorded: ${context}`} title="Recorded" /> : <p className={styles.quiet}>No measurements recorded.</p>}
    {countOnly ? null : sets}{notes}
  </>;
  const plan = comparisonRows(block.prescribed, {});
  if (block.kind === "guidance") return <><p className={styles.outcome}>Guidance · {block.status ? OUTCOMES[block.status] : "Completion not recorded"}</p>
    <Comparison rows={plan} label={`Prescribed: ${context}`} title="Saved guidance" />{notes}</>;
  if (block.kind !== "exercise") {
    const quantified = block.completed_dose?.completion_state === "quantified";
    return <>{quantified ? <Comparison rows={comparisonRows(block.prescribed, block.completed_dose!)} label={`Performance: ${context}`} title="Completed dose vs prescribed" bar />
      : <><Comparison rows={plan} label={`Prescribed: ${context}`} title="Prescribed" />
        <p className={styles.quiet}>{block.status === "skipped" ? "Skipped. No completed dose recorded." : "Completed amount unknown."}</p></>}
      {notes}
      {block.responses.map((response, i) => <div key={i} className={styles.response} aria-label="Recorded rehab response">
        <span>Recorded response for the rehab work</span>
        {response.during_response !== "not_reported" ? <p>During: {response.during_response.replaceAll("_", " ")}</p> : null}
        {response.stopped_due_to_symptoms != null ? <p>{response.stopped_due_to_symptoms ? "Reduced or stopped because of symptoms." : "No reduction or stop reported."}</p> : null}
        {response.pain_during != null ? <p>Pain during: {typeof response.pain_during === "number" ? `${response.pain_during}/10` : "Not sure"}</p> : null}
        {response.pain_immediate_after != null ? <p>Pain after: {typeof response.pain_immediate_after === "number" ? `${response.pain_immediate_after}/10` : "Not sure"}</p> : null}
        {response.next_day_response !== "not_yet_known" ? <p>Next day: {response.next_day_response.replaceAll("_", " ")}</p> : null}
        {response.worsening_reported != null ? <p>Worsening reported: {response.worsening_reported ? "Yes" : "No"}</p> : null}
      </div>)}
    </>;
  }
  const actuals = detailMeasurements(block.actual).length > 0;
  // Confirmed or repeated values add nothing beside the prescription.
  const same = block.status === "as_prescribed" && (!actuals || matchesPrescription(block.prescribed, block.actual));
  if (same || !actuals) return <>
    {same ? <p className={styles.confirmed}><StatusIcon status="done" /> Completed as prescribed{actuals ? " · reported values match" : ""}</p> : null}
    <Comparison rows={plan} label={`Prescribed: ${context}`} title="Prescription" />
    {same ? null : <p className={styles.quiet}>{block.status === "skipped" ? "Skipped. No performance values logged." : "Performed values unknown."}</p>}
    {notes}
  </>;
  const rows = comparisonRows(block.prescribed, block.actual);
  return <>
    {countOnly ? <><Comparison rows={plan} label={`Prescribed: ${context}`} title="Prescription" /><p className={styles.quiet}>{countOnly}</p></>
      : <Comparison rows={rows} label={`Performance: ${context}`} title="Reported vs prescribed" bar />}
    {countOnly ? null : sets}
    {!countOnly && rows.some(row => row.plan && !row.did) ? <p className={styles.quiet}>{block.status === "as_prescribed" ? "Other prescribed values confirmed as completed." : "Other performed values are unknown."}</p> : null}
    {notes}
  </>;
}

/** A collapsible record card: name, dose and the weight in gold; the full record expands on request. */
export function TrainingBlock({ block, index, heading, recordId, open, onToggle, numbered = true }: {
  block: SessionDetailBlock; index: number; heading?: string; recordId?: string; open: boolean; onToggle: () => void; numbered?: boolean;
}) {
  const body = useId();
  const context = heading ? `${block.name} · ${heading} · Record ${index + 1}` : block.name;
  const label = block.source === "manual" ? "Logged" : block.status ? SHORT_OUTCOMES[block.status] : "Not recorded";
  const { load, dose } = blockSummary(block);
  return <li className={styles.block} data-performance={recordId} data-open={open} tabIndex={recordId ? -1 : undefined}>
    <div className={styles.blockHeading}>{numbered ? <span className={styles.number}>{String(index + 1).padStart(2, "0")}</span> : null}
      <div className={styles.blockTitle}><h4><button type="button" className={styles.blockToggle} aria-expanded={open} aria-controls={body} onClick={onToggle}>{heading || block.name}</button></h4>
        {dose ? <p className={styles.blockSummary}>{dose}</p> : null}</div>
      <div className={styles.blockSide}>{load ? <strong className={styles.weight}>{load}</strong> : null}
        <span className={styles.status} data-status={recordStatus(block.status, block.source === "manual")}><StatusIcon status={recordStatus(block.status, block.source === "manual")} />{label}</span></div>
      <span className={styles.chevron} aria-hidden="true">›</span></div>
    {open ? <div id={body} className={styles.blockBody}><TrainingBlockBody block={block} context={context} /></div> : null}
  </li>;
}

/** Native modal supplies focus containment, inert background and Escape handling. */
/** The rough shape of a session record: its facts, then its exercises. */
function SessionRecordSkeleton() {
  return <div className={styles.recordSkeleton}>
    <p role="status" className="sr-only">Loading session details…</p>
    <div className={styles.recordSkeletonFacts}>
      <Skeleton height={62} /><Skeleton height={62} /><Skeleton height={62} />
    </div>
    <Skeleton variant="text" width="52%" height={18} />
    <Skeleton height={64} /><Skeleton height={64} />
  </div>;
}

/** RPE when the athlete rated the session, otherwise the logged effort ("Easy"). */
function sessionEffort(record: { session_rpe?: number | null; effort?: string | null }): string | null {
  if (record.session_rpe != null) return `RPE ${record.session_rpe}/10`;
  if (!record.effort) return null;
  return record.effort.charAt(0).toUpperCase() + record.effort.slice(1);
}

type RunMetric = "distance" | "pace";
const DISTANCE_STEPS = [0.5, 1, 2, 5, 10, 20, 50];
const PACE_STEPS = [5, 10, 15, 30, 60, 120, 300, 600];

/**
 * A run reads like a run, laid out like the weight tracker: a summary card
 * with distance as the headline and time, pace and speed beneath, then a
 * trend card to compare it with the athlete's other logged runs, and its week.
 */
function RunRecord({ run, history, onEditRun }: {
  run: TrainingHistorySession;
  /** Every run the athlete logged through this run's week, from the server; null when it was not sent. */
  history: readonly SessionDetailRun[] | null;
  onEditRun?: () => void;
}) {
  // Bests and comparisons come only from the complete list the server sends,
  // never from a partly loaded History page.
  const runs: TrainingHistorySession[] = (history ?? []).map(item => ({ ...item, source: "external", activity: "roadwork", status: "done",
    title: "Running", plan_id: null, effort: null }));
  const unit = run.distance_unit ?? "km";
  const unitShort = unit === "km" ? "km" : "mi";
  const distance = run.distance_value && run.distance_unit ? run.distance_value : null;
  const pace = distance && run.duration_seconds ? formatRunTime(run.duration_seconds / distance) : null;
  const speed = distance && run.duration_seconds ? (distance / (run.duration_seconds / 3600)).toFixed(1) : null;
  const time = run.duration_seconds ? formatRunTime(run.duration_seconds) : null;
  const insights = runInsights(run, runs);
  const effort = sessionEffort(run);
  const missing = [!distance && "distance", !run.duration_seconds && "exact time"].filter(Boolean) as string[];
  // Units sit small beside the number so pace and speed fit three across a phone.
  const tiles: { label: string; value: string | null; unit?: string }[] = distance ? [
    { label: "Time", value: time ?? (run.duration_minutes != null ? String(run.duration_minutes) : null), unit: time ? undefined : "min" },
    { label: "Pace", value: pace, unit: `/${unitShort}` },
    { label: "Speed", value: speed, unit: unit === "km" ? "km/h" : "mph" },
  ] : [
    { label: "Distance", value: null },
    { label: "Pace", value: null },
    { label: "Speed", value: null },
  ];

  const paced = insights.trend.filter(point => point.paceSecondsPerKm !== null);
  const [metric, setMetric] = useState<RunMetric>(insights.trend.length >= 2 || paced.length < 2 ? "distance" : "pace");
  const [view, setView] = useState<"bars" | "line">("bars");
  const points = (metric === "pace" ? paced : insights.trend).map(point => ({
    id: point.id, day: point.day, current: point.current,
    value: metric === "pace" ? fromKmPace(point.paceSecondsPerKm!, unit) : fromKm(point.km, unit),
  }));
  const currentIndex = points.findIndex(point => point.current);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const found = points.findIndex(point => point.id === selectedId);
  const selectedIndex = found >= 0 ? found : currentIndex >= 0 ? currentIndex : points.length - 1;
  const selected = points[selectedIndex];
  const current = currentIndex >= 0 ? points[currentIndex] : null;
  const tick = (value: number) => metric === "pace" ? formatRunTime(value) : `${Number(value.toFixed(1))}`;
  const formatValue = (value: number) => metric === "pace" ? `${formatRunTime(value)}/${unitShort}` : `${Number(value.toFixed(2))} ${unitShort}`;
  const change = !selected ? null : selected.current
    ? metric === "pace" ? insights.paceDelta && describePaceDelta(insights.paceDelta, unit) : insights.distanceDelta && describeDistanceDelta(insights.distanceDelta, unit)
    : current ? (() => {
      const diff = selected.value - current.value;
      if (metric === "pace") return Math.round(diff) === 0 ? "Same pace as this run" : `${formatRunTime(Math.abs(Math.round(diff)))}/${unitShort} ${diff < 0 ? "faster" : "slower"} than this run`;
      return Math.abs(diff) < 0.05 ? "Same distance as this run" : `${Number(Math.abs(diff).toFixed(2))} ${unitShort} ${diff > 0 ? "further" : "shorter"} than this run`;
    })() : null;
  const selectedRow = selected ? runs.find(row => row.id === selected.id) ?? (selected.id === run.id ? run : null) : null;

  return <div className={styles.runCards}>
    <section className={`history-summary ${styles.runCard}`} aria-label="Run summary">
      <div className={summary.head}>
        <p className="history-summary-label">{distance ? "Distance" : "Time"}</p>
        {onEditRun && !missing.length ? <button type="button" className={`${progress.textButton} ${styles.runEdit}`} aria-haspopup="dialog" onClick={onEditRun}>Edit run</button>
          : <span className={styles.runMeta}>Added by you</span>}
      </div>
      <div className={styles.runHeadline}>
        <strong>{distance ? Number(distance.toFixed(2)) : time ?? run.duration_minutes ?? "—"}</strong>
        <span>{distance ? unit === "km" ? "km" : "miles" : time ? "time" : "min"}<br />{[run.run_type ? RUN_TYPE_LABELS[run.run_type] : "Run", effort].filter(Boolean).join(" · ")}</span>
      </div>
      {insights.records.length ? <p className={styles.runRecords}>{insights.records.map(record => <span key={record} className={progress.bestPill}>★ {record}</span>)}</p> : null}
      <dl className={`history-summary-stats ${summary.stats} ${styles.runTiles}`} style={{ "--stat-count": 3 } as CSSProperties}>
        {tiles.map(tile => <div key={tile.label}><dt>{tile.label}</dt><dd>{tile.value ?? "—"}{tile.value && tile.unit ? <small>{tile.unit}</small> : null}</dd></div>)}
      </dl>
      {missing.length ? <div className={styles.runPrompt}>
        <p>Add your {missing.join(" and ")} to see pace, personal bests and trends.</p>
        {onEditRun ? <button type="button" className="secondary-button" aria-haspopup="dialog" onClick={onEditRun}>Add run details</button> : null}
      </div> : null}
    </section>

    {points.length >= 2 ? <section className={`history-summary ${styles.runCard}`} aria-label="Run trend">
      <div className={styles.runTrendHead}>
        <h3 className={styles.runSectionTitle}>Run trend</h3>
        <div className={`${summary.toggle} ${styles.runToggle}`} role="group" aria-label="Trend measure">
          {(["distance", "pace"] as const).map(value => <button key={value} type="button" aria-pressed={metric === value}
            disabled={value === "pace" && paced.length < 2}
            onClick={() => { setMetric(value); setSelectedId(null); }}>{value === "pace" ? "Pace" : "Distance"}</button>)}
        </div>
      </div>
      {selected ? <div className={styles.runReadout} aria-live="polite">
        <p><strong>{formatValue(selected.value)}</strong></p>
        {change ? <p className={styles.runChange}>{change}</p> : null}
        <p className={styles.runReadoutMeta}>{[formatAppDate(selected.day), selected.current ? "This run" : selectedRow?.run_type ? RUN_TYPE_LABELS[selectedRow.run_type] : null].filter(Boolean).join(" · ")}</p>
      </div> : null}
      {view === "bars" ? <RunBarChart points={points} label={metric === "pace" ? "Inspect pace of your recent runs" : "Inspect distance of your recent runs"}
        format={formatValue} short={tick} height={value => metric === "pace" ? 1 / value : value}
        selectedIndex={selectedIndex} onSelect={index => setSelectedId(points[index].id)} />
        : <RunTrendChart points={points} label={metric === "pace" ? "Inspect pace of your recent runs" : "Inspect distance of your recent runs"}
          steps={metric === "pace" ? PACE_STEPS : DISTANCE_STEPS} invert={metric === "pace"} format={formatValue} tick={tick}
          selectedIndex={selectedIndex} onSelect={index => setSelectedId(points[index].id)} />}
      <div className={styles.runChartFoot}>
        <p className={progress.note}>Last {points.length} runs{metric === "pace" ? ` with an exact time · ${view === "bars" ? "faster is taller" : "faster is higher"}` : ` · ${unitShort}`}</p>
        <div className={`${summary.toggle} ${styles.runToggle}`} role="group" aria-label="Chart view">
          {(["bars", "line"] as const).map(value => <button key={value} type="button" aria-pressed={view === value}
            onClick={() => setView(value)}>{value === "bars" ? "Bars" : "Line"}</button>)}
        </div>
      </div>
    </section> : null}

    {insights.week.runs ? <section className={`history-summary ${styles.runCard}`} aria-label="Week of this run">
      <p className="history-summary-label">Week of this run</p>
      <dl className={`history-summary-stats ${summary.stats} ${styles.runTiles}`} style={{ "--stat-count": 3 } as CSSProperties}>
        <div><dt>Runs</dt><dd>{insights.week.runs}</dd></div>
        <div><dt>Distance</dt><dd>{insights.week.distance > 0 ? <>{Number(insights.week.distance.toFixed(1))}<small>{unitShort}</small></> : "—"}</dd></div>
        <div><dt>Time</dt><dd>{insights.week.timed > 0 ? formatRunTime(insights.week.timed) : "—"}</dd></div>
      </dl>
      <details className={progress.about}>
        <summary>About these numbers</summary>
        <p>Pace and speed use your exact time, so a run logged in whole minutes has no pace.</p>
        <p>Comparisons and bests count every run you have logged up to this one, so an old run keeps the bests it set at the time.</p>
      </details>
    </section> : null}
  </div>;
}

export function SessionDetails({ row, token, cache, onDismiss, restoreFocus = true, onEditExercises, onEditRun, actions }: {
  row: TrainingHistorySession; token: string; cache: SessionDetailsCache; onDismiss: () => void;
  /** A suspended parent sheet owns focus restoration during a modal handoff. */
  restoreFocus?: boolean;
  actions?: ReactNode;
  /** Hands off to the exercise logger; this sheet closes first so only one modal is open. */
  onEditExercises?: (row: TrainingHistorySession) => void;
  /** Hands off to the run's edit sheet; this sheet closes first. */
  onEditRun?: (row: TrainingHistorySession) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useId();
  // Slides away before the parent removes it.
  const { closing, close: dismiss } = useSheetExit(onDismiss);
  const [details, setDetails] = useState<Details | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    const previous = document.activeElement as HTMLElement | null;
    element.showModal();
    element.querySelector<HTMLButtonElement>("button")?.focus();
    const release = lockOverlayScroll();
    return () => { element.close(); release(); if (restoreFocus) previous?.focus(); };
  }, [restoreFocus]);
  useEffect(() => {
    let cancelled = false;
    const key = `${row.source}:${row.id}`;
    let request = cache.get(key);
    if (!request) {
      request = getSessionDetails(token, row.source, row.id);
      cache.set(key, request);
    }
    void request.then(result => { if (!cancelled) setDetails(result); }).catch(() => {
      if (cache.get(key) === request) cache.delete(key);
      if (!cancelled) setError(true);
    });
    return () => { cancelled = true; };
  }, [row.id, row.source, token, cache, attempt]);
  const record = details?.session;
  const [open, setOpen] = useState<ReadonlySet<number> | null>(null);
  const run = record && isRun(record) ? { ...row, ...record } : null;
  const expanded = open ?? new Set(details?.blocks.flatMap((block, index) => opensByDefault(block, details.blocks.length) ? [index] : []) ?? []);
  return <dialog ref={dialog} className={`${styles.dialog} ${styles.workspace}`} aria-labelledby={heading} data-closing={closing || undefined}
    onCancel={event => { if (event.target !== event.currentTarget) return; event.preventDefault(); dismiss(); }}
    onClick={event => { if (event.target === event.currentTarget) {
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dismiss();
    } }}>
    <header className={styles.header}><div><p className={styles.eyebrow}>Session details</p>
      <h2 id={heading}>{row.source === "external" && row.activity === "roadwork" ? trainingTitle(row) : record?.title || row.title}</h2>
      <p className={styles.quiet}>{formatAppDate(record?.training_day || row.training_day)}
        {record ? <span className={styles.sessionStatus} data-status={sessionTone(record.status)}><StatusIcon status={sessionTone(record.status)} />{sessionStatusLabel(record.status)}</span> : null}</p></div>
      <button type="button" className={styles.close} aria-label="Close session details" onClick={() => dismiss()}>×</button></header>
    <div className={styles.body} tabIndex={0} role="region" aria-label="Session record">
      {row.source === "unlxck" && row.completion_origin == null ? <p className={styles.quiet}>Legacy completion · workout identity unverified</p> : null}
      {actions}
      {/* Opens near its loaded size (a skeleton of the record), glides the
          rest of the way, and fades the record in without moving the sheet. */}
      <AutoHeight swapKey={details ? "loaded" : error ? "error" : "loading"} swapMotion="fade">
        {!details && !error ? <SessionRecordSkeleton /> : null}
        {error ? <div role="alert"><p>Unable to load session details. Try again.</p><button type="button" className="secondary-button" onClick={() => { setError(false); setAttempt(value => value + 1); }}>Retry</button></div> : null}
        {details && record && run ? <div className={styles.recordContent}>
          <RunRecord run={run} history={details.runs ?? null} onEditRun={onEditRun ? () => onEditRun(run) : undefined} />
          {details.blocks.length || onEditExercises ? <section className={styles.section}>
            <div className={styles.manualHead}><h3>Drills & exercises</h3>
              {onEditExercises ? <button type="button" className={styles.manualAction} aria-haspopup="dialog"
                onClick={() => onEditExercises(run)}>{details.blocks.length ? "Edit exercises" : "Add exercises"}</button> : null}</div>
            <ol className={styles.manualList}>{details.blocks.map((block, index) => <li key={index}>
              <span className={styles.manualName}>{block.name}</span>
              {block.actual.set_entries?.length ? <SetResults entries={block.actual.set_entries} completed={block.actual.sets} /> : <span className={styles.manualValues}>{summarise(block.actual, block.format) || NO_MEASUREMENTS}</span>}
              {block.notes ? <p className={styles.note}>{block.notes}</p> : null}
            </li>)}</ol>
          </section> : null}
          {record.notes || record.takeaway ? <section className={styles.section}><h3>Notes</h3>
            {record.notes ? <p className={styles.note}>{record.notes}</p> : null}
            {record.takeaway ? <p className={styles.note}>Takeaway: {record.takeaway}</p> : null}</section> : null}
        </div> : null}
        {details && record && !run ? <div className={styles.sessionLayout}>
          <section className={styles.sessionSummary} aria-label="Session overview">
            <dl className={styles.sessionFacts}>
              <div><TileIcon name="source" /><dt>Source</dt><dd>{record.source === "external" ? "Added by you" : "Unlxck"}</dd></div>
              {/* One effort tile and one duration tile, worded the same whether the
                  session came from Unlxck or was logged by hand. */}
              {sessionEffort(record) ? <div><TileIcon name="effort" /><dt>Session effort</dt><dd>{sessionEffort(record)}</dd></div> : null}
              {record.duration_minutes != null ? <div><TileIcon name="time" /><dt>Duration</dt><dd>{record.duration_minutes} min</dd></div> : null}
              {record.pain_after != null ? <div><TileIcon name="pain" /><dt>Pain after</dt><dd>{record.pain_after}/10</dd></div> : null}
            </dl>
          </section>
          <div className={styles.recordContent}>
          {record.source === "external" ? <section className={styles.section}>
            {record.sport || record.session_type ? <dl className={styles.measurements}>
              {record.sport ? <div><dt>Sport</dt><dd>{sportLabel(record.sport)}</dd></div> : null}
              {record.session_type ? <div><dt>Session type</dt><dd>{sessionTypes(record.sport).find(item => item.id === record.session_type)?.label || record.session_type.replaceAll("_", " ")}</dd></div> : null}
            </dl> : null}
            <div className={styles.manualHead}><h3>Exercises</h3>
              {onEditExercises && record.activity !== "sparring" ? <button type="button" className={styles.manualAction} aria-haspopup="dialog"
                onClick={() => onEditExercises({ ...row, ...record })}>{details.blocks.length ? "Edit exercises" : "Add exercises"}</button> : null}</div>
            {details.blocks.length ? <ol className={styles.manualList}>{details.blocks.map((block, index) => <li key={index}>
              <span className={styles.manualName}>{block.name}</span>
              {block.actual.set_entries?.length ? <SetResults entries={block.actual.set_entries} completed={block.actual.sets} /> : <span className={styles.manualValues}>{summarise(block.actual, block.format) || NO_MEASUREMENTS}</span>}
              {block.notes ? <p className={styles.note}>{block.notes}</p> : null}
            </li>)}</ol> : <p className={styles.quiet}>No exercises recorded. Only activity, duration and effort were logged.</p>}
            {details.sparring ? <><h3>Sparring report</h3><p>{details.sparring.rounds_completed} rounds{details.sparring.round_seconds != null ? ` · ${details.sparring.round_seconds} sec` : ""} · {details.sparring.intensity}</p>
              {details.sparring.head_contact ? <p>Head contact: {details.sparring.head_contact}</p> : null}
              {details.sparring.rocked != null ? <p>Rocked: {details.sparring.rocked ? "Yes" : "No"}</p> : null}
              {details.sparring.notes ? <p className={styles.note}>{details.sparring.notes}</p> : null}</> : null}
          </section> : <section className={styles.section}><h3>Exercises & training blocks</h3>
            {details.prescription_source === "exercise_logs" ? <p className={styles.quiet}>Saved exercise logs only. Full prescription and block order unavailable.</p> : null}
            {details.blocks.length ? <ol className={styles.blocks}>{details.blocks.map((block, index) => <TrainingBlock key={index} block={block} index={index}
              open={expanded.has(index)} onToggle={() => setOpen(new Set(expanded.has(index) ? [...expanded].filter(item => item !== index) : [...expanded, index]))} />)}</ol> : null}
          </section>}
          {details.message ? <p className={styles.quiet}>{details.message}</p> : null}
          {record.notes || record.modification_reason || record.takeaway ? <section className={styles.section}><h3>Session feedback</h3>
            {record.modification_reason ? <p className={styles.note}>Modification: {record.modification_reason}</p> : null}
            {record.notes ? <p className={styles.note}>{record.notes}</p> : null}
            {record.takeaway ? <p className={styles.note}>Takeaway: {record.takeaway}</p> : null}</section> : null}
          </div>
        </div> : null}
      </AutoHeight>
    </div>
  </dialog>;
}
