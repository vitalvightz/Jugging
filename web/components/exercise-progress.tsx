"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ExercisePerformance, PersonalExercise } from "@/lib/api-schema.generated";
import { formatAppDate } from "@/lib/date-format";
import { measurementSummary, splitLoad } from "@/lib/session-details";
import { measurementBasis, measurementCount, progressPoints, progressSeries } from "@/lib/exercise-progress";
import styles from "./training-progress.module.css";
import { useSeenOnce } from "./once-hint";

export function ExerciseProgress({ records, exercise, hasMore, about, onInspect }: {
  records: ExercisePerformance[]; exercise: PersonalExercise; hasMore: boolean;
  /** Record-level explanations, folded into the chart's "About these numbers". */
  about?: ReactNode; onInspect: (id: string) => void;
}) {
  const options = useMemo(() => progressSeries(records), [records]);
  const [seriesId, setSeriesId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pointHint, dismissPointHint] = useSeenOnce("discovery-progress-point-seen");
  const series = options.find(option => option.id === seriesId) ?? options[0];
  // Only eligible measurements: axis, path, count, selection and best all derive from this set.
  const valid = useMemo(() => series ? progressPoints(records, series) : [], [records, series]);
  const plot = useRef<HTMLDivElement>(null);
  const [plotWidth, setPlotWidth] = useState(600);
  const chartVisible = valid.length >= 3;
  useEffect(() => {
    const element = plot.current;
    if (!element) return;
    const observer = new ResizeObserver(entries => setPlotWidth(Math.max(24, entries[0].contentRect.width)));
    observer.observe(element);
    return () => observer.disconnect();
  }, [chartVisible]);
  const selected = valid.find(point => point.record.id === selectedId) ?? valid.at(-1);
  const selectedIndex = valid.findIndex(point => point.record.id === selected?.record.id);
  const values = valid.map(point => point.value);
  const max = Math.max(...values), min = Math.min(...values);
  const highestPoint = valid.findLast(point => point.value === max);
  const padding = Math.max((max - min) * .2, max * .05, 1);
  // Whole-number bounds when the range allows, so the axis labels read as real values.
  const whole = max + padding - Math.max(0, min - padding) >= 4;
  const floor = whole ? Math.floor(Math.max(0, min - padding)) : Math.max(0, min - padding);
  const ceiling = whole ? Math.ceil(max + padding) : max + padding;
  const firstDay = valid[0]?.record.training_day, lastDay = valid.at(-1)?.record.training_day;
  const start = Date.parse(firstDay ?? ""), span = Date.parse(lastDay ?? "") - start;
  const x = (day: string) => span ? 12 + (Date.parse(day) - start) / span * (plotWidth - 24) : plotWidth / 2;
  const y = (value: number) => 180 - (value - floor) / (ceiling - floor) * 160;
  // Irregular dates without records do not invent samples.
  const path = valid.map((point, index) => `${index ? "L" : "M"}${x(point.record.training_day).toFixed(2)},${y(point.value).toFixed(2)}`).join(" ");
  const format = (value: number) => `${Number(value.toFixed(2))} ${series?.unit}`;
  const best = series?.metric === "load" ? exercise.best_loads.find(load => load.unit === series.unit)?.value : undefined;
  const choose = (index: number) => {
    dismissPointHint();
    setSelectedId(valid[Math.max(0, Math.min(valid.length - 1, index))].record.id);
  };
  // Sessions with several loads plot their heaviest set; say so beside the value.
  const loads = selected && series.metric === "load" ? (selected.record.performed_values?.set_entries ?? []).flatMap(entry => entry.load?.unit === series.unit ? [entry.load.value] : []) : [];
  const ranged = loads.length > 1 && Math.min(...loads) < Math.max(...loads);
  // The dose beside the value; a plotted load isn't repeated, its range is stated above.
  const performed = { ...selected?.record.performed_values as Record<string, unknown>, rest: undefined, effort: undefined };
  const dose = !selected ? "" : series.metric === "load" ? splitLoad(performed).dose : measurementSummary(performed);
  const weight = series?.metric === "load";
  return <section className={styles.exerciseChart} aria-label="Exercise progress">
    <div className={styles.sectionHead}><h3>{series?.metric === "load" ? "Heaviest load per session" : "Recorded performance"}</h3>
      {options.length > 1 && options.length <= 4 ? <div className={`${styles.segment} ${styles.metrics}`} role="group" aria-label="Progress measurement">
        {options.map(option => <button key={option.id} type="button" aria-pressed={option.id === series.id} onClick={() => { setSeriesId(option.id); setSelectedId(null); }}>
          {option.label}{option.unit !== option.metric ? ` · ${option.unit}` : ""}</button>)}
      </div> : options.length > 1 ? <label className={styles.selectLabel}>Measurement<select aria-label="Progress measurement" value={series.id} onChange={event => { setSeriesId(event.target.value); setSelectedId(null); }}>
        {options.map(option => <option key={option.id} value={option.id}>{option.label}{option.unit !== option.metric ? ` · ${option.unit}` : ""}</option>)}
      </select></label> : series ? <span className={styles.note}>{series.label} · {series.unit}</span> : null}
    </div>
    <details className={styles.about}><summary>About these numbers</summary>
      {about}
      {series?.metric === "load" && records.some(record => record.performed_values?.set_entries?.length) ? <p>Load is each session’s heaviest set.</p> : null}
      {series?.metric === "load" ? <p>A heavier load isn’t automatically a better performance: different sets and reps are not equivalent strength performances.</p> : null}
    </details>
    {!selected ? <p className={styles.note}>No comparable measurements in the loaded records.{hasMore ? " Load earlier performances to check older records." : ""}</p> : <>
      <div className={styles.selectedRecord} aria-live="polite" aria-label="Selected performance">
        <p><strong data-weight={weight || undefined}>{format(selected.value)}</strong>
          {best != null && selected.value === best ? <span className={styles.bestPill}>Best</span> : null}
          {ranged ? <span className={styles.note}> heaviest of {Math.min(...loads)}–{Math.max(...loads)} {series.unit}</span> : null}</p>
        <p className={styles.note}>{[formatAppDate(selected.record.training_day), dose, measurementBasis(selected.record, series.metric)].filter(Boolean).join(" · ")}</p>
        <button type="button" className={styles.textButton} onClick={() => onInspect(selected.record.id)}>Show in history <span aria-hidden="true">↓</span></button>
      </div>
      <p className={styles.note}>{measurementCount(valid.length, series.metric)}{hasMore ? " · Earlier records available" : ""}{best != null ? ` · Best recorded ${format(best)}` : ""}.</p>
      {valid.length >= 3 ? <>
        {/* The plot is the control: tap a point, or use arrow keys when focused. */}
        {pointHint ? <div className={styles.discoveryHint} role="note">
          <span>Tap a point to inspect that result.</span>
          <button type="button" onClick={dismissPointHint} aria-label="Dismiss chart tip">×</button>
        </div> : null}
        <div ref={plot} className={styles.linePlot} role="slider" tabIndex={0} aria-label="Inspect performance"
          aria-valuemin={1} aria-valuemax={valid.length} aria-valuenow={selectedIndex + 1}
          aria-valuetext={`${formatAppDate(selected.record.training_day)}: ${format(selected.value)}`}
          onKeyDown={event => {
            const next = { ArrowLeft: selectedIndex - 1, ArrowDown: selectedIndex - 1, ArrowRight: selectedIndex + 1, ArrowUp: selectedIndex + 1, Home: 0, End: valid.length - 1 }[event.key];
            if (next === undefined) return;
            event.preventDefault(); choose(next);
          }}
          onPointerDown={event => {
            const bounds = event.currentTarget.getBoundingClientRect();
            const px = (event.clientX - bounds.left) / bounds.width * plotWidth;
            const py = (event.clientY - bounds.top) / bounds.height * 200;
            const nearest = valid.reduce((a, b) => Math.hypot(x(a.record.training_day) - px, y(a.value) - py) <= Math.hypot(x(b.record.training_day) - px, y(b.value) - py) ? a : b);
            dismissPointHint();
            setSelectedId(nearest.record.id);
            if (event.pointerType === "mouse") event.currentTarget.setPointerCapture(event.pointerId);
          }} onPointerMove={event => {
            if (event.buttons !== 1 || event.pointerType !== "mouse") return;
            const bounds = event.currentTarget.getBoundingClientRect();
            const px = (event.clientX - bounds.left) / bounds.width * plotWidth;
            const nearest = valid.reduce((a, b) => Math.abs(x(a.record.training_day) - px) <= Math.abs(x(b.record.training_day) - px) ? a : b);
            setSelectedId(nearest.record.id);
          }}>
          <svg viewBox={`0 0 ${plotWidth} 200`} preserveAspectRatio="none" aria-hidden="true">
            {[20, 100, 180].map(value => <line key={value} x1="0" x2={plotWidth} y1={value} y2={value} className={styles.lineGrid} />)}
            <path d={`${path} L${x(lastDay!).toFixed(2)},180 L${x(firstDay!).toFixed(2)},180 Z`} className={styles.area} />
            <path d={path} className={styles.line} />
            {valid.map(point => <g key={point.record.id}>
              {point === highestPoint ? <circle cx={x(point.record.training_day)} cy={y(point.value)} r="8" className={weight ? styles.bestRing : styles.point} /> : null}
              <circle cx={x(point.record.training_day)} cy={y(point.value)} r={selected.record.id === point.record.id ? 6 : 4} className={selected.record.id === point.record.id ? styles.activePoint : styles.point} />
            </g>)}
          </svg>
          {[ceiling, (ceiling + floor) / 2, floor].map((value, index) => <span key={index} className={styles.yTick} style={{ top: `${10 + index * 40}%` }} aria-hidden="true">{format(value)}</span>)}
        </div>
        <div className={styles.dateAxis}><span>{formatAppDate(firstDay)}</span><span>{formatAppDate(lastDay)}</span></div>
      </> : valid.length === 2 ? <div className={styles.twoRecords}>{valid.map(point => <button key={point.record.id} type="button" aria-pressed={selected.record.id === point.record.id} onClick={() => setSelectedId(point.record.id)}>
        <span>{formatAppDate(point.record.training_day)}</span><strong>{format(point.value)}</strong><span>Inspect performance</span>
      </button>)}</div> : null}
    </>}
  </section>;
}
