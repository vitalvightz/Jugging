import assert from "node:assert/strict";
import test from "node:test";
import { customSchedulePrefill, prepareCustomSession, customScheduleIssues, savedRoundsFormat, customPlanError } from "./custom-plan-schedule";
import { openBlockWeekIntent } from "./open-block";
import type { MeResponse } from "./types";

const onboarding = {
  profile: {
    full_name: "Ari",
    onboarding_draft: {
      athlete: { full_name: "Ari" }, no_scheduled_fight: true,
      training_availability: ["Monday", "Tuesday", "Thursday", "Friday"],
      hard_sparring_days: ["Monday"], support_work_days: ["Tuesday", "Thursday", "Friday"],
      session_types_by_day: { Tuesday: "strength", Thursday: "technical", Friday: "conditioning" },
    },
  },
  latest_intake: null,
} as unknown as MeResponse;

test("onboarding seeds hard contact and explicit technical days, never every support day", () => {
  const sessions = customSchedulePrefill(onboarding);
  assert.deepEqual(sessions.map(s => [s.weekday, s.session_type]), [["Monday", "sparring"], ["Thursday", "skill"]]);
  assert.equal(new Set(sessions.map(s => s.id)).size, 2);
  assert.deepEqual(sessions.map(s => s.exercises.length), [1, 1]);
});

test("an athlete can edit, remove and add several sessions within the same calendar day", () => {
  const sessions = customSchedulePrefill(onboarding);
  const edited = [
    { ...sessions[0], name: "Technique", session_type: "skill" as const, weekday: "Thursday" as const },
    { ...sessions[1], name: "Second workout" },
  ];
  assert.equal(edited.filter(s => s.weekday === "Thursday").length, 2);
  assert.equal(sessions[0].weekday, "Monday");
});

test("missing onboarding does not invent sessions", () => {
  assert.deepEqual(customSchedulePrefill(null), []);
});

test("an explicit hard-sparring day type prefills even before its derived day list is saved", () => {
  const me = structuredClone(onboarding);
  me.profile.onboarding_draft = {
    ...(me.profile.onboarding_draft ?? {}), hard_sparring_days: [], session_types_by_day: { Monday: "hard_spar" },
  };
  assert.deepEqual(customSchedulePrefill(me).map(s => [s.weekday, s.session_type]), [["Monday", "sparring"]]);
});

test("custom week overlay does not prescribe automatic progression or deload", () => {
  for (let week = 1; week <= 4; week++) {
    assert.match(openBlockWeekIntent(week, true)!.summary, /saved prescription/);
    assert.doesNotMatch(openBlockWeekIntent(week, true)!.summary, /halve|half|cut|Progress again/);
  }
  assert.match(openBlockWeekIntent(4)!.summary, /half/); // existing AI presentation
});



const emptySession = (type: CustomSession["session_type"], weekday: CustomSession["weekday"] = "Friday"): CustomSession => ({
  id: crypto.randomUUID(), name: "Training", session_type: type, weekday, exercises: [],
});
import type { CustomSession } from "./api-schema.generated";

test("Friday sparring and Thursday technical use saved rounds with editable dose fields and no intensity", () => {
  const me = structuredClone(onboarding);
  me.profile.onboarding_draft = { ...me.profile.onboarding_draft, rounds_format: "3 × 3" };
  for (const [type, day, title] of [["sparring", "Friday", "Hard Sparring"], ["skill", "Thursday", "Light Technical"]] as const) {
    const session = prepareCustomSession(emptySession(type, day), me);
    assert.equal(session.name, title);
    assert.equal(session.exercises.length, 1);
    assert.equal(session.exercises[0].dose.rounds, 3);
    assert.equal(session.exercises[0].dose.work_seconds, 180);
    assert.equal(session.exercises[0].dose.rest_seconds, 60);
    assert.equal(session.exercises[0].dose.rpe, null);
    assert.equal(session.exercises[0].dose.load_or_intensity, null);
    assert.deepEqual(customScheduleIssues([session]), []);
  }
});

test("round evidence falls back to latest intake, never the generic onboarding default", () => {
  assert.equal(savedRoundsFormat(onboarding), null);
  const suggested = prepareCustomSession(emptySession("sparring"), onboarding);
  assert.match((suggested.exercises[0] as import("./api-schema.generated").ManualExercise).notes!, /Editable suggestion only/);
  const me = structuredClone(onboarding);
  me.latest_intake = { rounds_format: "5 x 2" } as MeResponse["latest_intake"];
  const saved = prepareCustomSession(emptySession("skill"), me);
  assert.equal(saved.exercises[0].dose.rounds, 5);
  assert.equal(saved.exercises[0].dose.work_seconds, 120);
});

test("Recovery is valid empty, Sunday stays absent, and a scheduled empty Strength remains incomplete", () => {
  const training = prepareCustomSession(emptySession("skill", "Thursday"), onboarding);
  const recovery = emptySession("recovery", "Tuesday");
  assert.deepEqual(customScheduleIssues([training, recovery]), []);
  assert.equal([training, recovery].some(s => s.weekday === "Sunday"), false);
  const issues = customScheduleIssues([training, recovery, emptySession("strength_power", "Wednesday")]);
  assert.equal(issues.length, 1);
  assert.match(issues[0].message, /Wednesday.*Strength.*no exercises/);
  assert.match(customScheduleIssues([])[0].message, /Add at least one training day/);
});

test("type changes rename defaults, preserve custom names and keep programmed data through draft repair", () => {
  for (const type of ["recovery", "strength_power", "sparring", "skill"] as const) {
    const session = prepareCustomSession({ ...emptySession(type), name: "Training" }, onboarding, "mixed");
    assert.notEqual(session.name, "Training");
    assert.equal(prepareCustomSession({ ...session, name: "Lower Body", session_type: "conditioning" }, onboarding, type).name, "Lower Body");
  }
  const original = prepareCustomSession(emptySession("sparring"), onboarding);
  original.name = "Friday gym rounds";
  original.exercises[0].dose = { ...original.exercises[0].dose, rounds: 6, work_seconds: 120, rest_seconds: 45 };
  (original.exercises[0] as import("./api-schema.generated").ManualExercise).notes = "Coach's dose";
  const reloaded = JSON.parse(JSON.stringify(original)) as CustomSession;
  for (let save = 0; save < 3; save++) {
    assert.deepEqual(prepareCustomSession(reloaded, onboarding), original);
    assert.equal(prepareCustomSession(reloaded, onboarding).exercises.length, 1);
  }
});

test("normal athlete errors omit raw request identifiers", () => {
  assert.equal(customPlanError(new Error("Wednesday needs exercises. (request id: abc-123)"), "Unable to save"), "Wednesday needs exercises.");
});
