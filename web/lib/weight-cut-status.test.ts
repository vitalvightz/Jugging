import assert from "node:assert/strict";
import test from "node:test";

import { cutProgress, daysLeftLabel, daysToFight, describeCutStatus, inFinalWeeks, weighInDetails, weighInMoment } from "./weight-cut-status.ts";
import type { NutritionDerivedState } from "./types.ts";

const derived = (overrides: Partial<NutritionDerivedState>): NutritionDerivedState => ({
  weight_cut_pct: 3.4,
  weight_cut_risk: true,
  aggressive_weight_cut: false,
  high_pressure_weight_cut: false,
  short_notice: false,
  fight_week: false,
  readiness_flags: [],
  fight_week_override_band: "none",
  foundation_status: "complete",
  missing_required_fields: [],
  days_until_fight: 19,
  ...overrides,
});

test("final weeks run from four weeks out to fight day", () => {
  assert.equal(daysToFight("2026-11-22", "2026-11-03"), 19);
  assert.equal(inFinalWeeks("2026-12-01", "2026-11-03"), true);
  assert.equal(inFinalWeeks("2026-12-02", "2026-11-03"), false);
  assert.equal(inFinalWeeks("2026-11-03", "2026-11-03"), true);
  assert.equal(inFinalWeeks("2026-11-02", "2026-11-03"), false);
  assert.equal(inFinalWeeks(null, "2026-11-03"), false);
  assert.equal(inFinalWeeks("", "2026-11-03"), false);
});

test("weigh-in moment follows the weigh-in type", () => {
  assert.equal(weighInMoment("2026-11-14", "day_before", "18:00"), "Fri 13 Nov 2026 · 18:00");
  assert.equal(weighInMoment("2026-11-14", "same_day", null), "Sat 14 Nov 2026");
  assert.equal(weighInMoment("2026-11-14", "informal", "09:00"), "Informal weigh-in · 09:00");
  assert.equal(weighInMoment("2026-11-14", null, "18:00"), null);
  assert.equal(weighInMoment("", "day_before", "18:00"), null);
});

test("status copy names who to talk to and never how to cut", () => {
  assert.deepEqual(describeCutStatus(derived({ weight_cut_status: "on_track", kg_to_target: 2.4 })),
    { label: "On track", tone: "green", detail: "2.4 kg to go." });
  assert.deepEqual(describeCutStatus(derived({ weight_cut_status: "behind", kg_to_target: 3.3, weight_cut_behind_kg: 0.8, days_until_fight: 12 })),
    { label: "Behind by 0.8 kg", tone: "amber", detail: "Steep for 12 days out. Talk to your coach about your plan." });
  assert.equal(describeCutStatus(derived({ weight_cut_status: "behind", weight_cut_behind_kg: 0.3, days_until_fight: 1 }))?.detail,
    "Steep for 1 day out. Talk to your coach about your plan.");
  assert.deepEqual(describeCutStatus(derived({ weight_cut_status: "too_aggressive", kg_to_target: 6 })),
    { label: "Cut too aggressive", tone: "red", detail: "Don't attempt this without your coach or a medical professional overseeing it." });
  assert.equal(describeCutStatus(derived({ weight_cut_status: "on_target", kg_to_target: 0 }))?.label, "On target");
  assert.equal(describeCutStatus(derived({ weight_cut_status: "none" })), null);
  // An older API without the status says nothing rather than guessing.
  assert.equal(describeCutStatus(derived({})), null);
  assert.equal(describeCutStatus(null), null);
});

test("the card's kicker and weigh-in row", () => {
  assert.equal(daysLeftLabel(18), "18 days left");
  assert.equal(daysLeftLabel(1), "1 day left");
  assert.equal(daysLeftLabel(0), "Fight day");
  assert.deepEqual(weighInDetails("2026-11-22", "day_before", "18:00"), { when: "Sat 21 Nov · 18:00", label: "Official weigh-in" });
  assert.deepEqual(weighInDetails("2026-11-22", "same_day", null), { when: "Sun 22 Nov", label: "Official weigh-in" });
  assert.deepEqual(weighInDetails("2026-11-22", "informal", null), { when: null, label: "Informal weigh-in" });
  assert.equal(weighInDetails("2026-11-22", null, "18:00"), null);
});

test("progress runs from the first weight of the final weeks to the target", () => {
  const trend = [
    { date: "2026-10-20", scale_weight_kg: 75.0 },
    { date: "2026-10-26", scale_weight_kg: 73.4 },
    { date: "2026-11-02", scale_weight_kg: 72.6 },
  ];
  // Final weeks for a 22 Nov fight start on 25 Oct, so 20 Oct is before them.
  const halfway = cutProgress({ trend, fightDate: "2026-11-22", currentKg: 72.2, targetKg: 71 });
  assert.equal(halfway?.startKg, 73.4);
  assert.equal(Math.round(halfway!.fraction * 100) / 100, 0.5);
  // One entry so far: an empty bar that fills from the next entry.
  assert.deepEqual(cutProgress({ trend: trend.slice(0, 2), fightDate: "2026-11-22", currentKg: 73.4, targetKg: 71 }), { startKg: 73.4, fraction: 0 });
  // Heavier than the start, or past the target, stays inside the bar.
  assert.equal(cutProgress({ trend, fightDate: "2026-11-22", currentKg: 74, targetKg: 71 })?.fraction, 0);
  assert.equal(cutProgress({ trend, fightDate: "2026-11-22", currentKg: 70.5, targetKg: 71 })?.fraction, 1);
  assert.equal(cutProgress({ trend: [], fightDate: "2026-11-22", currentKg: 73, targetKg: 71 }), null);
  assert.equal(cutProgress({ trend, fightDate: "2026-11-22", currentKg: 72, targetKg: 74 }), null);
  assert.equal(cutProgress({ trend, fightDate: "2026-11-22", currentKg: null, targetKg: 71 }), null);
});
