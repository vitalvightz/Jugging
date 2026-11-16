import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { AssignedProgress } from "./coach-plans";
import type { CoachPlan, CoachPlannedSession } from "@/lib/api-schema.generated";
import type { Fighter } from "@/lib/coach";

const plan = { published_plan_id: "plan" } as CoachPlan;
const fighter: Fighter = {
  id: "fighter", connection_id: "connection", name: "Fighter", avatar_url: null, sport: null,
  today: "2026-11-15", timezone: "UTC", fight_date: null, plan_id: "plan", plan_name: "Coach week",
  share_recovery: false, share_injuries: false, share_weight: false, schedule: [], sessions: [], recovery: null, injuries: null,
};
const schedule: CoachPlannedSession[] = [
  { id: "strength", plan_id: "plan", day: "2026-11-09", title: "Strength", type: "strength_power", blocks: [] },
  { id: "skill", plan_id: "plan", day: "2026-11-15", title: "Skill", type: "skill", blocks: [] },
];
function result(statuses: string[], planned = schedule, extra: Fighter["sessions"] = []) {
  const sessions = statuses.map((status, index) => ({
    id: `record-${index}`, session_id: schedule[index].id, plan_id: "plan", day: schedule[index].day,
    type: "training", status, source: "unlxck", rpe: 6, recorded_at: `${schedule[index].day}T12:00:00Z`,
  })).reverse();
  const html = renderToStaticMarkup(<AssignedProgress plan={plan} fighter={{ ...fighter, sessions: [...extra, ...sessions] }} schedule={planned} />);
  return new JSDOM(html).window.document;
}

test("a fully completed week shows visual completion and a compact latest result", () => {
  const doc = result(["done", "done"]);
  assert.equal(doc.querySelector("progress")!.value, 100);
  assert.equal(doc.querySelector("progress")!.getAttribute("aria-valuetext"), "2 of 2 sessions complete, 100%");
  assert.match(doc.body.textContent!, /All sessions complete this week/);
  assert.match(doc.body.textContent!, /Latest: 15 Nov · Done · RPE 6/);
  assert.doesNotMatch(doc.body.textContent!, /No upcoming|scheduled sessions completed/);
});

test("modified sessions count toward completion while remaining visible as modified", () => {
  const doc = result(["done", "modified"]);
  assert.equal(doc.querySelector("progress")!.value, 100);
  assert.match(doc.querySelector("dl")!.textContent!, /Modified1Skipped0/);
  assert.match(doc.body.textContent!, /All sessions complete this week/);
  assert.match(doc.querySelector("details")!.textContent!, /Skill · Modified/);
});

test("skipped sessions keep the completion bar partial and never claim a complete week", () => {
  const doc = result(["done", "skipped"]);
  assert.equal(doc.querySelector("progress")!.value, 50);
  assert.match(doc.querySelector("dl")!.textContent!, /Modified0Skipped1/);
  assert.match(doc.body.textContent!, /1 session skipped this week/);
  assert.doesNotMatch(doc.body.textContent!, /All sessions complete/);
});

test("an empty schedule shows no invented 100 percent completion", () => {
  const doc = result([], []);
  assert.equal(doc.querySelector("progress")!.value, 0);
  assert.match(doc.body.textContent!, /No sessions scheduled this week/);
  assert.doesNotMatch(doc.body.textContent!, /100%|All sessions complete/);
});

test("unrecorded scheduled sessions and records from another plan cannot count as completed", () => {
  const unrelated = { id: "other", session_id: "strength", plan_id: "another-plan", day: "2026-11-09", type: "strength", status: "done", source: "unlxck", recorded_at: "2026-11-09T12:00:00Z" };
  const doc = result([], schedule, [unrelated]);
  assert.equal(doc.querySelector("progress")!.value, 0);
  assert.match(doc.body.textContent!, /1 upcoming session this week/);
  assert.doesNotMatch(doc.body.textContent!, /All sessions complete/);
});
