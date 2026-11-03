import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import type { TrainingFocusItem } from "./api-schema.generated.ts";
import {
  exerciseCues, focusFitsSession, focusFitsSport, focusSessionFor, movementPattern, relevantFocus, relevantFocusItem, upcomingSport,
  type FocusSession,
} from "./training-focus.ts";

// Real compiled athlete-built sessions; tests/test_custom_session_focus_contract.py
// keeps this file in step with the backend.
const custom = JSON.parse(readFileSync(new URL("../test/fixtures/custom-plan-focus-sessions.json", import.meta.url), "utf8")) as (FocusSession & { title: string })[];
const byTitle = (title: string) => custom.find((session) => session.title === title)!;

const generatedSparring: FocusSession = { session_type: "sparring", blocks: [{ block_type: "warmup" }, { block_type: "sparring" }] };
const generatedStrength: FocusSession = { session_type: "strength_power", blocks: [{ block_type: "strength" }] };
// Generated "skill" sessions include mental work such as Fight Visualisation.
const visualisation: FocusSession = { session_type: "skill", blocks: [{ block_type: "mindset" }] };
const conditioning: FocusSession = { session_type: "conditioning", blocks: [{ block_type: "conditioning" }] };
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
  assert.equal(focusFitsSession({ session: conditioning, contactLeads: false }), false);
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

// -- AI-inferred focus: shown only where the upcoming work is the same work ----

const inferred = (fields: Partial<TrainingFocusItem>): TrainingFocusItem => ({
  id: "inf-1", origin: "inferred", focus: "Stay balanced through each split squat.", training_day: "2026-11-01",
  sport: null, scope: "specific_exercise", category: "strength", exercise_targets: ["split squat"],
  movement_patterns: [], ...fields,
});
const withItems = (...items: TrainingFocusItem[]) => ({ focus: null, training_day: null, sport: null, items });
const strengthSession = (...names: string[]): FocusSession => ({
  session_type: "strength", blocks: [{ block_type: "warmup", display_name: "Bodyweight Split Squat" },
    ...names.map((display_name) => ({ block_type: "strength", display_name }))],
});
const card = (session: FocusSession | null, extra: { contactLeads?: boolean; sport?: string | null } = {}) =>
  ({ session, contactLeads: extra.contactLeads ?? false, sport: extra.sport ?? null });
const splitSquat = withItems(inferred({}));

test("a split-squat issue shows before split squats, in any variant", () => {
  assert.equal(relevantFocus(splitSquat, card(strengthSession("Split Squat", "Calf Raises"))), "Stay balanced through each split squat.");
  assert.equal(relevantFocus(splitSquat, card(strengthSession("Bulgarian Split Squat"))), "Stay balanced through each split squat.");
});

test("a split-squat issue stays hidden before deadlifts or an unrelated upper-body day", () => {
  assert.equal(relevantFocus(splitSquat, card(strengthSession("Trap Bar Deadlift", "Romanian Deadlift (RDL)"))), null);
  assert.equal(relevantFocus(splitSquat, card(strengthSession("Bench Press", "Pull-Up"))), null);
  // A warm-up drill of the same movement is not where the cue applies.
  assert.equal(relevantFocus(splitSquat, card(strengthSession("Assault Bike"))), null);
});

test("missing or ambiguous session detail hides a specific reminder", () => {
  assert.equal(relevantFocus(splitSquat, card(null)), null);
  assert.equal(relevantFocus(splitSquat, card({ session_type: "strength", blocks: [] })), null);
  assert.equal(relevantFocus(splitSquat, card({ session_type: "strength" })), null);
  // A contact-led card says nothing about the exercises.
  assert.equal(relevantFocus(splitSquat, card(null, { contactLeads: true })), null);
});

test("pattern transfer only where the inference grounded it", () => {
  const bracing = withItems(inferred({ focus: "Brace before every loaded rep.", scope: "general_quality",
    exercise_targets: [], movement_patterns: ["hinge", "squat"] }));
  assert.equal(relevantFocus(bracing, card(strengthSession("Trap Bar Deadlift"))), "Brace before every loaded rep.");
  assert.equal(relevantFocus(bracing, card(strengthSession("Goblet Squat"))), "Brace before every loaded rep.");
  assert.equal(relevantFocus(bracing, card(strengthSession("Overhead Press"))), null);
  // A general quality with no pattern never matches on the broad label alone.
  assert.equal(relevantFocus(withItems(inferred({ scope: "general_quality", exercise_targets: [], movement_patterns: [] })),
    card(strengthSession("Trap Bar Deadlift"))), null);
});

