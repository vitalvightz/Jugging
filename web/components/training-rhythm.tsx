"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { TrainingHistorySession } from "@/lib/training-history";
import { trainingTitle } from "@/lib/training-disciplines";
import { formatAppDate } from "@/lib/date-format";
import { sessionStatusLabel, sessionStatusTone } from "@/lib/history";
import { formatTrainingDuration, summaryWindowLoaded, trainingDiscipline } from "@/lib/training-summary";
import { rhythmWindowDays, trainingWeeks, weekComparison, weekValue, type TrainingWeek } from "@/lib/training-rhythm";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import { SessionDetails, type SessionDetailsCache } from "./session-details";
import sheet from "./session-details.module.css";
import styles from "./training-progress.module.css";
import { useSheetExit } from "@/lib/use-sheet-exit";

type Metric = "sessions" | "minutes";
type Props = {
  rows: readonly TrainingHistorySession[]; today: string; loadedFrom: string | null;
  loading: boolean; error: string | null; onLoadMore: () => Promise<void>;
  token: string; detailsCache: SessionDetailsCache;
};
const shortDate = (day: string) => new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }).format(new Date(`${day}T12:00:00Z`));
const count = (value: number, noun: string) => `${value} ${noun}${value === 1 ? "" : "s"}`;
const weekLabel = (week: TrainingWeek) => `${shortDate(week.start)} – ${shortDate(week.end)}${week.current ? " · This week" : ""}`;

