import assert from "node:assert/strict";
import test from "node:test";

import {
  getAvailabilityConsistency,
  getHardSparringWarning,
  getSparringConsistency,
} from "./training-schedule.ts";

test("counts distinct training days rather than double sessions", () => {
  assert.equal(getAvailabilityConsistency(["Monday", "Wednesday"], 2).hardError, null);
  assert.equal(getAvailabilityConsistency(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], 7).hardError, null);
});

test("flags days exceeding training availability", () => {
  assert.deepStrictEqual(
    getAvailabilityConsistency(["Monday", "Wednesday"], 3),
    {
      hardError: "You selected 2 available days but planned 3 training days per week.",
      softWarning: null,
    },
  );
});

test("requires a combat session only when building a Fight Camp", () => {
  assert.deepStrictEqual(
    getSparringConsistency(["Monday"], [], [], true),
    {
      hardError: "Add at least one hard sparring or light/technical combat day to build a Fight Camp. If none is scheduled, use Open Plan.",
      softWarning: null,
    },
  );
  assert.deepStrictEqual(
    getSparringConsistency(["Monday"], [], [], false),
    { hardError: null, softWarning: null },
  );
});

test("accepts hard or light combat sessions for a Fight Camp", () => {
  assert.equal(getSparringConsistency(["Monday"], ["Monday"], [], true).hardError, null);
  assert.equal(getSparringConsistency(["Monday"], [], ["Monday"], true).hardError, null);
});

test("flags hard sparring days that sit outside the available schedule", () => {
  assert.deepStrictEqual(
    getSparringConsistency(["Monday", "Wednesday"], ["Friday"], []),
    {
      hardError: "Hard sparring days must also be selected as available days: Friday.",
      softWarning: null,
    },
  );
});

test("flags overlap between hard sparring and Light Combat days", () => {
  assert.deepStrictEqual(
    getSparringConsistency(["Monday", "Wednesday", "Friday"], ["Wednesday"], ["Wednesday", "Friday"]),
    {
      hardError: "A day cannot be both hard sparring and Support Work (Light Combat): Wednesday.",
      softWarning: null,
    },
  );
});

test("blocks onboarding when hard sparring exceeds four days", () => {
  assert.deepStrictEqual(
    getSparringConsistency(
      ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
      ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
      [],
    ),
    {
      hardError: "Hard sparring days cap is 4; reduce to 4 or fewer to continue.",
      softWarning: null,
    },
  );
});

test("does not warn when hard sparring stays at two days or fewer", () => {
  assert.deepStrictEqual(
    getHardSparringWarning(["Monday", "Wednesday"], 5),
    {
      acknowledgementContextKey: "hard-sparring:5:Monday|Wednesday",
      message: null,
      requiresAcknowledgement: false,
    },
  );
});

test("warns when hard sparring reaches three or more days", () => {
  assert.deepStrictEqual(
    getHardSparringWarning(["Monday", "Wednesday", "Friday"], 5),
    {
      acknowledgementContextKey: "hard-sparring:5:Friday|Monday|Wednesday",
      message:
        "You selected 3 hard sparring days inside 5 planned training days per week. Confirm recovery planning before continuing.",
      requiresAcknowledgement: true,
    },
  );
});

test("changes the acknowledgement reset key when hard days or session count change", () => {
  const baseline = getHardSparringWarning(["Monday", "Wednesday", "Friday"], 5).acknowledgementContextKey;

  assert.notEqual(
    baseline,
    getHardSparringWarning(["Monday", "Thursday", "Friday"], 5).acknowledgementContextKey,
  );

  assert.notEqual(
    baseline,
    getHardSparringWarning(["Monday", "Wednesday", "Friday"], 4).acknowledgementContextKey,
  );
});

test("flags more combat days than planned training days", () => {
  const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];
  assert.deepStrictEqual(
    getSparringConsistency(days, ["Monday", "Wednesday", "Friday"], ["Tuesday", "Thursday"], true, 4),
    {
      hardError: "Your schedule doesn't add up: you selected 5 combat days but entered 4 training days per week. Update your total or adjust your combat days.",
      softWarning: null,
    },
  );
  assert.equal(
    getSparringConsistency(days, ["Monday", "Wednesday", "Friday"], ["Tuesday", "Thursday"], true, 5).hardError,
    null,
  );
});
