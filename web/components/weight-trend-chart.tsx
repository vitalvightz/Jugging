"use client";

import { useEffect, useRef, useState } from "react";

import { formatAppDate } from "@/lib/date-format";
import { type IndexedBodyweightEntry, getBodyweightTimestamp } from "@/lib/nutrition-bodyweight";
import type { NutritionBodyweightTrendPoint } from "@/lib/types";
import progress from "./training-progress.module.css";
import styles from "./weight-tracker.module.css";

const kg = (value: number) => `${value.toFixed(1)} kg`;

/**
 * Weigh-ins as dots under the smoothed trend line, in the Exercise Progress
 * style. The plot is the control: tap a weigh-in, or use the arrow keys when
 * focused. The target is a dashed line when it sits close enough to the data to
 * keep the trend readable. Without a server trend the weigh-ins are joined instead.
 */
export function WeightTrendChart({ trend, trendLine, targetKg, selectedIndex, onSelect }: {
  trend: readonly IndexedBodyweightEntry[];
  trendLine: readonly NutritionBodyweightTrendPoint[];
  targetKg: number | null;
  selectedIndex: number;
  onSelect: (index: number) => void;
}) {
  const plot = useRef<HTMLDivElement>(null);
  const [plotWidth, setPlotWidth] = useState(600);
  useEffect(() => {
    const element = plot.current;
    if (!element) return;
    const observer = new ResizeObserver(entries => setPlotWidth(Math.max(24, entries[0].contentRect.width)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const values = [...trend.map(entry => entry.weight_kg), ...trendLine.map(point => point.trend_weight_kg)];
  const low = Math.min(...values), high = Math.max(...values);
  // Keep a cut's target in view; one far enough off to flatten the trend is noted instead.
  const reach = Math.max(5, (high - low) * 2);
  const target = targetKg != null && targetKg >= low - reach && targetKg <= high + reach ? targetKg : null;
  const lo = target != null ? Math.min(low, target) : low;
  const hi = target != null ? Math.max(high, target) : high;
  const padding = Math.max((hi - lo) * .15, .5);
  const floor = lo - padding, ceiling = hi + padding;
  const times = trend.map(getBodyweightTimestamp);
  const start = times[0], span = times[times.length - 1] - start;
  const x = (index: number) => span ? 12 + (times[index] - start) / span * (plotWidth - 24) : plotWidth / 2;
  const y = (value: number) => 180 - (value - floor) / (ceiling - floor) * 160;
  // Each trend point sits on the weigh-in the server chose for that day.
  const trendX = (point: NutritionBodyweightTrendPoint) => {
    const index = trend.findIndex(entry => entry.date === point.date && Math.abs(entry.weight_kg - point.scale_weight_kg) < 0.05);
    return x(index >= 0 ? index : trend.findIndex(entry => entry.date === point.date));
  };
  const line = trendLine.length >= 2
    ? trendLine.map(point => [trendX(point), y(point.trend_weight_kg)] as const)
    : trend.map((entry, index) => [x(index), y(entry.weight_kg)] as const);
  const path = line.map(([px, py], index) => `${index ? "L" : "M"}${px.toFixed(2)},${py.toFixed(2)}`).join(" ");
  const selected = trend[selectedIndex];
  const choose = (index: number) => onSelect(Math.max(0, Math.min(trend.length - 1, index)));
  const nearest = (clientX: number, bounds: DOMRect) => {
    const px = (clientX - bounds.left) / bounds.width * plotWidth;
    return trend.reduce((best, _, index) => Math.abs(x(index) - px) < Math.abs(x(best) - px) ? index : best, 0);
  };

  return <>
    <div ref={plot} className={progress.linePlot} role="slider" tabIndex={0} aria-label="Inspect logged weight"
      aria-valuemin={1} aria-valuemax={trend.length} aria-valuenow={selectedIndex + 1}
      aria-valuetext={selected ? `${formatAppDate(selected.date)}: ${kg(selected.weight_kg)}` : undefined}
      onKeyDown={event => {
        const next = { ArrowLeft: selectedIndex - 1, ArrowDown: selectedIndex - 1, ArrowRight: selectedIndex + 1, ArrowUp: selectedIndex + 1, Home: 0, End: trend.length - 1 }[event.key];
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
      <svg viewBox={`0 0 ${plotWidth} 200`} preserveAspectRatio="none" aria-hidden="true">
        {[20, 100, 180].map(value => <line key={value} x1="0" x2={plotWidth} y1={value} y2={value} className={progress.lineGrid} />)}
        {target != null ? <line x1="0" x2={plotWidth} y1={y(target)} y2={y(target)} className={styles.targetLine} /> : null}
        <path d={`${path} L${line[line.length - 1][0].toFixed(2)},180 L${line[0][0].toFixed(2)},180 Z`} className={progress.area} />
        <path d={path} className={trendLine.length >= 2 ? styles.trendLine : progress.line} />
        {trend.map((entry, index) => <circle key={`${entry.sourceIndex}`} cx={x(index)} cy={y(entry.weight_kg)}
          r={index === selectedIndex ? 6 : trend.length > 40 ? 2.5 : 4} className={index === selectedIndex ? progress.activePoint : progress.point} />)}
      </svg>
      {[ceiling, (ceiling + floor) / 2, floor].map((value, index) => <span key={index} className={progress.yTick} style={{ top: `${10 + index * 40}%` }} aria-hidden="true">{kg(value)}</span>)}
      {target != null ? <span className={styles.targetLabel} style={{ top: `${y(target) / 2}%` }} aria-hidden="true">Target {kg(target)}</span> : null}
    </div>
    <div className={progress.dateAxis}><span>{formatAppDate(trend[0].date)}</span><span>{formatAppDate(trend[trend.length - 1].date)}</span></div>
    {trendLine.length >= 2 ? <p className={styles.legend} aria-hidden="true">
      <span><i className={styles.legendDot} />Logged weights</span><span><i className={styles.legendLine} />Trend (estimate)</span>
    </p> : null}
    {targetKg != null && target == null ? <p className={progress.note}>Target {kg(targetKg)} is {targetKg < low ? "below" : "above"} this range.</p> : null}
  </>;
}
