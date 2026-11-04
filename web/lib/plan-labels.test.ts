import assert from "node:assert/strict";
import test from "node:test";

import { athleteFacingSessionTitle, formatPlanLabel, isRawEnumLabel, stripLeadingWeekday } from "./plan-labels.ts";

test("formatPlanLabel maps readiness/decision enums to readable labels", () => {
  assert.equal(formatPlanLabel("stop_and_report"), "Stop and report");
  assert.equal(formatPlanLabel("train_as_planned"), "Train as planned");
  assert.equal(formatPlanLabel("pull_back"), "Pull back");
  assert.equal(formatPlanLabel("modify"), "Modify");
});

test("formatPlanLabel maps plan status flags", () => {
  assert.equal(formatPlanLabel("ready"), "Ready");
  assert.equal(formatPlanLabel("publishable_with_flags"), "Ready, with review notes");
  assert.equal(formatPlanLabel("review_required"), "Awaiting review");
  assert.equal(formatPlanLabel("triage_blocked"), "Paused for safety review");
  assert.equal(formatPlanLabel("archived"), "Archived");
  assert.equal(formatPlanLabel("generated"), "Processing");
});

test("formatPlanLabel maps phase labels", () => {
  assert.equal(formatPlanLabel("SPP"), "Specific prep");
  assert.equal(formatPlanLabel("GPP"), "General prep");
  assert.equal(formatPlanLabel("TAPER"), "Taper");
  assert.equal(formatPlanLabel("FIGHT_WEEK"), "Fight week");
});

test("formatPlanLabel maps session/block types including spaced raw forms", () => {
  assert.equal(formatPlanLabel("MIXED"), "Mixed session");
  assert.equal(formatPlanLabel("ACCESSORY"), "Accessory");
  assert.equal(formatPlanLabel("MOBILITY ACTIVATION"), "Mobility");
  assert.equal(formatPlanLabel("PLYOMETRIC POWER"), "Power");
  assert.equal(formatPlanLabel("FIGHT OR MATCH"), "Fight day");
});

test("formatPlanLabel maps severities", () => {
  assert.equal(formatPlanLabel("red"), "Red");
  assert.equal(formatPlanLabel("amber"), "Amber");
});

test("formatPlanLabel is case/separator tolerant", () => {
  assert.equal(formatPlanLabel("mobility_activation"), "Mobility");
  assert.equal(formatPlanLabel("  Spp  "), "Specific prep");
});

test("formatPlanLabel falls back to a generic titleizer for unknown values", () => {
  assert.equal(formatPlanLabel("some_custom_block"), "Some Custom Block");
  assert.equal(formatPlanLabel("zone two"), "Zone Two");
});

test("formatPlanLabel returns empty string for blank/non-string input", () => {
  assert.equal(formatPlanLabel(""), "");
  assert.equal(formatPlanLabel("   "), "");
  assert.equal(formatPlanLabel(null), "");
  assert.equal(formatPlanLabel(undefined), "");
  assert.equal(formatPlanLabel(42), "");
});

test("isRawEnumLabel detects machine tokens but not human sentences", () => {
  assert.equal(isRawEnumLabel("stop_and_report"), true);
  assert.equal(isRawEnumLabel("modify"), true);
  assert.equal(isRawEnumLabel("Stop and report sharp pain."), false);
  assert.equal(isRawEnumLabel("Red"), false);
  assert.equal(isRawEnumLabel(""), false);
  assert.equal(isRawEnumLabel(null), false);
});

test("a generated title that opens with its weekday drops it", () => {
  assert.equal(stripLeadingWeekday("Monday. Strength anchor"), "Strength anchor");
  assert.equal(stripLeadingWeekday("Tuesday. Fight-pace conditioning"), "Fight-pace conditioning");
  assert.equal(stripLeadingWeekday("Wed - light flush"), "Light flush");
  assert.equal(athleteFacingSessionTitle("Friday: Neural primer"), "Neural primer");
  // Not a weekday prefix, or nothing would be left: unchanged.
  assert.equal(stripLeadingWeekday("Sunday"), "Sunday");
  assert.equal(stripLeadingWeekday("Mondays off"), "Mondays off");
  assert.equal(stripLeadingWeekday("Strength anchor"), "Strength anchor");
});

test("legacy technical_combat identifier is athlete-readable", () => {
  assert.equal(formatPlanLabel("technical_combat"), "Technical Combat");
  assert.equal(athleteFacingSessionTitle("technical_combat"), "Technical Combat");
  assert.equal(athleteFacingSessionTitle("Hard sparring"), "Hard sparring");
});
