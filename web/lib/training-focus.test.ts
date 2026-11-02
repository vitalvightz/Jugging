import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { focusFitsSession, focusFitsSport, relevantFocus, upcomingSport, type FocusSession } from "./training-focus.ts";

// Real compiled athlete-built sessions; tests/test_custom_session_focus_contract.py
// keeps this file in step with the backend.
const custom = JSON.parse(readFileSync(new URL("../test/fixtures/custom-plan-focus-sessions.json", import.meta.url), "utf8")) as (FocusSession & { title: string })[];
const byTitle = (title: string) => custom.find((session) => session.title === title)!;

const generatedSparring: FocusSession = { session_type: "sparring", blocks: [{ block_type: "warmup" }, { block_type: "sparring" }] };
const generatedStrength: FocusSession = { session_type: "strength_power", blocks: [{ block_type: "strength" }] };
// Generated "skill" sessions include mental work such as Fight Visualisation.
const visualisation: FocusSession = { session_type: "skill", blocks: [{ block_type: "mindset" }] };
const focus = { focus: "Keep my right hand up after jabbing", training_day: "2026-11-02", sport: null };

test("athlete-built technical and sparring sessions take a focus; their strength work does not", () => {
  assert.equal(focusFitsSession({ session: byTitle("Light Technical"), contactLeads: false }), true);
  assert.equal(focusFitsSession({ session: byTitle("Hard Sparring"), contactLeads: false }), true);
  assert.equal(focusFitsSession({ session: byTitle("Strength"), contactLeads: false }), false);
});

test("a generated session needs real sparring blocks; skill/mental work does not count", () => {
  assert.equal(focusFitsSession({ session: generatedSparring, contactLeads: false }), true);
  assert.equal(focusFitsSession({ session: generatedStrength, contactLeads: false }), false);
  assert.equal(focusFitsSession({ session: visualisation, contactLeads: false }), false);
});

test("only the shown session counts, unless the card leads with the day's contact", () => {
  // Strength shown on a mixed day: no reminder, whatever else the day holds.
  assert.equal(focusFitsSession({ session: generatedStrength, contactLeads: false }), false);
  // A coach-led contact day (with or without an app session) is about that contact.
  assert.equal(focusFitsSession({ session: generatedStrength, contactLeads: true }), true);
  assert.equal(focusFitsSession({ session: null, contactLeads: true }), true);
  assert.equal(focusFitsSession({ session: null, contactLeads: false }), false);
});

test("the upcoming sport is used only when unambiguous", () => {
  assert.equal(upcomingSport("boxing", ["boxing", "muay_thai"]), "boxing");
  assert.equal(upcomingSport("Muay Thai", []), "muay_thai");
  assert.equal(upcomingSport("boxing, muay_thai", ["boxing", "muay_thai"]), null);
  assert.equal(upcomingSport(null, ["boxing"]), "boxing");
  assert.equal(upcomingSport(null, ["boxing", "muay_thai"]), null);
  assert.equal(upcomingSport("combat_sports", ["boxing", "muay_thai"]), null);
});

test("a sport-specific focus needs that exact upcoming sport", () => {
  assert.equal(focusFitsSport(null, null), true);
  assert.equal(focusFitsSport("boxing", "boxing"), true);
  // A two-sport fighter's Muay Thai note is not shown before boxing, or when unsure.
  assert.equal(focusFitsSport("muay_thai", "boxing"), false);
  assert.equal(focusFitsSport("muay_thai", null), false);
});

test("relevantFocus combines both and ignores blanks", () => {
  const target = { session: generatedSparring, contactLeads: false, sport: "boxing" };
  assert.equal(relevantFocus(focus, target), "Keep my right hand up after jabbing");
  assert.equal(relevantFocus({ ...focus, sport: "muay_thai" }, target), null);
  assert.equal(relevantFocus(focus, { ...target, session: generatedStrength }), null);
  assert.equal(relevantFocus({ ...focus, focus: "  " }, target), null);
  assert.equal(relevantFocus(null, target), null);
});
