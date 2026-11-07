"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useSeenOnce } from "./once-hint";
import { EmptyState } from "./empty-state";
import { TrainingBlock } from "./session-details";
import { ExerciseProgress } from "./exercise-progress";
import { getExercisePerformances } from "@/lib/api";
import type { ExerciseLibraryResponse, ExercisePerformancesResponse, PersonalExercise } from "@/lib/api-schema.generated";
import { formatAppDate } from "@/lib/date-format";
import { exerciseResult } from "@/lib/exercise-library";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import sheet from "./session-details.module.css";
import styles from "./exercise-library.module.css";
import { useSheetExit } from "@/lib/use-sheet-exit";
import { Skeleton } from "./skeleton";
import { AutoHeight } from "./today/auto-height";

export type ExerciseHistoryCache = Map<string, Promise<ExercisePerformancesResponse>>;

function ExerciseCard({ exercise, onSelect }: { exercise: PersonalExercise; onSelect: () => void }) {
  const result = exerciseResult(exercise);
  return <li className={`history-row ${styles.card}`} data-tone={exercise.identity_kind === "legacy" ? "grey" : "green"}>
    <button className={styles.open} type="button" aria-haspopup="dialog" aria-label={`View ${exercise.name} performance history${exercise.identity_kind === "legacy" ? ` for ${formatAppDate(exercise.latest_day)}` : ""}`} onClick={onSelect}>
      <span className={styles.cardHeading}><span className="history-row-title">{exercise.name}</span><span className={styles.chevron} aria-hidden="true">›</span></span>
      <span className={styles.resultLabel}>{result.label}</span><strong className={styles.result} data-weight={exercise.best_loads.length > 0 || undefined}>{result.value}</strong>
      <span className={styles.date}>{exercise.identity_kind === "legacy" ? "Unlinked record" : exercise.identity_kind === "personal" ? "Your exercise · Last performed" : "Last performed"} · {formatAppDate(exercise.latest_day)}</span>
    </button>
  </li>;
}

export function ExerciseLibrary({ library, token, cache }: { library: ExerciseLibraryResponse; token: string; cache: ExerciseHistoryCache }) {
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<PersonalExercise | null>(null);
  const [progressHintUnseen, dismissProgressHint] = useSeenOnce("discovery-exercise-progress-seen");
  function selectExercise(exercise: PersonalExercise) {
    dismissProgressHint();
    setSelected(exercise);
  }
  if (!library.exercises.length) return <EmptyState eyebrow="Exercise records" title="No performed exercises recorded yet."
    description="Completed and modified exercise logs, and exercises you add to logged training, build your personal record. Skipped-only exercises are excluded."
    example="Log your performance on Today, or add exercises to a session you logged." />;
  const query = search.trim().toLocaleLowerCase();
  const matching = library.exercises.filter(exercise => `${exercise.name} ${exercise.identity_kind === "canonical" ? exercise.id.slice(4).replaceAll("-", " ") : ""}`.toLocaleLowerCase().includes(query));
  // Stable identities: catalogue movements and the athlete's own named ones.
  const canonical = matching.filter(exercise => exercise.identity_kind !== "legacy");
  const legacy = matching.filter(exercise => exercise.identity_kind === "legacy");
  return <div className="history-tab-body">
    <label className="history-exercise-search">Find an exercise
      <input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search your exercise records" />
    </label>
    <p className={styles.scope}>Best saved loads, kept separate per weight unit.</p>
    {progressHintUnseen && !search ? <div className={styles.discoveryHint} role="note">
      <span>Tap an exercise to see its progress and session history.</span>
      <button type="button" onClick={dismissProgressHint} aria-label="Dismiss exercise progress tip">×</button>
    </div> : null}
    {canonical.length ? <ul className={`history-list ${styles.library}`} aria-label="Your exercises">
      {canonical.map(exercise => <ExerciseCard key={exercise.id} exercise={exercise} onSelect={() => selectExercise(exercise)} />)}
    </ul> : null}
    {legacy.length ? <section className={styles.legacy} aria-label="Unlinked legacy records">
      <h2>Unlinked legacy records</h2><p className={styles.scope}>These records have no exercise key, so they stay separate.</p>
      <ul className={`history-list ${styles.library}`}>{legacy.map(exercise => <ExerciseCard key={exercise.id} exercise={exercise} onSelect={() => selectExercise(exercise)} />)}</ul>
    </section> : null}
    {!matching.length ? <p className="muted" role="status">No matching exercises in your saved history.</p> : null}
    {selected ? <ExerciseDetails key={selected.id} exercise={selected} asOf={library.as_of} token={token} cache={cache} onDismiss={() => setSelected(null)} /> : null}
  </div>;
}

