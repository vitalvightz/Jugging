import assert from "node:assert/strict";
import test from "node:test";

import {
  appendNavigation,
  isRouterPrefetch,
  routeRequestBlocked,
  withBlockedRoutes,
  median,
  resourcePhases,
  sanitizePath,
  sanitizeRequestId,
  summarize,
  type PerfNavigation,
} from "./perf-diagnostics.ts";

test("paths lose ids, numbers and every query value", () => {
  assert.equal(sanitizePath("/api/plans/41e5d6a9-8929-41e8-a0f4-fd6911cd03fd"), "/api/plans/:id");
  assert.equal(sanitizePath("https://app.unlxck.com/api/training/history?include_skipped=true&limit=200&before_day=2026-11-01"),
    "/api/training/history?before_day=&include_skipped=&limit=");
  assert.equal(sanitizePath("/api/training/sessions/12345/exercises/abc9"), "/api/training/sessions/:id/exercises/:id");
  assert.equal(sanitizePath("/history?_rsc=QkvofcDP00o4DU4j"), "/history");
  assert.equal(sanitizePath("/api/me?email=athlete@example.com&token=secret"), "/api/me?email=&token=");
});

const nav = (to: string, content: number | null, settled: number | null, api: number[] = []): PerfNavigation => ({
  from: "/", to, at: "2026-11-05T00:52:00.000Z", routeChanged: 100, firstFrame: 110, firstContent: content,
  placeholdersGone: settled, placeholdersAtStart: 0, blockedMs: 0, longestFrameMs: 16, timedOut: false,
  requests: api.map(end => ({ kind: "api" as const, path: "/api/today", start: 0, ttfb: null, end, bytes: null, status: null, server: [] })),
});

test("the summary gives per-route medians of what the athlete saw", () => {
  const summary = summarize([nav("/", 150, 1200, [1100]), nav("/", 170, 1300, [1250]), nav("/history", 160, 160)]);
  assert.deepEqual(summary.find(row => row.route === "/"), {
    route: "/", visits: 2, medianRouteMs: 100, medianContentMs: 170, medianSettledMs: 1300, medianApiMs: 1250,
  });
  assert.equal(summary.find(row => row.route === "/history")?.medianApiMs, null);
});

test("the log is bounded", () => {
  let log: PerfNavigation[] = [];
  for (let index = 0; index < 60; index++) log = appendNavigation(log, nav(`/r${index}`, 1, 1));
  assert.equal(log.length, 40);
  assert.equal(log[0].to, "/r20");
  assert.equal(median([null, 3, 1, 2]), 2);
});

test("request ids are kept only when they look like the API's random id", () => {
  assert.equal(sanitizeRequestId("3f9a01bc"), "3f9a01bc");
  assert.equal(sanitizeRequestId(null), null);
  assert.equal(sanitizeRequestId("athlete@example.com"), null);
  assert.equal(sanitizeRequestId("x".repeat(37)), null);
});

test("resource timing splits a request into setup, wait and download", () => {
  assert.deepEqual(resourcePhases({ startTime: 100, requestStart: 140.4, responseStart: 790.6, responseEnd: 812 }),
    { setup: 40, wait: 650, download: 21 });
  // Detail withheld (requestStart 0) or no entry: nothing rather than a wrong split.
  assert.equal(resourcePhases({ startTime: 100, requestStart: 0, responseStart: 0, responseEnd: 900 }), null);
  assert.equal(resourcePhases(undefined), null);
});

test("router prefetches are told apart from navigation payload requests", () => {
  assert.equal(isRouterPrefetch({ "Next-Router-Prefetch": "1", RSC: "1" }), true);
  assert.equal(isRouterPrefetch(new Headers({ "next-router-prefetch": "1" })), true);
  assert.equal(isRouterPrefetch([["rsc", "1"]]), false);
  assert.equal(isRouterPrefetch(undefined), false);
});

test("a route request counts as blocking only when the screen changed after its response", () => {
  // Started at the tap, answered at 310ms, screen changed at 330ms: the navigation waited.
  assert.equal(routeRequestBlocked({ start: 2, ttfb: 310 }, 330), true);
  // Screen changed at 20ms from the cache; the request is background work.
  assert.equal(routeRequestBlocked({ start: 2, ttfb: 310 }, 20), false);
  // Started after the screen changed (e.g. prefetching the next tabs).
  assert.equal(routeRequestBlocked({ start: 40, ttfb: 60 }, 20), false);
  // Not enough to judge.
  assert.equal(routeRequestBlocked({ start: 2, ttfb: null }, 330), null);
  assert.equal(routeRequestBlocked({ start: 2, ttfb: 310 }, null), null);
});

test("only route requests are marked", () => {
  const visit = nav("/today", 400, 900, [800]);
  visit.routeChanged = 330;
  visit.requests.push({ kind: "route", path: "/today", start: 2, ttfb: 310, end: 320, bytes: 1, status: 200, server: [], prefetch: false });
  const marked = withBlockedRoutes(visit).requests;
  assert.equal(marked[0].blockedRoute, undefined);
  assert.equal(marked[1].blockedRoute, true);
});
