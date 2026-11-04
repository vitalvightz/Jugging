import test from "node:test";
import assert from "node:assert/strict";
import { observeUsage } from "./usage-tracking";

test("hidden loads and navigation do not invent views; foregrounding records the actual page", () => {
  const source = Object.assign(new EventTarget(), { visibilityState: "hidden" as DocumentVisibilityState });
  let path = "/plans/private-plan?anything=secret";
  const events: string[][] = [];
  const observer = observeUsage(source, () => path, (event, feature) => events.push([event, feature]));
  assert.deepEqual(events, []);
  path = "/history";
  observer.pageChanged();
  assert.deepEqual(events, []);
  source.visibilityState = "visible";
  source.dispatchEvent(new Event("visibilitychange"));
  assert.deepEqual(events, [["app_opened", "onboarding"], ["page_viewed", "history"]]);
  observer.pageChanged(); // token refresh or render must not produce another view
  assert.equal(events.length, 2);
  path = "/plans/private-plan?anything=secret";
  observer.pageChanged();
  assert.deepEqual(events.at(-1), ["page_viewed", "plans"]);
  assert.doesNotMatch(JSON.stringify(events), /private-plan|secret/);
  observer.dispose();
  source.dispatchEvent(new Event("visibilitychange"));
  assert.equal(events.length, 3);
});
