import test from "node:test";
import assert from "node:assert/strict";
import "../components/test-dom";
import { offerProgrammeArrival, hasProgrammeArrival, consumeProgrammeArrival } from "./programme-arrival";

test("handoff matches one plan, consumes once, and rejects a refreshed document", () => {
  window.sessionStorage.clear();
  assert.equal(hasProgrammeArrival("new"), false);
  offerProgrammeArrival("new");
  assert.equal(consumeProgrammeArrival("other"), false);
  assert.equal(hasProgrammeArrival("new"), true);
  assert.equal(consumeProgrammeArrival("new"), true);
  assert.equal(consumeProgrammeArrival("new"), false);
  offerProgrammeArrival("new");
  const key = "unlxck:programme-arrival";
  const saved = JSON.parse(window.sessionStorage.getItem(key)!);
  window.sessionStorage.setItem(key, JSON.stringify({...saved, document: saved.document - 1}));
  assert.equal(hasProgrammeArrival("new"), false);
  window.sessionStorage.setItem(key, JSON.stringify({...saved, at: Date.now() - 300001}));
  assert.equal(hasProgrammeArrival("new"), false);
  window.sessionStorage.setItem(key, "invalid");
  assert.equal(hasProgrammeArrival("new"), false);
  window.sessionStorage.clear();
});
