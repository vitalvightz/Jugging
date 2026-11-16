import test from "node:test";
import assert from "node:assert/strict";

import { buildCompletionIndex } from "@/lib/camp-map";
import { buildPlanCalendar, calendarCaption } from "@/lib/plan-calendar";
import type { StructuredWeek, TodaySessionCompletionRecord } from "@/lib/types";

const strength = (id: string) => ({ session_id: id, title: "Strength", blocks: [{ display_name: "Squat" }] });

function datedWeeks(): StructuredWeek[] {
  return [
    {
      week_id: "wk-1",
      week_index: 1,
      days: [
        // Tue 27 Oct 2026, Thu 29 Oct, Sat 31 Oct (coach-led sparring).
        { date: "2026-10-27", sessions: [strength("s1")] },
        { date: "2026-10-29", sessions: [strength("s2")] },
        { date: "2026-10-31", today_card: { headline: "Coach-led sparring" }, sessions: [] },
      ],
    },
    {
      week_id: "wk-2",
      week_index: 2,
      days: [{ date: "2026-11-03", sessions: [strength("s3")] }],
    },
  ] as StructuredWeek[];
}

test("dated plans lay out Monday-first rows from the first to the last plan week", () => {
  const rows = buildPlanCalendar(datedWeeks(), { currentDayIso: "2026-10-29" });

  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((row) => row.label), ["W1", "W2"]);
  assert.equal(rows[0].cells.length, 7);
  // Mon 26 Oct precedes the first plan day, so it is outside the plan.
  assert.equal(rows[0].cells[0].iso, "2026-10-26");
  assert.equal(rows[0].cells[0].state, "outside");
  // Tuesday holds week 1 day 0.
  assert.equal(rows[0].cells[1].weekPos, 0);
  assert.equal(rows[0].cells[1].dayIndex, 0);
  assert.equal(rows[0].cells[1].state, "planned");
  // Wednesday falls between plan days: rest.
  assert.equal(rows[0].cells[2].state, "rest");
  assert.equal(rows[0].cells[3].isToday, true);
  assert.equal(rows[0].cells[5].state, "coach");
  // Week 2 ends on Tue 03 Nov; the rest of the row is outside.
  assert.equal(rows[1].cells[1].weekPos, 1);
  assert.equal(rows[1].cells[6].state, "outside");
});

test("live completions mark days done or missed, but never missed without them", () => {
  const completions = [
    { training_day: "2026-10-27", session_id: "s1", status: "done" },
  ] as TodaySessionCompletionRecord[];
  const options = {
    completionIndex: buildCompletionIndex(completions),
    currentDayIso: "2026-11-01",
  };
  const rows = buildPlanCalendar(datedWeeks(), options);
  assert.equal(rows[0].cells[1].state, "done");
  assert.equal(rows[0].cells[3].state, "missed");
  assert.equal(rows[1].cells[1].state, "planned");

  const withoutIndex = buildPlanCalendar(datedWeeks(), { currentDayIso: "2026-11-01" });
  assert.equal(withoutIndex[0].cells[3].state, "planned");
});

test("weekday-only open plans get one row per week, dated from the anchor", () => {
  const weeks = [1, 2, 3, 4].map((index) => ({
    week_id: `wk-${index}`,
    week_index: index,
    days: [
      { weekday: "Tue", sessions: [strength(`t${index}`)] },
      { weekday: "Friday", sessions: [strength(`f${index}`)] },
    ],
  })) as StructuredWeek[];

  const anchored = buildPlanCalendar(weeks, { anchorIso: "2026-10-26", currentDayIso: "2026-11-03" });
  assert.equal(anchored.length, 4);
  assert.equal(anchored[1].cells[1].iso, "2026-11-03");
  assert.equal(anchored[1].cells[1].isToday, true);
  assert.equal(anchored[1].cells[4].dayIndex, 1);
  assert.equal(anchored[1].cells[0].state, "rest");

  const unanchored = buildPlanCalendar(weeks, { currentDayIso: "2026-11-03", currentWeekPos: 2 });
  assert.equal(unanchored[2].cells[1].iso, null);
  assert.equal(unanchored[2].cells[1].dayNumber, null);
  assert.equal(unanchored[2].cells[1].isToday, true);
  assert.equal(unanchored[0].cells[1].isToday, false);
});


test("rest and off are inert calendar days, but prescribed recovery remains openable", () => {
  const weeks = [{ week_index: 1, days: [
    { date: "2026-11-16", day_type: "rest", sessions: [], today_card: { headline: "Rest day" } },
    { date: "2026-11-17", day_type: "moderate", sessions: [], today_card: { headline: "Off day" } },
    { date: "2026-11-18", day_type: "recovery", sessions: [], today_card: {
      headline: "Prescribed recovery", primary_warning: "Review recovery restrictions",
    } },
  ] }] as StructuredWeek[];
  const days = buildPlanCalendar(weeks)[0].cells;
  assert.equal(days[0].state, "rest");
  assert.equal(days[1].state, "rest");
  assert.equal(days[2].state, "planned");
});

test("caption spans the months the plan covers", () => {
  assert.equal(calendarCaption(buildPlanCalendar(datedWeeks())), "Oct – Nov 2026");
  assert.equal(calendarCaption([]), null);
});