function WeeklyChart({ weeks, metric, selected, onSelect, preview = false, active = true }: {
  weeks: TrainingWeek[]; metric: Metric; selected?: string; onSelect: (start: string) => void; preview?: boolean; active?: boolean;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const first = weeks.findIndex(week => week.sessions > 0);
  const shown = first > 0 ? weeks.slice(first) : weeks;
  const target = selected ?? shown.at(-1)?.start;
  const targetIndex = shown.findIndex(week => week.start === target);
  useEffect(() => {
    if (!active) return;
    // Run after the native dialog has entered the top layer, when widths exist.
    const frame = window.requestAnimationFrame(() => {
      const element = scroller.current;
      if (element) element.scrollLeft = 30 + (targetIndex + .5) * ((element.scrollWidth - 30) / shown.length) - element.clientWidth / 2;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [active, targetIndex, shown.length]);
  const max = Math.max(1, ...shown.map(week => weekValue(week, metric) ?? 0));
  const ceiling = metric === "sessions" ? Math.ceil(max / 2) * 2 : Math.ceil(max / 30) * 30;
  return <div ref={scroller} className={styles.chartScroll}>
    <div className={styles.weekChart} data-preview={preview || undefined} style={{ minWidth: Math.max(240, shown.length * 44 + 30) }}>
      <div className={styles.weekPlot}>
        {[1, .5, 0].map(fraction => <div key={fraction} className={styles.gridline} style={{ bottom: `${fraction * 100}%` }}>
          <span>{Math.round(ceiling * fraction)}</span>
        </div>)}
        <div className={styles.columns}>
          {shown.map(week => {
            const value = weekValue(week, metric);
            return <button type="button" key={week.start} className={styles.weekButton}
              aria-pressed={selected === week.start} aria-label={`${weekLabel(week)}: ${value === null ? "Recorded minutes unavailable" : (metric === "minutes" ? `${value} recorded minutes` : count(value, "session"))}${week.untimed ? `; ${count(week.untimed, "missing duration")}` : ""}`}
              onClick={() => onSelect(week.start)}>
              <span className={styles.barTrack} aria-hidden="true"><span className={styles.weekBar} data-current={week.current || undefined}
                data-unknown={value === null || undefined} style={{ height: value === null ? "12px" : `${Math.max(value > 0 ? 2 : 0, (value / ceiling) * 100)}%` }} /></span>
              <span className={styles.weekTick}>{shortDate(week.start)}</span>
            </button>;
          })}
        </div>
      </div>
    </div>
  </div>;
}

/** The controller stays mounted across the single-modal session handoff. */
export function TrainingRhythm(props: Props) {
  const { rows, today, loadedFrom, loading, error, onLoadMore, token, detailsCache } = props;
  const [open, setOpen] = useState(false);
  const [range, setRange] = useState<4 | 12>(12);
  const [metric, setMetric] = useState<Metric>("sessions");
  const [discipline, setDiscipline] = useState("all");
  const [selectedWeek, setSelectedWeek] = useState<string | null>(null);
  const [session, setSession] = useState<TrainingHistorySession | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const scrollTop = useRef(0);
  const returnSession = useRef<string | null>(null);
  const heading = useId();
  const needed = rhythmWindowDays(today, open ? range : 8);
  const complete = summaryWindowLoaded(loadedFrom, today, needed);
  const previewComplete = summaryWindowLoaded(loadedFrom, today, rhythmWindowDays(today, 8));
  const weeks = useMemo(() => trainingWeeks(rows, today, range, discipline), [rows, today, range, discipline]);
  const preview = useMemo(() => trainingWeeks(rows, today, 8), [rows, today]);
  const selected = weeks.find(week => week.start === selectedWeek) ?? weeks.at(-2)!;
  const disciplines = useMemo(() => {
    const values = new Map<string, string>();
    for (const week of trainingWeeks(rows, today, range)) for (const row of week.rows) {
      const item = trainingDiscipline(row); values.set(item.key, item.label);
    }
    // Keep an active filter selectable even when its range contains no records.
    if (discipline !== "all" && !values.has(discipline)) {
      const row = rows.find(row => trainingDiscipline(row).key === discipline);
      if (row) values.set(discipline, trainingDiscipline(row).label);
    }
    return [...values].sort((a, b) => a[1].localeCompare(b[1]));
  }, [rows, today, range, discipline]);
  useEffect(() => {
    if (!complete && !loading && !error) void onLoadMore();
  }, [complete, loading, error, onLoadMore]);
  useEffect(() => {
    if (!open || session) return;
    const element = dialog.current!;
    element.showModal();
    const release = lockOverlayScroll();
    const content = body.current;
    if (content) content.scrollTop = scrollTop.current;
    const target = returnSession.current;
    const sessionButton = target ? [...element.querySelectorAll<HTMLButtonElement>("[data-session]")].find(button => button.dataset.session === target) : null;
    (sessionButton ?? element.querySelector<HTMLButtonElement>("button"))?.focus({ preventScroll: true });
    return () => { scrollTop.current = content?.scrollTop ?? scrollTop.current; element.close(); release(); };
  }, [open, session]);
  function finishClose() { dialog.current?.close(); setOpen(false); returnSession.current = null; scrollTop.current = 0; trigger.current?.focus(); }
  // Slides away first, then closes.
  const { closing, close } = useSheetExit(finishClose);
  function show(start?: string) { if (start) setSelectedWeek(start); setOpen(true); }
  const last = preview.at(-2)!;
  return <>
    <section className={styles.preview} aria-label="Training Rhythm preview">
      <div className={styles.sectionHead}><div><h3>Training Rhythm</h3><p>Recorded sessions across 8 weeks</p></div>
        <button ref={trigger} type="button" className={styles.textButton} aria-haspopup="dialog" onClick={() => show()}>View trends <span aria-hidden="true">↗</span></button></div>
      {previewComplete ? preview.some(week => week.sessions) ? <>
        <div className={styles.headline}><strong>{last.sessions}</strong><span>{last.sessions === 1 ? "session" : "sessions"}<br />last completed week</span></div>
        <WeeklyChart weeks={preview} metric="sessions" onSelect={show} preview />
        <p className={styles.note}>Select a week to see its training.</p>
      </> : <p className={styles.note}>No sessions recorded in this period.</p>
        : <Loading complete={false} loading={loading} error={error} onLoadMore={onLoadMore} />}
    </section>
    {open ? <dialog ref={dialog} className={sheet.dialog} aria-labelledby={heading} data-closing={closing || undefined} onCancel={event => { event.preventDefault(); close(); }}>
      <header className={sheet.header}><div><p className={sheet.eyebrow}>Your training</p><h2 id={heading}>Training Rhythm</h2><p className={sheet.quiet}>Recorded training, week by week.</p></div>
        <button type="button" className={sheet.close} aria-label="Close training trends" onClick={() => close()}>×</button></header>
      <div ref={body} className={sheet.body} role="region" aria-label="Training trends" tabIndex={0}>
        <div className={styles.controls}>
          <div className={styles.segment} role="group" aria-label="Trend range">{([4, 12] as const).map(value => <button key={value} type="button" aria-pressed={range === value} onClick={() => setRange(value)}>{value} weeks</button>)}</div>
          <div className={styles.segment} role="group" aria-label="Trend metric">{(["sessions", "minutes"] as const).map(value => <button key={value} type="button" aria-pressed={metric === value} onClick={() => setMetric(value)}>{value === "sessions" ? "Sessions" : "Minutes"}</button>)}</div>
          <label className={styles.selectLabel}>Discipline<select aria-label="Discipline" value={discipline} onChange={event => setDiscipline(event.target.value)}><option value="all">All disciplines</option>{disciplines.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        </div>
        {complete ? <>
          <div className={styles.headline}><strong>{metric === "sessions" ? selected.sessions : weekValue(selected, metric) === null ? "—" : selected.minutes}</strong><span>{metric === "sessions" ? selected.sessions === 1 ? "session" : "sessions" : "recorded minutes"}<br />{weekLabel(selected)}</span></div>
          <p className={styles.note}>{weekComparison(weeks, metric)}</p>
          {weeks.some(week => week.sessions > 0) ? <WeeklyChart weeks={weeks} metric={metric} selected={selected.start} onSelect={setSelectedWeek} active={open && !session} />
            : <p className={styles.note}>No sessions recorded in this period.</p>}
          <div className={styles.weekDetail} aria-live="polite"><h3>{weekLabel(selected)}</h3>
            <p className={styles.note}>{count(selected.sessions, "session")} · {selected.sessions > 0 && selected.untimed === selected.sessions ? "Recorded time unavailable" : `${formatTrainingDuration(selected.minutes)} recorded`}{selected.untimed ? ` · ${count(selected.untimed, "missing duration")}` : ""}</p>
          </div>
          <ul className={`history-list ${styles.sessionList}`} aria-label="Sessions in selected week">{selected.rows.map(row => <li key={`${row.source}:${row.id}`} className="history-row" data-tone={sessionStatusTone(row.status)}>
            <button type="button" className={styles.sessionOpen} data-session={`${row.source}:${row.id}`} aria-haspopup="dialog" onClick={() => {
              scrollTop.current = body.current?.scrollTop ?? 0; returnSession.current = `${row.source}:${row.id}`;
              // Close synchronously before SessionDetails mounts; never two modals.
              dialog.current?.close(); setSession(row);
            }}><span><strong>{trainingTitle(row)}</strong><span>{formatAppDate(row.training_day)} · {row.duration_minutes ? `${row.duration_minutes}m recorded` : "Duration not recorded"}</span></span>
              <span className="badge history-status-badge" data-tone={sessionStatusTone(row.status)}>{sessionStatusLabel(row.status)}</span></button>
          </li>)}</ul>
          {!selected.sessions ? <p className={styles.note}>No sessions recorded.</p> : null}
        </> : <Loading complete={false} loading={loading} error={error} onLoadMore={onLoadMore} />}
      </div>
    </dialog> : null}
    {session ? <SessionDetails key={`${session.source}:${session.id}`} row={session} token={token} cache={detailsCache} onDismiss={() => setSession(null)} restoreFocus={false} /> : null}
  </>;
}

function Loading({ loading, error, onLoadMore }: Pick<Props, "loading" | "error" | "onLoadMore"> & { complete: false }) {
  return error ? <div role="alert"><p className={styles.note}>Some sessions failed to load. Trends unavailable until the period is complete.</p><button type="button" className="secondary-button" disabled={loading} onClick={() => void onLoadMore()}>Retry trends</button></div>
    : <p role="status" className={styles.note}>Loading every session in this period…</p>;
}
