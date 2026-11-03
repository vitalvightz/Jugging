"use client";

import { useEffect, useRef, useState } from "react";

import { formatAppDate } from "@/lib/date-format";
import { type IndexedBodyweightEntry, getBodyweightTimestamp, shiftDate } from "@/lib/nutrition-bodyweight";
import progress from "./training-progress.module.css";
import styles from "./weight-tracker.module.css";

const kg = (value: number) => `${value.toFixed(1)} kg`;
const dayStart = (date: string) => Date.parse(`${date}T00:00`);
const STEPS = [0.2, 0.5, 1, 2, 5, 10, 20, 50];
// Room for the y labels on the left, and for the markers at the edges.
const LEFT = 40, RIGHT = 8, TOP = 6, BOTTOM = 6, PAD = 1 / 3;

/** "26 Oct" (or "26 Oct 2025" when the window spans years), from the app's canonical format. */
export const shortDate = (date: string, withYear = false) => formatAppDate(date).split(" ").slice(1, withYear ? 4 : 3).join(" ");

/**
 * Logged weights on a calendar time axis ending today: every day in the range
 * keeps its place, so days without a weigh-in stay visible as gaps. Only
 * recorded weights are drawn, joined in order; nothing is smoothed or filled
 * in. The y-axis fits the weights alone; a target outside it is named below
 * the chart instead of stretching it. The plot is the control: tap or drag
 * across it, or use the arrow keys when focused; the tooltip shows only while
 * that lasts.
 */
