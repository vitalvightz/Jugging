"use client";

import { useEffect, useRef, useState } from "react";

import { shiftDate } from "@/lib/nutrition-bodyweight";
import progress from "./training-progress.module.css";
import styles from "./run-trend-chart.module.css";
import weight from "./weight-tracker.module.css";
import { shortDate } from "./weight-trend-chart";

export type RunChartPoint = { id: string; day: string; value: number; current: boolean };

const dayMid = (date: string) => Date.parse(`${date}T12:00`);
// Room for the y labels on the left, and for the markers at the edges.
const LEFT = 44, RIGHT = 10, TOP = 8, BOTTOM = 8, PAD = 1 / 3;

/**
 * Logged runs on a calendar time axis ending at this run, drawn like the
 * weight trend: four round gridlines fitted to the values, the runs joined in
 * order, nothing smoothed. Pace is inverted so faster sits higher. The plot is
 * the control: tap or drag across it, or use the arrow keys when focused.
 */
export function RunTrendChart({ points, steps, format, tick, invert = false, label, selectedIndex, onSelect }: {
  points: readonly RunChartPoint[];
  /** Round gridline steps to choose from, smallest first. */
  steps: readonly number[];
  format: (value: number) => string;
  tick: (value: number) => string;
  invert?: boolean;
  label: string;
  selectedIndex: number;
  onSelect: (index: number) => void;
}) {
  const plot = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 600, height: 150 });
  const [active, setActive] = useState(false);
  useEffect(() => {
    const element = plot.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(entries => {
      const { width, height } = entries[0].contentRect;
      setSize({ width: Math.max(LEFT + RIGHT + 24, width), height: Math.max(TOP + BOTTOM + 24, height) });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const { width, height } = size;

  const firstDay = points[0].day, lastDay = points[points.length - 1].day;
  const start = dayMid(shiftDate(firstDay, -1)), end = dayMid(shiftDate(lastDay, 1));
  const x = (day: string) => LEFT + (dayMid(day) - start) / (end - start) * (width - LEFT - RIGHT);

  const values = points.map(point => point.value);
  const low = Math.min(...values), high = Math.max(...values);
  const base = (step: number) => Math.round(((low + high) / 2 - 1.5 * step) / step) * step;
  const fits = (s: number) => base(s) - PAD * s <= low + 1e-9 && base(s) + (3 + PAD) * s >= high - 1e-9;
  const largest = steps[steps.length - 1];
  const step = steps.find(fits) ?? Math.ceil((high - low) / ((3 + 2 * PAD) * largest) + 1) * largest;
  const ticks = [3, 2, 1, 0].map(n => base(step) + n * step).filter(value => value >= 0);
  const floor = base(step) - PAD * step, ceiling = base(step) + (3 + PAD) * step;
  const y = (value: number) => {
    const share = (value - floor) / (ceiling - floor);
    return TOP + (invert ? share : 1 - share) * (height - TOP - BOTTOM);
  };

  const coords = points.map(point => [x(point.day), y(point.value)] as const);
  const path = coords.map(([px, py], index) => `${index ? "L" : "M"}${px.toFixed(2)},${py.toFixed(2)}`).join(" ");
  const last = points.length - 1;
  const selected = points[selectedIndex] ?? points[last];
  const [selectedX, selectedY] = coords[selectedIndex] ?? coords[last];
  const choose = (index: number) => onSelect(Math.max(0, Math.min(last, index)));
  const nearest = (clientX: number, bounds: DOMRect) => {
    const px = (clientX - bounds.left) / bounds.width * width;
    return coords.reduce((best, [pointX], index) => Math.abs(pointX - px) < Math.abs(coords[best][0] - px) ? index : best, 0);
  };
  const withYear = firstDay.slice(0, 4) !== lastDay.slice(0, 4);

  return <>
    <div ref={plot} className={`${progress.linePlot} ${weight.plot}`} role="slider" tabIndex={0} aria-label={label}
      aria-valuemin={1} aria-valuemax={points.length} aria-valuenow={selectedIndex + 1}
      aria-valuetext={`${shortDate(selected.day, true)}: ${format(selected.value)}${selected.current ? " (this run)" : ""}`}
      onKeyDown={event => {
        const next = { ArrowLeft: selectedIndex - 1, ArrowDown: selectedIndex - 1, ArrowRight: selectedIndex + 1, ArrowUp: selectedIndex + 1, Home: 0, End: last }[event.key];
        if (next === undefined) return;
        event.preventDefault(); setActive(true); choose(next);
      }}
      onBlur={() => setActive(false)}
      onPointerDown={event => {
        setActive(true);
        choose(nearest(event.clientX, event.currentTarget.getBoundingClientRect()));
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onPointerMove={event => { if (event.buttons) choose(nearest(event.clientX, event.currentTarget.getBoundingClientRect())); }}
      onPointerUp={() => setActive(false)}
      onPointerCancel={() => setActive(false)}>
      <svg viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
        {ticks.map(value => <line key={value} x1={LEFT} x2={width} y1={y(value)} y2={y(value)} className={progress.lineGrid} />)}
        {active ? <line x1={selectedX} x2={selectedX} y1={0} y2={height} className={weight.guide} /> : null}
        {points.length >= 2 ? <path d={path} className={weight.line} /> : null}
        {coords.map(([cx, cy], index) => <circle key={points[index].id} cx={cx} cy={cy}
          r={index === selectedIndex ? 5.5 : points[index].current ? 4 : 3}
          className={index === selectedIndex ? weight.selectedPoint : points[index].current ? weight.latestPoint : weight.point} />)}
      </svg>
      {ticks.map(value => <span key={value} className={weight.yTick} style={{ top: y(value) }} aria-hidden="true">{tick(value)}</span>)}
      {active ? <span className={weight.tooltip} aria-hidden="true" data-edge={selectedY < height / 2 ? "bottom" : "top"}
        style={{ left: selectedX, transform: `translateX(-${(selectedX / width * 100).toFixed(1)}%)` }}>
        <strong>{format(selected.value)}</strong> {shortDate(selected.day, withYear)}{selected.current ? " · this run" : ""}
      </span> : null}
    </div>
    <div className={weight.dateAxis} aria-hidden="true">
      <span style={{ left: LEFT }}>{shortDate(firstDay, withYear)}</span>
      {lastDay !== firstDay ? <span style={{ right: RIGHT }}>{shortDate(lastDay, withYear)}</span> : null}
    </div>
  </>;
}

/**
 * One bar per run, oldest first, from a zero baseline; this run in red. Bar
 * height is ``height(value)`` (speed for pace, so faster is taller) and the
 * selected bar carries its value. Tap a bar, or use the arrow keys.
 */
export function RunBarChart({ points, format, short, height, label, selectedIndex, onSelect }: {
  points: readonly RunChartPoint[];
  /** The full value, read out ("6 km"); ``short`` labels the bar ("6"). */
  format: (value: number) => string;
  short: (value: number) => string;
  height: (value: number) => number;
  label: string;
  selectedIndex: number;
  onSelect: (index: number) => void;
}) {
  const heights = points.map(point => height(point.value));
  const max = Math.max(...heights, Number.EPSILON);
  const last = points.length - 1;
  const selected = points[selectedIndex] ?? points[last];
  const withYear = points[0].day.slice(0, 4) !== points[last].day.slice(0, 4);
  // Every date when they fit; otherwise every other one, always keeping this run's.
  const labelEvery = points.length > 8 ? 2 : 1;
  return <div className={styles.bars} role="slider" tabIndex={0} aria-label={label}
    aria-valuemin={1} aria-valuemax={points.length} aria-valuenow={selectedIndex + 1}
    aria-valuetext={`${shortDate(selected.day, true)}: ${format(selected.value)}${selected.current ? " (this run)" : ""}`}
    onKeyDown={event => {
      const next = { ArrowLeft: selectedIndex - 1, ArrowDown: selectedIndex - 1, ArrowRight: selectedIndex + 1, ArrowUp: selectedIndex + 1, Home: 0, End: last }[event.key];
      if (next === undefined) return;
      event.preventDefault(); onSelect(Math.max(0, Math.min(last, next)));
    }}>
    {points.map((point, index) => <div key={point.id} className={styles.barColumn} data-current={point.current || undefined}
      data-selected={index === selectedIndex || undefined} onPointerDown={() => onSelect(index)}>
      {index === selectedIndex ? <span className={styles.barValue} aria-hidden="true">{short(point.value)}</span> : null}
      <span className={styles.bar} style={{ height: `${Math.max(4, heights[index] / max * 78)}%` }} aria-hidden="true" />
      <span className={styles.barDay} aria-hidden="true">{(last - index) % labelEvery === 0 || point.current ? shortDate(point.day, withYear) : ""}</span>
    </div>)}
  </div>;
}
