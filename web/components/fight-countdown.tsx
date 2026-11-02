"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { getFightCountdown } from "@/lib/fight-countdown";
import { formatWeight } from "@/lib/nutrition-bodyweight";
import { formatPlanFightDate } from "@/lib/plan-format";
import type { StructuredPlan } from "@/lib/types";

const COUNT_MS = 1100;

/** True once the element has scrolled into view (immediately where
 * IntersectionObserver is unavailable), so the animation plays when the athlete
 * actually sees the card rather than off-screen on load. Waits for `ready` so
 * it starts only once the plan's weeks (bar + starting count) have loaded. */
function useRevealed<T extends Element>(ready: boolean): [React.RefObject<T | null>, boolean] {
  const ref = useRef<T | null>(null);
  const [revealed, setRevealed] = useState(false);
  useEffect(() => {
    const node = ref.current;
    if (!ready || !node || revealed) return;
    if (typeof IntersectionObserver === "undefined") {
      const frame = requestAnimationFrame(() => setRevealed(true));
      return () => cancelAnimationFrame(frame);
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setRevealed(true);
          observer.disconnect();
        }
      },
      { threshold: 0.4 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [ready, revealed]);
  return [ref, revealed];
}

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
}

/** Ticks the day count down from the camp's length to today's value once the
 * card is revealed, in step with the bar's CSS fill. From reveal until the last
 * frame it shows the animated value (starting at `from`), so today's count
 * never flashes before the tick-down. Renders the final value on the server and
 * skips the animation under reduced motion. Plays once per mount. */
function useCountdownTick(target: number, from: number | null, revealed: boolean): number {
  const [animated, setAnimated] = useState<number | null>(null);
  const [done, setDone] = useState(false);
  const willAnimate = from !== null && from > target && !prefersReducedMotion();
  useLayoutEffect(() => {
    if (!revealed || done || !willAnimate || from === null) {
      return;
    }
    let frame = 0;
    const start = performance.now();
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / COUNT_MS);
      const eased = 1 - Math.pow(1 - t, 3);
      if (t < 1) {
        setAnimated(Math.round(from - (from - target) * eased));
        frame = requestAnimationFrame(step);
      } else {
        setDone(true);
      }
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target, from, revealed, done, willAnimate]);
  if (revealed && willAnimate && !done) {
    return animated ?? from;
  }
  return target;
}

/** Shown when the athlete gave no event name in camp setup. */
export const FIGHT_EVENT_FALLBACK = "Fight Camp";

function WeightGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9.5 7.5a2.5 2.5 0 1 1 5 0" />
      <path d="M6.6 7.5h10.8a1 1 0 0 1 1 .8l2 11a1 1 0 0 1-1 1.2H4.6a1 1 0 0 1-1-1.2l2-11a1 1 0 0 1 1-.8Z" />
      <path d="m10 13 2 2.5 2-2.5M12 15.5V18" />
    </svg>
  );
}

/**
 * Overview fight-camp countdown: the event, days to fight night, the target
 * fight weight, a gold camp-progress bar with the current week and phase, and
 * the next phase change. Renders nothing without a fight date (open plans) or
 * after fight day, so the caller can drop it in unconditionally.
 */