export function WeightTrendChart({ trend, rangeStart, today, targetKg, selectedIndex, onSelect }: {
  trend: readonly IndexedBodyweightEntry[];
  /** First day of the window (YYYY-MM-DD); null for all history. */
  rangeStart: string | null;
  /** Last day of the window: today, local. */
  today: string;
  targetKg: number | null;
  selectedIndex: number;
  onSelect: (index: number) => void;
}) {
  const plot = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 600, height: 150 });
  const [active, setActive] = useState(false);
  useEffect(() => {
    const element = plot.current;
    if (!element) return;
    const observer = new ResizeObserver(entries => {
      const { width, height } = entries[0].contentRect;
      setSize({ width: Math.max(LEFT + RIGHT + 24, width), height: Math.max(TOP + BOTTOM + 24, height) });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const { width, height } = size;

  // Time domain: whole local days from the window's first day to the end of today.
  const firstDay = rangeStart ?? trend[0].date;
  const start = dayStart(firstDay), end = dayStart(shiftDate(today, 1));
  const x = (time: number) => LEFT + (time - start) / (end - start) * (width - LEFT - RIGHT);

  // Four gridlines at the smallest round kg step that fits the recorded
  // weights alone, centred on them; the scale runs a third of a step past the
  // outer gridlines, so readings near them keep some padding.
  const weights = trend.map(entry => entry.weight_kg);
  const low = Math.min(...weights), high = Math.max(...weights);
  const base = (step: number) => Math.round(((low + high) / 2 - 1.5 * step) / step) * step;
  const fits = (s: number) => base(s) - PAD * s <= low + 1e-9 && base(s) + (3 + PAD) * s >= high - 1e-9;
  const step = STEPS.find(fits) ?? STEPS[STEPS.length - 1];
  const ticks = [3, 2, 1, 0].map(n => base(step) + n * step);
  const floor = ticks[3] - PAD * step, ceiling = ticks[0] + PAD * step;
  const y = (value: number) => TOP + (ceiling - value) / (ceiling - floor) * (height - TOP - BOTTOM);
  const targetInView = targetKg != null && targetKg >= floor && targetKg <= ceiling;

  const points = trend.map(entry => [x(getBodyweightTimestamp(entry)), y(entry.weight_kg)] as const);
  const path = points.map(([px, py], index) => `${index ? "L" : "M"}${px.toFixed(2)},${py.toFixed(2)}`).join(" ");
  const latestIndex = trend.length - 1;
  const selected = trend[selectedIndex];
  const choose = (index: number) => onSelect(Math.max(0, Math.min(latestIndex, index)));
  const nearest = (clientX: number, bounds: DOMRect) => {
    const px = (clientX - bounds.left) / bounds.width * width;
    return points.reduce((best, [pointX], index) => Math.abs(pointX - px) < Math.abs(points[best][0] - px) ? index : best, 0);
  };
  const [selectedX, selectedY] = points[selectedIndex] ?? points[latestIndex];

  // Date ticks: the window's first day, its middle when there's room, and today.
  const withYear = firstDay.slice(0, 4) !== today.slice(0, 4);
  const spanDays = Math.round((end - start) / 86_400_000);
  const middleDay = spanDays >= 3 ? shiftDate(firstDay, Math.floor(spanDays / 2)) : null;
  const radius = trend.length > 40 ? 2 : 3;

  return <>
    <div ref={plot} className={`${progress.linePlot} ${styles.plot}`} role="slider" tabIndex={0} aria-label="Inspect logged weight"
      aria-valuemin={1} aria-valuemax={trend.length} aria-valuenow={selectedIndex + 1}
      aria-valuetext={selected ? `${formatAppDate(selected.date)}: ${kg(selected.weight_kg)}` : undefined}
      onKeyDown={event => {
        const next = { ArrowLeft: selectedIndex - 1, ArrowDown: selectedIndex - 1, ArrowRight: selectedIndex + 1, ArrowUp: selectedIndex + 1, Home: 0, End: latestIndex }[event.key];
        if (next === undefined) return;
        event.preventDefault(); setActive(true); choose(next);
      }}
      onBlur={() => setActive(false)}
      onPointerDown={event => {
        setActive(true);
        choose(nearest(event.clientX, event.currentTarget.getBoundingClientRect()));
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={event => {
        if (!event.buttons) return;
        choose(nearest(event.clientX, event.currentTarget.getBoundingClientRect()));
      }}
      onPointerUp={() => setActive(false)}
      onPointerCancel={() => setActive(false)}>
      <svg viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
        {ticks.map(value => <line key={value} x1={LEFT} x2={width} y1={y(value)} y2={y(value)} className={progress.lineGrid} />)}
        {targetInView ? <line x1={LEFT} x2={width} y1={y(targetKg)} y2={y(targetKg)} className={styles.targetLine} /> : null}
        {active ? <line x1={selectedX} x2={selectedX} y1={0} y2={height} className={styles.guide} /> : null}
        {trend.length >= 2 ? <path d={path} className={styles.line} /> : null}
        {points.map(([cx, cy], index) => <circle key={trend[index].sourceIndex} cx={cx} cy={cy}
          r={index === latestIndex ? 5.5 : index === selectedIndex ? 4.5 : radius}
          className={index === latestIndex ? styles.latestPoint : index === selectedIndex ? styles.selectedPoint : styles.point} />)}
      </svg>
      {ticks.map(value => <span key={value} className={styles.yTick} style={{ top: y(value) }} aria-hidden="true">{value.toFixed(1)}</span>)}
      {targetInView ? <span className={styles.targetLabel} style={{ top: y(targetKg) }} aria-hidden="true">Target</span> : null}
      {active && selected ? (
        // Pinned to the half of the plot away from the point, and slid along
        // so it stays inside the plot at either edge.
        <span className={styles.tooltip} aria-hidden="true" data-edge={selectedY < height / 2 ? "bottom" : "top"}
          style={{ left: selectedX, transform: `translateX(-${(selectedX / width * 100).toFixed(1)}%)` }}>
          <strong>{kg(selected.weight_kg)}</strong> {shortDate(selected.date)}{selected.time ? ` · ${selected.time}` : ""}
        </span>
      ) : null}
    </div>
    <div className={styles.dateAxis} aria-hidden="true">
      <span style={{ left: LEFT }}>{shortDate(firstDay, withYear)}</span>
      {middleDay ? <span style={{ left: x(dayStart(middleDay) + 43_200_000), transform: "translateX(-50%)" }}>{shortDate(middleDay, withYear)}</span> : null}
      <span style={{ right: RIGHT }}>{shortDate(today, withYear)}</span>
    </div>
    {targetKg != null && !targetInView ? (
      <p className={styles.targetOutside}>Target {kg(targetKg)} {targetKg < floor ? "↓" : "↑"}</p>
    ) : null}
  </>;
}
