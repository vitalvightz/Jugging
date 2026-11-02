import test from "node:test";
import assert from "node:assert/strict";

import { focusFitsDay, focusFitsSport, relevantFocus } from "./training-focus.ts";
import type { StructuredDay, StructuredSession } from "./types";

const sparringDay = { today_card: { coach_led_contact: "Hard sparring" }, sessions: [] } as unknown as StructuredDay;
const strengthDay = { day_type: "moderate", today_card: { headline: "Strength" }, sessions: [{}] } as unknown as StructuredDay;
const strength = { session_type: "strength_power", blocks: [{ block_type: "strength" }] } as unknown as StructuredSession;
const sparringSession = { session_type: "sparring", blocks: [{ block_type: "sparring" }] } as unknown as StructuredSession;
const visualisation = { session_type: "skill", blocks: [{ block_type: "mindset" }] } as unknown as StructuredSession;
const focus = { focus: "Keep my right hand up after jabbing", training_day: "2026-11-02", sport: null };

test("contact days and sparring sessions take a focus", () => {
  assert.equal(focusFitsDay(sparringDay, []), true);
  assert.equal(focusFitsDay(strengthDay, [sparringSession]), true);
});

test("strength, conditioning and skill/mental sessions do not", () => {
  assert.equal(focusFitsDay(strengthDay, [strength]), false);
  assert.equal(focusFitsDay(strengthDay, [visualisation]), false);
  assert.equal(focusFitsDay(null, []), false);
});

test("a focus from another sport stays hidden; unknown sport follows the athlete", () => {
  assert.equal(focusFitsSport(null, ["boxing"]), true);
  assert.equal(focusFitsSport("boxing", ["boxing", "muay_thai"]), true);
  assert.equal(focusFitsSport("muay_thai", ["boxing"]), false);
  assert.equal(focusFitsSport("boxing", []), false);
});

test("relevantFocus combines both and ignores blanks", () => {
  assert.equal(relevantFocus(focus, sparringDay, [], ["boxing"]), "Keep my right hand up after jabbing");
  assert.equal(relevantFocus(focus, strengthDay, [strength], ["boxing"]), null);
  assert.equal(relevantFocus({ ...focus, focus: "  " }, sparringDay, [], ["boxing"]), null);
  assert.equal(relevantFocus(null, sparringDay, [], ["boxing"]), null);
});