export function FightCountdown({
  fightDate,
  trainingDay,
  phase,
  plan,
  eventName,
  targetWeightKg,
}: {
  fightDate: string | null | undefined;
  trainingDay: string | null | undefined;
  phase?: string | null;
  plan?: StructuredPlan | null;
  /** Event/competition name from camp setup; falls back to "Fight Camp". */
  eventName?: string | null;
  /** Target fight weight in kg (never current bodyweight); hidden when unset. */
  targetWeightKg?: number | null;
}) {
  const countdown = getFightCountdown({ fightDate, trainingDay, phase, plan });
  // If the date arrived before the calendar, keep its already-visible count
  // stable when the weeks load instead of jumping back to the camp's start.
  const [initialCampDays] = useState(countdown?.campDays ?? null);
  const [ref, revealed] = useRevealed<HTMLElement>(countdown?.pct != null);
  const shown = useCountdownTick(countdown?.daysOut ?? 0, initialCampDays, revealed);
  if (!countdown) {
    return null;
  }

  const { daysOut, fightDateISO, pct, weekLabel, phaseLabel, nextLabel } = countdown;
  const fightDay = daysOut === 0;
  const meta = [weekLabel, phaseLabel].filter(Boolean).join(" · ");
  const label = fightDay ? "Fight day" : `${daysOut} ${daysOut === 1 ? "day" : "days"} to fight night`;
  const event = eventName?.trim() || FIGHT_EVENT_FALLBACK;
  const fightWeight = targetWeightKg != null && Number.isFinite(targetWeightKg) && targetWeightKg > 0
    ? formatWeight(targetWeightKg)
    : null;

  return (
    <section
      ref={ref}
      className="fight-countdown"
      data-revealed={revealed ? "true" : undefined}
      data-pending={initialCampDays !== null && pct !== null && !revealed ? "true" : undefined}
      aria-label={`Fight countdown: ${label}`}
    >
      <div className="fight-countdown-heading">
        <p className="fight-countdown-kicker">Upcoming fight</p>
        {/* Without a fight weight the event name takes the weight's slot on
            the countdown row, so the card stays compact. */}
        {fightWeight ? <h2 className="fight-countdown-event">{event}</h2> : null}
      </div>
      <div className="fight-countdown-stats">
        <div className="fight-countdown-count">
          {fightDay ? null : <span className="fight-countdown-number" aria-hidden="true">{shown}</span>}
          <div className="fight-countdown-count-copy">
            <p className="fight-countdown-days" aria-hidden="true">
              {fightDay ? "Fight day" : `${daysOut === 1 ? "day" : "days"} to fight`}
            </p>
            {/* "22 Nov 2026": the card drops the weekday to stay uncluttered. */}
            <p className="fight-countdown-date">{formatPlanFightDate(fightDateISO).replace(/^[A-Za-z]{3}\s+/, "")}</p>
          </div>
        </div>
        {fightWeight ? (
          <div className="fight-countdown-weight">
            <span className="fight-countdown-weight-icon"><WeightGlyph /></span>
            <div>
              <p className="fight-countdown-weight-label">Fight weight</p>
              <p className="fight-countdown-weight-value">{fightWeight}</p>
            </div>
          </div>
        ) : (
          <h2 className="fight-countdown-event fight-countdown-event-side">{event}</h2>
        )}
      </div>
      {meta ? <p className="fight-countdown-meta">{meta}</p> : null}
      {pct !== null ? (
        <div
          className="overview-progress-track fight-countdown-track"
          role="progressbar"
          aria-valuenow={Math.round(pct)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Camp progress"
        >
          <span className="overview-progress-fill fight-countdown-fill" style={{ width: `${pct}%` }} />
        </div>
      ) : null}
      {nextLabel ? (
        <p className="fight-countdown-next">
          <span className="fight-countdown-next-label">Next</span> {nextLabel}
        </p>
      ) : null}
    </section>
  );
}

/** Placeholder text block: shimmer sized by its (invisible) text. */
function Bone({ children }: { children: string }) {
  return <span className="skeleton skeleton-text fight-countdown-bone">{children}</span>;
}

/** Placeholder shown in the countdown's slot while the plan calendar loads
 * (like the XP card's skeleton), so Overview lays out once and the card fills
 * in place instead of popping in later. It reuses the card's own markup with
 * invisible stand-in text, so it is the card's exact size at every width. */
export function FightCountdownSkeleton() {
  return (
    <section className="fight-countdown fight-countdown-skeleton" aria-busy="true" aria-label="Fight countdown loading">
      <div className="fight-countdown-heading">
        <p className="fight-countdown-kicker"><Bone>Upcoming fight</Bone></p>
        <p className="fight-countdown-event"><Bone>Fight Camp</Bone></p>
      </div>
      <div className="fight-countdown-stats">
        <div className="fight-countdown-count">
          <span className="fight-countdown-number"><Bone>00</Bone></span>
          <div className="fight-countdown-count-copy">
            <p className="fight-countdown-days"><Bone>days to fight</Bone></p>
            <p className="fight-countdown-date"><Bone>00 Nov 0000</Bone></p>
          </div>
        </div>
      </div>
      <p className="fight-countdown-meta"><Bone>Week 0 of 0 · General prep</Bone></p>
      <div className="overview-progress-track fight-countdown-track" aria-hidden="true" />
      <p className="fight-countdown-next"><Bone>Next Specific prep starts in 00 days</Bone></p>
    </section>
  );
}