test("a boxing technique focus shows before boxing technical work, never before a conditioning run", () => {
  const jab = withItems(inferred({ focus: "Bring the jab straight back to guard.", scope: "sport_technique",
    category: "sparring", sport: "boxing", exercise_targets: [] }));
  const technical = byTitle("Light Technical");
  assert.equal(relevantFocus(jab, card(technical, { sport: "boxing" })), "Bring the jab straight back to guard.");
  assert.equal(relevantFocus(jab, card(generatedSparring, { sport: "boxing" })), "Bring the jab straight back to guard.");
  assert.equal(relevantFocus(jab, card(conditioning, { sport: "boxing" })), null);
  assert.equal(relevantFocus(jab, card({ session_type: "conditioning", blocks: [{ block_type: "conditioning", display_name: "Easy Run" }] },
    { sport: "boxing" })), null);
  assert.equal(relevantFocus(jab, card(technical, { sport: "muay_thai" })), null);
});

test("the newest focus that fits wins; the athlete's own focus keeps its existing rules", () => {
  const explicit: TrainingFocusItem = { ...inferred({}), id: "log-1", origin: "athlete", focus: "Slip outside the jab",
    scope: null, category: null, exercise_targets: [] };
  const both = withItems(explicit, inferred({}));
  assert.deepEqual(relevantFocusItem(both, card(generatedSparring)), { id: "log-1", text: "Slip outside the jab", origin: "athlete" });
  assert.deepEqual(relevantFocusItem(both, card(strengthSession("Split Squat"))),
    { id: "inf-1", text: "Stay balanced through each split squat.", origin: "inferred" });
  assert.equal(relevantFocusItem(withItems(), card(generatedSparring)), null);
});

test("movement patterns match the backend's shared fixture", () => {
  const cases = JSON.parse(readFileSync(new URL("../test/fixtures/training-focus-patterns.json", import.meta.url), "utf8")) as Record<string, string | null>;
  for (const [name, pattern] of Object.entries(cases)) assert.equal(movementPattern(name), pattern, name);
});

test("every candidate is considered: an older match behind many newer misses still shows", () => {
  const misses = Array.from({ length: 7 }, (_, index) =>
    inferred({ id: `miss-${index}`, focus: `Cue ${index}`, exercise_targets: ["bench press"] }));
  const older = inferred({ id: "older", focus: "Stay balanced through each split squat." });
  assert.deepEqual(relevantFocusItem(withItems(...misses, older), card(strengthSession("Split Squat"))),
    { id: "older", text: "Stay balanced through each split squat.", origin: "inferred" });
});

// -- Only the upcoming session's own work, never the whole day -----------------

const dayWideLive = [
  { block_type: "strength", display_name: "Bench Press", source_session_id: "am-upper" },
  { block_type: "strength", display_name: "Split Squat", source_session_id: "pm-lower" },
];

test("today's day-wide live blocks are narrowed to the session the card shows", () => {
  const upper = focusSessionFor({ sessionId: "am-upper", sessionType: "strength", liveBlocks: dayWideLive, plannedSessionCount: 2 });
  assert.deepEqual(upper?.blocks?.map((block) => block.display_name), ["Bench Press"]);
  // A split-squat cue never shows on the upper-body card because a later session that day has split squats.
  assert.equal(relevantFocus(splitSquat, card(upper)), null);
  const lower = focusSessionFor({ sessionId: "pm-lower", sessionType: "strength", liveBlocks: dayWideLive, plannedSessionCount: 2 });
  assert.equal(relevantFocus(splitSquat, card(lower)), "Stay balanced through each split squat.");
});

test("untagged live blocks count only on a one-session day", () => {
  const untagged = [{ block_type: "strength", display_name: "Split Squat" }];
  assert.equal(focusSessionFor({ sessionId: "s1", liveBlocks: untagged, plannedSessionCount: 1 })?.blocks?.length, 1);
  assert.equal(focusSessionFor({ sessionId: "s1", liveBlocks: untagged, plannedSessionCount: 2 })?.blocks?.length, 0);
  assert.equal(focusSessionFor({ sessionId: null, liveBlocks: untagged, plannedSessionCount: 1 }), null);
});

