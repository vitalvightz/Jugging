import { test } from "node:test";
import assert from "node:assert/strict";
import { planRelevance, prioritiseForMode, rosterPhysicalCompleted, sessionRelevance, trainingRelevance, useCoachMode, validCoachMode } from "./coach-mode";
import type { CoachSession } from "./coach";
import { window } from "../components/test-dom";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import type { TrainingPick } from "./training-disciplines";

test("coach modes default safely", () => {
  assert.equal(validCoachMode(null), "sc");
  assert.equal(validCoachMode("invalid"), "sc");
  assert.equal(validCoachMode("technical"), "technical");
});

test("plan type classification keeps mixed, recovery and legacy shared", () => {
  for (const type of ["skill", "sparring"]) assert.equal(planRelevance(type), "technical");
  for (const type of ["strength_power", "conditioning"]) assert.equal(planRelevance(type), "sc");
  for (const type of ["mixed", "recovery", "rest", "", "unknown", null]) assert.equal(planRelevance(type), "shared");
});

test("logged work respects structured discipline classifications", () => {
  const pick = (activity: string, sport: string | null = null, session_type: string | null = null) =>
    ({ activity, sport, session_type } as TrainingPick);
  assert.equal(trainingRelevance(pick("boxing", "boxing", "pads")), "technical");
  assert.equal(trainingRelevance(pick("other", "bjj", "rolling")), "technical");
  assert.equal(trainingRelevance(pick("other", "mma", "grappling")), "technical");
  assert.equal(trainingRelevance(pick("other", "wrestling", "live")), "technical");
  assert.equal(trainingRelevance(pick("other", "muay_thai", "clinch")), "technical");
  assert.equal(trainingRelevance(pick("strength")), "sc");
  assert.equal(trainingRelevance(pick("roadwork")), "sc");
  assert.equal(trainingRelevance(pick("boxing", "bjj", "rolling")), "shared");
  assert.equal(trainingRelevance(pick("other")), "shared");
});

test("relevance order is stable and never drops opposite-mode items", () => {
  const rows = ["strength_power", "skill", "mixed", "sparring", "conditioning", "rest"];
  assert.deepEqual(prioritiseForMode(rows, "technical", planRelevance), ["skill", "sparring", "strength_power", "mixed", "conditioning", "rest"]);
  assert.deepEqual(prioritiseForMode(rows, "sc", planRelevance), ["strength_power", "conditioning", "skill", "mixed", "sparring", "rest"]);
});

test("mode is isolated on account change, sign-out and invalid stored values", async () => {
  const host = window.document.createElement("div");
  const root = createRoot(host);
  function Probe({ account }: { account: string | null }) {
    const { ready, coachMode, setMode } = useCoachMode(account);
    return createElement("button", { onClick: () => setMode("technical") }, ready ? coachMode : "pending");
  }
  const render = async (account: string | null) => {
    await act(async () => root.render(createElement(Probe, { account })));
    return host.textContent;
  };
  try {
    assert.equal(await render("a"), "sc");
    await act(async () => host.querySelector("button")!.click());
    assert.equal(host.textContent, "technical");
    assert.equal(await render(null), "pending");
    window.localStorage.setItem("unlxck:coach-mode:b", "invalid");
    assert.equal(await render("b"), "sc");
    assert.equal(await render("a"), "technical");
  } finally {
    act(() => root.unmount());
    window.localStorage.clear();
  }
});

test("roster S&C counts completed work in each fighter's local week from the roster read", () => {
  const row = (day: string, type: string, extra: Partial<CoachSession> = {}): CoachSession =>
    ({ id: `${day}:${type}`, day, type, status: "done", source: "unlxck", recorded_at: `${day}T12:00:00Z`, ...extra });
  assert.equal(sessionRelevance(row("2026-11-10", "strength_power")), "sc");
  // Masked injury work arrives as "programme" and never counts.
  assert.equal(sessionRelevance(row("2026-11-10", "programme")), "shared");
  assert.equal(sessionRelevance(row("2026-11-10", "strength", { source: "external" })), "sc");
  assert.equal(sessionRelevance(row("2026-11-10", "strength", { source: "external", sport: "boxing" })), "shared");
  const london = { today: "2026-11-15", sessions: [
    row("2026-11-09", "strength_power"), row("2026-11-15", "conditioning"),
    row("2026-11-12", "conditioning", { status: "skipped" }), row("2026-11-11", "sparring"),
    row("2026-11-08", "strength_power"), row("2026-11-14", "roadwork", { source: "external" }),
  ] };
  // Already Monday in Tokyo: last week's work belongs to the previous week.
  const tokyo = { today: "2026-11-16", sessions: [row("2026-11-15", "strength_power"), row("2026-11-16", "conditioning")] };
  assert.equal(rosterPhysicalCompleted([london]), 3);
  assert.equal(rosterPhysicalCompleted([london, tokyo]), 4);
  assert.equal(rosterPhysicalCompleted([]), 0);
});