export function ExerciseDetails({ exercise, asOf, token, cache, onDismiss }: {
  exercise: PersonalExercise; asOf: string; token: string; cache: ExerciseHistoryCache; onDismiss: () => void;
}) {
  // Slides away before the parent removes it.
  const { closing, close: dismiss } = useSheetExit(onDismiss);
  const dialog = useRef<HTMLDialogElement>(null);
  const alive = useRef(false);
  const loadingMore = useRef(false);
  const heading = useId();
  const timeline = useRef<HTMLOListElement>(null);
  const [data, setData] = useState<ExercisePerformancesResponse | null>(null);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  // History rows start collapsed; the chart opens the one it points to.
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  // A fresh object per request, so inspecting the same point twice still scrolls.
  const [reveal, setReveal] = useState<{ id: string } | null>(null);
  const [view, setView] = useState<"progress" | "history">("progress");
  const [limit, setLimit] = useState<number | null>(null);
  const tabs = useId();
  useEffect(() => {
    if (!reveal) return;
    const target = [...(timeline.current?.children ?? [])].find(element => (element as HTMLElement).dataset.performance === reveal.id) as HTMLElement | undefined;
    target?.scrollIntoView({ block: "start", behavior: "instant" }); target?.focus({ preventScroll: true });
  }, [reveal]);
  const cacheKey = `${asOf}:${exercise.id}`;
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    alive.current = true;
    const previous = document.activeElement as HTMLElement | null;
    element.showModal(); element.querySelector<HTMLButtonElement>("button")?.focus();
    const release = lockOverlayScroll();
    return () => { alive.current = false; element.close(); release(); previous?.focus(); };
  }, []);
  useEffect(() => {
    let cancelled = false;
    let request = cache.get(cacheKey);
    if (!request) { request = getExercisePerformances(token, exercise.id, asOf); cache.set(cacheKey, request); }
    void request.then(page => { if (!cancelled) setData(page); }).catch(() => {
      if (cache.get(cacheKey) === request) cache.delete(cacheKey);
      if (!cancelled) setError(true);
    });
    return () => { cancelled = true; };
  }, [cache, cacheKey, exercise.id, asOf, token, attempt]);
  async function more() {
    if (!data?.next_cursor || loadingMore.current) return;
    loadingMore.current = true; setBusy(true); setError(false);
    const previous = data;
    const request = getExercisePerformances(token, exercise.id, asOf, data.next_cursor).then(page => {
      const seen = new Set(previous.performances.map(item => item.id));
      return { ...page, performances: [...previous.performances, ...page.performances.filter(item => !seen.has(item.id))] };
    });
    cache.set(cacheKey, request);
    try { const page = await request; if (alive.current) setData(page); }
    catch { if (cache.get(cacheKey) === request) cache.set(cacheKey, Promise.resolve(previous)); if (alive.current) setError(true); }
    finally { loadingMore.current = false; if (alive.current) setBusy(false); }
  }
  const result = exerciseResult(exercise);
  return <dialog ref={dialog} className={`${sheet.dialog} ${sheet.workspace}`} aria-labelledby={heading} data-closing={closing || undefined}
    onCancel={event => { event.preventDefault(); dismiss(); }}
    onClick={event => { if (event.target === event.currentTarget) {
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dismiss();
    } }}>
    <header className={sheet.header}><div className={styles.detailHeading}><div><p className={sheet.eyebrow}>Exercise record</p><h2 id={heading}>{exercise.name}</h2></div>
      <div><p className={styles.resultLabel}>{result.label}</p><strong className={styles.detailResult} data-weight={exercise.best_loads.length > 0 || undefined}>{result.value}</strong></div></div>
      <button type="button" className={sheet.close} aria-label="Close exercise history" onClick={() => dismiss()}>×</button></header>
    <div className={sheet.body} tabIndex={0} role="region" aria-label="Exercise performance record">
      {/* Opens near its loaded size (skeletons of the chart and the list),
          glides the rest of the way, and fades the record in without moving
          the sheet. */}
      <AutoHeight swapKey={data ? "loaded" : error ? "error" : "loading"} swapMotion="fade">
        {/* Phones and tablets switch between the chart and the list; wide screens show both. */}
        <div className={styles.views} role="tablist" aria-label="Exercise record views">
          {(["progress", "history"] as const).map(id => <button key={id} type="button" role="tab" id={`${tabs}-${id}`} aria-controls={`${tabs}-${id}-panel`}
            aria-selected={view === id} onClick={() => setView(id)}>{id === "progress" ? "Progress" : "History"}</button>)}
        </div>
        <div className={sheet.exerciseLayout}><div className={sheet.analysisColumn} role="tabpanel" id={`${tabs}-progress-panel`} aria-labelledby={`${tabs}-progress`} data-active={view === "progress"}>
        {data ? <ExerciseProgress records={data.performances} exercise={exercise} hasMore={data.next_cursor !== null} about={<>
          <p>{exercise.identity_kind === "legacy" ? "One original legacy record. Movement identity across other dates is unknown."
            : exercise.identity_kind === "personal" ? "An exercise you named, from training you logged. Only you can see it. These are not individual set records."
            : "Saved results from your plans and the training you logged. These are not individual set records."}</p>
          {exercise.best_loads.length ? <p>Highest eligible recorded load by unit; not an estimated one-rep maximum.</p> : null}
        </>} onInspect={id => { setView("history"); setLimit(null); setOpen(current => new Set(current).add(id)); setReveal({ id }); }} /> : !error ? <ProgressSkeleton /> : null}
        </div>
        <section className={`${sheet.section} ${sheet.performanceColumn}`} role="tabpanel" id={`${tabs}-history-panel`} aria-labelledby={`${tabs}-history`} data-active={view === "history"}>
          <div className={styles.historyHead}><h3>History</h3>
            {data && data.performances.length > 3 ? <div className={styles.chips} role="group" aria-label="Show records">
              {[null, 3, 6, 12].filter(count => count === null || count < data.performances.length).map(count => <button key={count ?? "all"} type="button" aria-pressed={limit === count}
                onClick={() => setLimit(count)}>{count === null ? "All" : `Last ${count}`}</button>)}
            </div> : null}</div>
          {!data && !error ? <HistorySkeleton /> : null}
          {data ? <ol ref={timeline} className={sheet.blocks}>{data.performances.slice(0, limit ?? undefined).map((item, index) => <TrainingBlock key={item.id} block={item.block} index={index} heading={formatAppDate(item.training_day)} recordId={item.id} numbered={false}
            open={open.has(item.id)} onToggle={() => setOpen(current => { const next = new Set(current); if (!next.delete(item.id)) next.add(item.id); return next; })} />)}</ol> : null}
          {data?.performances.length === 0 ? <p className={sheet.quiet}>No remaining records for this exercise.</p> : null}
          {error ? <div role="alert"><p>{data ? "Couldn't load earlier records. Loaded records are still here." : "Unable to load exercise records. Try again."}</p><button type="button" className="secondary-button" onClick={() => { setError(false); if (data?.next_cursor) void more(); else setAttempt(value => value + 1); }}>Retry</button></div> : null}
          {data?.next_cursor && !error && limit === null ? <button type="button" className="secondary-button" disabled={busy} onClick={() => void more()}>{busy ? "Loading…" : "Load earlier performances"}</button> : null}
        </section>
        </div>
      </AutoHeight>
    </div>
  </dialog>;
}

/** The rough shape of the progress view: its heading and the chart card. */
function ProgressSkeleton() {
  return <div className={styles.recordSkeleton} aria-hidden="true">
    <Skeleton variant="text" width="48%" height={18} />
    <Skeleton height={132} />
    <Skeleton variant="text" width="62%" height={12} />
  </div>;
}

/** The rough shape of the history list: a few collapsed rows. */
function HistorySkeleton() {
  return <div className={styles.recordSkeleton}>
    <p role="status" className="sr-only">Loading exercise history…</p>
    <Skeleton height={64} /><Skeleton height={64} /><Skeleton height={64} />
  </div>;
}
