import assert from "node:assert/strict";
import test from "node:test";
import disciplines from "../../shared/training-disciplines.json";
import {
  activityFor, athleteSport, fitsPicks, isValidClassification, sessionTypes, TRAINING_SPORTS, trainingLabel, trainingPicks,
} from "./training-disciplines";

test("the sport list matches the shared taxonomy the API validates against", () => {
  assert.deepEqual([...TRAINING_SPORTS], disciplines.sports.map(sport => sport.id));
});

test("each requested discipline is labelled as logged, never as Boxing", () => {
  assert.equal(trainingLabel({ activity: "boxing", sport: "boxing", session_type: "pads" }), "Boxing · Pads");
  assert.equal(trainingLabel({ activity: "other", sport: "bjj", session_type: "rolling" }), "BJJ · Rolling");
  assert.equal(trainingLabel({ activity: "other", sport: "muay_thai", session_type: "clinch" }), "Muay Thai · Clinch");
  assert.equal(trainingLabel({ activity: "other", sport: "mma", session_type: "striking" }), "MMA · Striking");
  assert.equal(trainingLabel({ activity: "other", sport: "mma", session_type: "wrestling" }), "MMA · Wrestling");
  assert.equal(trainingLabel({ activity: "other", sport: "wrestling", session_type: "live" }), "Wrestling · Live wrestling");
  assert.equal(trainingLabel({ activity: "other", sport: "kickboxing" }), "Kickboxing");
  // Older records keep the label they always had.
  assert.equal(trainingLabel({ activity: "sparring" }), "Sparring");
  assert.equal(trainingLabel({ activity: null }), "Session");
});

test("only striking sparring is stored as sparring; grappling live work never is", () => {
  for (const sport of ["boxing", "muay_thai", "kickboxing", "mma"] as const) assert.equal(activityFor(sport, "sparring"), "sparring");
  assert.equal(activityFor("bjj", "rolling"), "other");
  assert.equal(activityFor("wrestling", "live"), "other");
  assert.equal(activityFor("boxing"), "boxing");
  assert.ok(!sessionTypes("bjj").some(item => item.id === "sparring"));
  assert.deepEqual(sessionTypes(null), []);
});

test("classification is consistent or not sent", () => {
  assert.ok(isValidClassification({ activity: "sparring" }));
  assert.ok(isValidClassification({ activity: "other", sport: "bjj", session_type: "rolling" }));
  assert.ok(!isValidClassification({ activity: "sparring", sport: "bjj", session_type: "rolling" }));
  assert.ok(!isValidClassification({ activity: "boxing", sport: "boxing", session_type: "clinch" as never }));
  assert.ok(!isValidClassification({ activity: "other", session_type: "pads" }));
});

test("the athlete's own sport decides the Activity choices", () => {
  assert.equal(athleteSport(["pressure_fighter", "bjj"]), "bjj");
  assert.equal(athleteSport(["karate"]), null);
  assert.equal(athleteSport(undefined), null);
  const labels = (sport: Parameters<typeof trainingPicks>[0]) => trainingPicks(sport).map(pick => pick.label);
  assert.deepEqual(labels("boxing"), ["Pads", "Bag work", "Shadowboxing", "Technical drills", "Sparring", "Strength", "Conditioning", "Running", "Other"]);
  assert.deepEqual(labels("bjj"), ["Drilling", "Positional", "Rolling", "Strength", "Conditioning", "Running", "Other"]);
  assert.deepEqual(labels("muay_thai").slice(0, 5), ["Pads", "Bag work", "Clinch", "Technical drills", "Sparring"]);
  // No sport known: the original six, unchanged.
  assert.deepEqual(labels(null), ["Boxing", "Sparring", "Strength", "Conditioning", "Running", "Other"]);
  // Every choice is a valid stored classification; S&C keeps the existing activities without a sport.
  for (const sport of [...TRAINING_SPORTS, null]) assert.ok(trainingPicks(sport).every(pick => isValidClassification(pick)));
  assert.deepEqual(trainingPicks("bjj").find(pick => pick.label === "Strength"), { label: "Strength", activity: "strength", sport: null, session_type: null });
});

test("older Boxing and Sparring records do not fit a sport's grid, so they open unchanged", () => {
  assert.equal(fitsPicks({ activity: "sparring" }, "boxing"), false);
  assert.equal(fitsPicks({ activity: "boxing" }, "boxing"), false);
  assert.equal(fitsPicks({ activity: "strength" }, "bjj"), true);
  assert.equal(fitsPicks({ activity: "sparring", sport: "boxing", session_type: "sparring" }, "boxing"), true);
});