test("a preview uses that session's planned blocks, not today's live prescription", () => {
  const planned: FocusSession = { session_type: "strength", blocks: [{ block_type: "strength", display_name: "Trap Bar Deadlift" }] };
  const preview = focusSessionFor({ sessionId: "tomorrow", planned, cardBlocks: [], plannedSessionCount: 1 });
  assert.deepEqual(preview?.blocks?.map((block) => block.display_name), ["Trap Bar Deadlift"]);
  assert.equal(relevantFocus(splitSquat, card(preview)), null);
});

test("an inferred technique cue needs its own known sport to match", () => {
  const technique = (sport: string | null) => withItems(inferred({ focus: "Return the jab to guard.", scope: "sport_technique",
    category: "sparring", sport, exercise_targets: [] }));
  assert.equal(relevantFocus(technique("boxing"), card(generatedSparring, { sport: "boxing" })), "Return the jab to guard.");
  assert.equal(relevantFocus(technique("boxing"), card(generatedSparring, { sport: "muay_thai" })), null);
  assert.equal(relevantFocus(technique("boxing"), card(generatedSparring, { sport: null })), null);
  assert.equal(relevantFocus(technique(null), card(generatedSparring, { sport: "boxing" })), null);
});

test("prehab and rehab-labelled blocks are never where a cue applies", () => {
  const session: FocusSession = { session_type: "strength", blocks: [
    { block_type: "prehab", display_name: "Split Squat Iso Hold" },
    { block_type: "knee_rehab", display_name: "Split Squat" },
    { block_type: "rehabilitation", display_name: "Split Squat Hold" },
  ] };
  assert.equal(relevantFocus(splitSquat, card(session)), null);
});

// -- YOUR FOCUS and UNLXCK CUE never compete for one slot -----------------------

const athleteOnly = (item: TrainingFocusItem) => item.origin === "athlete";

test("the fighter's own focus is never replaced by a newer fitting cue", () => {
  const explicit: TrainingFocusItem = { ...inferred({}), id: "log-1", origin: "athlete", focus: "Need to get in pockets",
    scope: null, category: null, exercise_targets: [] };
  const jab = inferred({ id: "jab", focus: "Bring the jab straight back to guard.", scope: "sport_technique", sport: "boxing" });
  const target = card(generatedSparring, { sport: "boxing" });
  assert.equal(relevantFocusItem(withItems(jab, explicit), target)?.id, "jab");
  assert.equal(relevantFocusItem(withItems(jab, explicit), target, athleteOnly)?.text, "Need to get in pockets");
  assert.equal(relevantFocusItem(withItems(jab), target, athleteOnly), null);
});

test("exercise cues sit under the exercise each is about, once", () => {
  const block = (block_id: string, display_name: string, block_type = "strength") => ({ block_id, block_type, display_name });
  const pistol = inferred({ id: "pistol", focus: "Keep your foot planted and control the descent.", exercise_targets: ["pistol squat"] });
  const bracing = inferred({ id: "brace", focus: "Brace before every loaded rep.", scope: "movement_pattern",
    exercise_targets: [], movement_patterns: ["squat"] });
  const jab = inferred({ id: "jab", scope: "sport_technique", sport: "boxing", exercise_targets: [] });
  const explicit: TrainingFocusItem = { ...inferred({}), id: "log-1", origin: "athlete", focus: "Split squat depth" };
  const blocks = [block("w", "Goblet Squat", "warmup"), block("a", "Assisted Pistol Squat"), block("b", "Goblet Squat"),
    block("c", "Back Squat"), block("d", "Bench Press"), { display_name: "Front Squat", block_type: "strength" }];
  assert.deepEqual(exerciseCues(withItems(explicit, jab, pistol, bracing), blocks), {
    a: { id: "pistol", text: "Keep your foot planted and control the descent.", origin: "inferred" },
    // The pattern cue shows under the first squat it fits, not again under Back Squat.
    b: { id: "brace", text: "Brace before every loaded rep.", origin: "inferred" },
  });
  // No fitting cue, or no focus at all: nothing is shown.
  assert.deepEqual(exerciseCues(withItems(pistol), [block("d", "Bench Press")]), {});
  assert.deepEqual(exerciseCues(null, blocks), {});
  // Athlete-built rows match the same way.
  assert.deepEqual(Object.keys(exerciseCues(splitSquat, [{ ...block("x", "Split Squat"), prescription_source: "athlete" as const }])), ["x"]);
});
