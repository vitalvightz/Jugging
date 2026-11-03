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
const LEFT = 44, RIGHT = 8, TOP = 10, BOTTOM = 10;

/**
 * Logged weights on a calendar time axis: the window covers every day in the
 * range, so days without a weigh-in stay visible as gaps. Only recorded weights
 * are drawn, joined in order; nothing is smoothed or filled in. The plot is the
 * control: tap a weigh-in, or use the arrow keys when focused. A target close
 * enough to the data to keep it readable is a dashed line.
 */
export function WeightTrendChart({ trend, days, targetKg, selectedIndex, inspecting, onSelect }: {
  trend: readonly IndexedBodyweightEntry[];
  /** Calendar days in the window, ending on the latest weigh-in's day; null for all history. */
  days: number | null;
  targetKg: number | null;
  selectedIndex: number;
  /** Whether a weigh-in was picked, which shows its tooltip. */
  inspecting: boolean;
  onSelect: (index: number) => void;
}) {
  const plot = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 600, height: 168 });
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

  // Time domain: whole local days from the window's first day to the end of the latest day.
  const firstDay = days ? shiftDate(trend[trend.length - 1].date, 1 - days) : trend[0].date;
  const lastDay = trend[trend.length - 1].date;
  const start = dayStart(firstDay), end = dayStart(shiftDate(lastDay, 1));
  const x = (time: number) => LEFT + (time - start) / (end - start) * (width - LEFT - RIGHT);

  // Three evenly stepped gridlines centred on the data, at a round kg step.
  const weights = trend.map(entry => entry.weight_kg);
  const low = Math.min(...weights), high = Math.max(...weights);
  // Keep a cut's target in view; one far enough off to flatten the data is noted instead.
  // The chart is short, so the target may add at most ~half again to the data's span.
  const reach = Math.max(1.5, (high - low) * 1.5);
  const target = targetKg != null && targetKg >= low - reach && targetKg <= high + reach ? targetKg : null;
  const lo = Math.min(low, target ?? low), hi = Math.max(high, target ?? high);
  const middleTick = (step: number) => Math.round((lo + hi) / 2 / step) * step;
  const step = STEPS.find(s => middleTick(s) - s <= lo + 1e-9 && middleTick(s) + s >= hi - 1e-9) ?? STEPS[STEPS.length - 1];
  const ticks = [middleTick(step) + step, middleTick(step), middleTick(step) - step];
  const y = (value: number) => TOP + (ticks[0] - value) / (2 * step) * (height - TOP - BOTTOM);

  const points = trend.map(entry => [x(getBodyweightTimestamp(entry)), y(entry.weight_kg)] as const);
  const path = points.map(([px, py], index) => `${index ? "L" : "M"}${px.toFixed(2)},${py.toFixed(2)}`).join(" ");
  const latestIndex = trend.length - 1;
  const selected = trend[selectedIndex];
  const choose = (index: number) => onSelect(Math.max(0, Math.min(latestIndex, index)));
  const nearest = (clientX: number, bounds: DOMRect) => {
    const px = (clientX - bounds.left) / bounds.width * width;
    return points.reduce((best, [pointX], index) => Math.abs(pointX - px) < Math.abs(points[best][0] - px) ? index : best, 0);
  };

  // Date ticks: first and last day of the window, and its middle when there's room.
  const short = (date: string) => formatAppDate(date).split(" ").slice(1, firstDay.slice(0, 4) === lastDay.slice(0, 4) ? 3 : 4).join(" ");
  const spanDays = Math.round((end - start) / 86_400_000);
  const middleDay = spanDays >= 3 ? shiftDate(firstDay, Math.floor(spanDays / 2)) : null;
  const radius = trend.length > 40 ? 2 : 3.5;

  return <>
    <div ref={plot} className={`${progress.linePlot} ${styles.plot}`} role="slider" tabIndex={0} aria-label="Inspect logged weight"
      aria-valuemin={1} aria-valuemax={trend.length} aria-valuenow={selectedIndex + 1}
      aria-valuetext={selected ? `${formatAppDate(selected.date)}: ${kg(selected.weight_kg)}` : undefined}
      onKeyDown={event => {
        const next = { ArrowLeft: selectedIndex - 1, ArrowDown: selectedIndex - 1, ArrowRight: selectedIndex + 1, ArrowUp: selectedIndex + 1, Home: 0, End: latestIndex }[event.key];
        if (next === undefined) return;
        event.preventDefault(); choose(next);
      }}
      onPointerDown={event => {
        choose(nearest(event.clientX, event.currentTarget.getBoundingClientRect()));
        if (event.pointerType === "mouse") event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={event => {
        if (event.buttons !== 1 || event.pointerType !== "mouse") return;
        choose(nearest(event.clientX, event.currentTarget.getBoundingClientRect()));
      }}>
      <svg viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
        {ticks.map(value => <line key={value} x1={LEFT} x2={width} y1={y(value)} y2={y(value)} className={progress.lineGrid} />)}
        {target != null ? <line x1={LEFT} x2={width} y1={y(target)} y2={y(target)} className={styles.targetLine} /> : null}
        {trend.length >= 2 ? <path d={path} className={styles.line} /> : null}
        {points.map(([cx, cy], index) => <circle key={trend[index].sourceIndex} cx={cx} cy={cy}
          r={index === latestIndex || index === selectedIndex ? 5 : radius}
          className={index === latestIndex ? styles.latestPoint : index === selectedIndex ? styles.selectedPoint : styles.point} />)}
      </svg>
      {ticks.map(value => <span key={value} className={styles.yTick} style={{ top: y(value) }} aria-hidden="true">{value.toFixed(1)}</span>)}
      {target != null ? <span className={styles.targetLabel} style={{ top: y(target) }} aria-hidden="true">Target {kg(target)}</span> : null}
      {inspecting && selected ? (
        <span className={styles.tooltip} aria-hidden="true" data-below={points[selectedIndex][1] < 44 || undefined}
          style={{ left: Math.min(Math.max(points[selectedIndex][0], LEFT + 40), width - 40), top: points[selectedIndex][1] }}>
          <strong>{kg(selected.weight_kg)}</strong> {short(selected.date)}{selected.time ? ` · ${selected.time}` : ""}
        </span>
      ) : null}
    </div>
    <div className={styles.dateAxis} aria-hidden="true">
      <span style={{ left: LEFT }}>{short(firstDay)}</span>
      {middleDay ? <span style={{ left: x(dayStart(middleDay) + 43_200_000), transform: "translateX(-50%)" }}>{short(middleDay)}</span> : null}
      <span style={{ right: RIGHT }}>{short(lastDay)}</span>
    </div>
    {targetKg != null && target == null ? <p className={progress.note}>Target {kg(targetKg)} is {targetKg < low ? "below" : "above"} this range.</p> : null}
  </>;
}
