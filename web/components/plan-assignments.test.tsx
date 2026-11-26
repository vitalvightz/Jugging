import assert from "node:assert/strict";
import test from "node:test";
import "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { PlanAssignments } from "./plan-assignments";
import type { CoachPlan } from "@/lib/api-schema.generated";
import { blankDose } from "@/lib/custom-plan-dose";

const original: CoachPlan = {
  id: "assignment", name: "Coach week", kind: "self_directed", schedule_mode: "weekly", start_date: null, end_date: null, fighter_id: "fighter",
  coach_name: "Coach Sam", assigned_at: "2026-11-16T12:00:00Z", assignment_status: "pending",
  created_at: "2026-11-16T12:00:00Z", updated_at: "2026-11-16T12:00:00Z", published_plan_id: null, incomplete: false,
  sessions: [{ id: "squat-day", name: "Strength", scheduled_date: null, weekday: "Monday", session_type: "strength_power",
    exercises: [{ id: "squat", origin: "manual", name: "Squat", dose: { ...blankDose(), sets: 3, reps: 5 }, incomplete: false,
      exercise_type: "strength", equipment: null, notes: "Use rack", media_id: null, community_id: null, community_revision_id: null, coaching_cues: ["Brace"], stop_rules: ["Stop if painful"] }] }],
};
async function mount(mode: "activate" | "decline" | "overlap" | "denied") {
  const host = document.createElement("div"); document.body.append(host);
  const root = createRoot(host), before = globalThis.fetch;
  const writes: { path: string; body: unknown }[] = [];
  let row = { ...original }, activated = 0;
  globalThis.fetch = async (input, init) => {
    if (init?.method === "POST") {
      const body = init.body ? JSON.parse(String(init.body)) : null;
      const path = String(input); writes.push({ path, body });
      if (mode === "denied") return Response.json({ detail: "Connection ended" }, { status: 409 });
      if (mode === "overlap" && !body?.overlap_action) return Response.json({ detail: { code: "active_plan_overlap", message: "Choose pause or replace" } }, { status: 409 });
      row = { ...row, assignment_status: path.endsWith("decline") ? "declined" : "active" };
      return Response.json(row);
    }
    return Response.json([row]);
  };
  await act(async () => root.render(<PlanAssignments token="token" onActivated={() => { activated++; }} />));
  const click = async (text: string) => {
    const button = [...document.querySelectorAll("button")].find(el => el.textContent?.trim() === text);
    assert.ok(button, `Missing button ${text}`); await act(async () => button.click());
  };
  return { host, click, writes, activated: () => activated, close() { act(() => root.unmount()); host.remove(); globalThis.fetch = before; } };
}

test("fighter previews the full prescription before activation", async () => {
  const ui = await mount("activate");
  try {
    assert.match(ui.host.textContent!, /Coach Sam/); assert.match(ui.host.textContent!, /Pending/);
    assert.match(ui.host.textContent!, /Your plan stays the same until you tap Activate/);
    assert.equal(ui.host.querySelectorAll("button:not([aria-label='Dismiss tip'])").length, 1); assert.equal(ui.writes.length, 0);
    await ui.click("Preview Plan");
    for (const text of [/Monday/, /Squat/, /3 × 5/, /Use rack/, /Brace/, /Stop if painful/]) assert.match(ui.host.textContent!, text);
    await ui.click("Activate plan");
    assert.equal(ui.writes.length, 1); assert.match(ui.writes[0].path, /assignment\/activate$/);
    assert.equal(ui.activated(), 1); assert.match(ui.host.textContent!, /Active/);
  } finally { ui.close(); }
});

test("fighter can decline without activating", async () => {
  const ui = await mount("decline");
  try {
    await ui.click("Preview Plan"); await ui.click("Decline");
    assert.equal(ui.activated(), 0);
    assert.equal(ui.writes.length, 1); assert.match(ui.writes[0].path, /assignment\/decline$/);
    assert.equal(ui.host.textContent, "", "declined assignments leave the active/pending list");
    assert.equal([...ui.host.querySelectorAll("button")].some(el => el.textContent === "Activate plan"), false);
  } finally { ui.close(); }
});

test("revoked/changed access keeps the pending assignment and shows the error", async () => {
  const ui = await mount("denied");
  try {
    await ui.click("Preview Plan"); await ui.click("Activate plan");
    assert.equal(ui.activated(), 0); assert.match(ui.host.textContent!, /Pending/);
    assert.match(ui.host.querySelector('[role="alert"]')!.textContent!, /connection may have ended/);
  } finally { ui.close(); }
});

test("overlap requires the existing plan-switch dialog before retrying", async () => {
  const ui = await mount("overlap");
  try {
    await ui.click("Preview Plan"); await ui.click("Activate plan");
    assert.equal(ui.activated(), 0); assert.ok(document.querySelector('[role="dialog"]'));
    const confirm = [...document.querySelectorAll("button")].find(button => /Make.*active|Switch.*plan|Activate|Pause.*switch/i.test(button.textContent ?? "") && button.closest('[role="dialog"]'));
    assert.ok(confirm); await act(async () => confirm.click());
    assert.equal(ui.activated(), 1); assert.deepEqual(ui.writes[1].body, { overlap_action: "pause" });
  } finally { ui.close(); }
});

test("reports when the read settles, and a revisit shows the last rows at once", async () => {
  const before = globalThis.fetch;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  globalThis.fetch = async () => { await gate; return Response.json([{ ...original, id: "remembered" }]); };
  const first = document.createElement("div"); document.body.append(first);
  const firstRoot = createRoot(first);
  let settled = 0;
  try {
    await act(async () => firstRoot.render(<PlanAssignments token="recall-token" onActivated={() => undefined} onSettled={() => { settled++; }} />));
    assert.equal(settled, 0, "not settled while the read is in flight");
    assert.equal(first.textContent, "");
    await act(async () => { release(); await gate; });
    assert.equal(settled, 1);
    assert.match(first.textContent!, /Coach week/);
    act(() => firstRoot.unmount());

    // The next visit renders the remembered rows in its very first frame.
    globalThis.fetch = () => new Promise<Response>(() => undefined);
    const second = document.createElement("div"); document.body.append(second);
    const secondRoot = createRoot(second);
    act(() => secondRoot.render(<PlanAssignments token="recall-token" onActivated={() => undefined} />));
    assert.match(second.textContent!, /Coach week/);
    act(() => secondRoot.unmount()); second.remove();
  } finally { first.remove(); globalThis.fetch = before; }
});
