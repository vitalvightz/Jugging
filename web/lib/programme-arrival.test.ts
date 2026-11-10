import test from "node:test";
import assert from "node:assert/strict";
import "../components/test-dom";
import { offerProgrammeArrival, hasProgrammeArrival, consumeProgrammeArrival, programmeArrivalHeadline } from "./programme-arrival";

test("completion survives navigation and refresh, matches one plan and cannot be reoffered after display", () => {
  window.sessionStorage.clear();
  window.localStorage.clear();
  assert.equal(hasProgrammeArrival("new"), false);
  offerProgrammeArrival("new");
  assert.equal(consumeProgrammeArrival("other"), false);
  window.history.replaceState(null, "", "/plans/new");
  assert.equal(hasProgrammeArrival("new"), true);
  // The receipt has no document identity: an unshown receipt survives reload.
  assert.deepEqual(JSON.parse(window.sessionStorage.getItem("unlxck:programme-arrival")!), {planId: "new"});
  assert.equal(consumeProgrammeArrival("new"), true);
  offerProgrammeArrival("new");
  assert.equal(hasProgrammeArrival("new"), false);
  assert.equal(consumeProgrammeArrival("new"), false);
  window.sessionStorage.setItem("unlxck:programme-arrival", "invalid");
  assert.equal(hasProgrammeArrival("new"), false);
  window.sessionStorage.clear();
  // Copied URLs alone do not confer eligibility.
  window.history.replaceState(null, "", "/today?plan=new");
  assert.equal(hasProgrammeArrival("new"), false);
  window.history.replaceState(null, "", "/today");
  window.localStorage.clear();
});

test("ready heading uses only duration and a recognised sport, never profile prose", () => {
  assert.equal(programmeArrivalHeadline(3, false, null, "Boxing, 3 x 3 rounds; primary focus speed, with footwork as the main limiter.."), "3-week boxing camp");
  assert.equal(programmeArrivalHeadline(1, true, "mma", "Boxing, speed focus"), "1-week MMA programme");
  assert.equal(programmeArrivalHeadline(6, false, "muay_thai"), "6-week muay thai camp");
  assert.equal(programmeArrivalHeadline(3, false, null, "A long unrecognised description"), "3-week camp");
  assert.equal(programmeArrivalHeadline(0, true), "programme");
});
