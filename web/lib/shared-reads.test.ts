import assert from "node:assert/strict";
import test, { afterEach, beforeEach } from "node:test";

import {
  deletePlan,
  getActivePlan,
  getPlan,
  getPlanCompletions,
  getToday,
  getTrainingHistory,
  listPlans,
  recordCompliance,
  renamePlan,
  setActivePlan,
  trackProductEvent,
  submitRehabResponses,
  submitTodayCheckin,
  submitTodayInjuryCheckin,
  submitTodaySessionCompletion,
  updateMe,
} from "./api.ts";
import { SHARED_READ_MAX_AGE_MS, beginSharedReadWrite, clearSharedReads, sharedRead } from "./shared-reads.ts";

const realFetch = globalThis.fetch;
const realNow = performance.now.bind(performance);
const realConsole = { info: console.info, warn: console.warn, error: console.error };
const REUSE = { reuseForMs: SHARED_READ_MAX_AGE_MS };

type Call = { method: string; path: string; token: string | null; signal: AbortSignal };
let calls: Call[];
let clock: number;
let respond: (call: Call) => Promise<Response> | Response;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const reads = (path: string) => calls.filter((call) => call.method === "GET" && call.path === path).length;
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  console.info = () => {};
  console.warn = () => {};
  console.error = () => {};
  calls = [];
  clock = 1_000;
  performance.now = () => clock;
  clearSharedReads();
  // Each response names who it was for and how many reads preceded it.
  respond = (call) => json({ path: call.path, token: call.token, n: calls.length, active_plan: { id: "plan-1" } });
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    const headers = new Headers(init?.headers);
    const { pathname, search } = new URL(String(url), "http://127.0.0.1");
    const call = { method: init?.method ?? "GET", path: `${pathname}${search}`, token: headers.get("Authorization"), signal: init!.signal! };
    calls.push(call);
    return respond(call);
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  performance.now = realNow;
  Object.assign(console, realConsole);
});

test("identical concurrent reads by one athlete share one request", async () => {
  const [a, b] = await Promise.all([getToday("athlete-a"), getToday("athlete-a")]);
  assert.equal(reads("/api/today"), 1);
  assert.deepEqual(a, b);
  const [plans] = await Promise.all([listPlans("athlete-a"), listPlans("athlete-a")]);
  assert.equal(reads("/api/plans"), 1);
  assert.ok(plans);
});

test("different plans, cursors and query parameters never share", async () => {
  await Promise.all([
    getPlan("athlete-a", "plan-1"), getPlan("athlete-a", "plan-2"),
    getPlanCompletions("athlete-a", "plan-1"), getPlanCompletions("athlete-a", "plan-2"),
    getTrainingHistory("athlete-a"), getTrainingHistory("athlete-a", true),
    getTrainingHistory("athlete-a", true, "2026-10-01"), getTrainingHistory("athlete-a", true, "2026-09-01"),
  ]);
  assert.equal(calls.length, 8);
  assert.equal(new Set(calls.map((call) => call.path)).size, 8);
});

test("different athletes, admins and refreshed tokens never share a request or a result", async () => {
  const [a, b, admin] = await Promise.all([getToday("athlete-a"), getToday("athlete-b"), getToday("admin")]);
  assert.equal(reads("/api/today"), 3);
  assert.deepEqual([a, b, admin].map((view) => (view as unknown as { token: string }).token),
    ["Bearer athlete-a", "Bearer athlete-b", "Bearer admin"]);
  // A held result is only ever reused by the exact token that read it.
  const reusedB = await getToday("athlete-b", REUSE);
  assert.equal((reusedB as unknown as { token: string }).token, "Bearer athlete-b");
  await getToday("athlete-a-refreshed", REUSE);
  assert.equal(reads("/api/today"), 4);
});

test("held results serve only callers that opt in, and only briefly", async () => {
  const first = await getPlan("athlete-a", "plan-1");
  // A plain read (Home, Today, a background poll) always reaches the server.
  await getPlan("athlete-a", "plan-1");
  assert.equal(reads("/api/plans/plan-1"), 2);
  const reused = await getPlan("athlete-a", "plan-1", REUSE);
  assert.equal(reads("/api/plans/plan-1"), 2);
  assert.notDeepEqual(reused, first); // The latest read is the one reused.
  clock += SHARED_READ_MAX_AGE_MS + 1;
  await getPlan("athlete-a", "plan-1", REUSE);
  assert.equal(reads("/api/plans/plan-1"), 3);
  // A caller can ask for less than the maximum age.
  clock += 5_000;
  await getPlan("athlete-a", "plan-1", { reuseForMs: 1_000 });
  assert.equal(reads("/api/plans/plan-1"), 4);
});

test("completion rows, plan lists and history are never reused once resolved", async () => {
  await getPlanCompletions("athlete-a", "plan-1");
  await getPlanCompletions("athlete-a", "plan-1");
  await listPlans("athlete-a");
  await listPlans("athlete-a");
  await getTrainingHistory("athlete-a", true);
  await getTrainingHistory("athlete-a", true);
  assert.equal(calls.length, 6);
});

for (const [name, write] of [
  ["plan rename", (token: string) => renamePlan(token, "plan-1", "New name")],
  ["plan activation", (token: string) => setActivePlan(token, "plan-1")],
  ["plan archive", (token: string) => deletePlan(token, "plan-1")],
  ["session completion", (token: string) => submitTodaySessionCompletion(token, {} as never)],
  ["readiness check-in", (token: string) => submitTodayCheckin(token, {} as never)],
  ["injury update", (token: string) => submitTodayInjuryCheckin(token, {} as never)],
  ["delayed rehab answer", (token: string) => submitRehabResponses(token, {} as never)],
  ["profile or timezone change", (token: string) => updateMe(token, {} as never)],
  ["health-consent withdrawal", (token: string) => recordCompliance(token, {} as never)],
] as const) {
  test(`${name} makes earlier reads unusable`, async () => {
    await Promise.all([getToday("athlete-a"), getPlan("athlete-a", "plan-1"), getActivePlan("athlete-a")]);
    await write("athlete-a");
    await Promise.all([getToday("athlete-a", REUSE), getPlan("athlete-a", "plan-1", REUSE), getActivePlan("athlete-a", REUSE)]);
    assert.equal(reads("/api/today"), 2);
    assert.equal(reads("/api/plans/plan-1"), 2);
    assert.equal(reads("/api/plans/active"), 2);
  });
}

test("a write invalidates reads for every token, and analytics events do not", async () => {
  await Promise.all([getToday("athlete-a"), getToday("athlete-b")]);
  await trackProductEvent("athlete-a", "app_opened", "onboarding");
  await Promise.all([getToday("athlete-a", REUSE), getToday("athlete-b", REUSE)]);
  assert.equal(reads("/api/today"), 2);
  await renamePlan("athlete-b", "plan-1", "Renamed");
  await Promise.all([getToday("athlete-a", REUSE), getToday("athlete-b", REUSE)]);
  assert.equal(reads("/api/today"), 4);
});

test("a read never joins one that started before a write, nor shares during it", async () => {
  const pending: Array<() => void> = [];
  respond = (call) => call.method === "GET"
    ? new Promise((resolve) => pending.push(() => resolve(json({ n: calls.indexOf(call) }))))
    : json({});
  const before = getToday("athlete-a");
  await flush();
  const endWrite = beginSharedReadWrite();
  const during = [getToday("athlete-a"), getToday("athlete-a")];
  await flush();
  assert.equal(reads("/api/today"), 3);
  endWrite();
  const after = getToday("athlete-a");
  await flush();
  assert.equal(reads("/api/today"), 4);
  pending.forEach((resolve) => resolve());
  const [old, , , fresh] = await Promise.all([before, ...during, after]);
  assert.notDeepEqual(old, fresh);
  // Results read across a write are not held for reuse either.
  await getToday("athlete-a", REUSE);
  assert.equal(reads("/api/today"), 4);
  const late = getToday("athlete-a");
  const lateEnd = beginSharedReadWrite();
  lateEnd();
  await flush();
  pending.at(-1)!();
  await late;
  const check = getToday("athlete-a", REUSE);
  await flush();
  assert.equal(reads("/api/today"), 6);
  pending.at(-1)!();
  await check;
});

test("failed and timed-out reads are dropped and retried, never reused", async () => {
  respond = () => json({ detail: "Not found" }, 404);
  await assert.rejects(getPlan("athlete-a", "plan-1", REUSE));
  await assert.rejects(getPlan("athlete-a", "plan-1", REUSE));
  assert.equal(reads("/api/plans/plan-1"), 2);
  // Timeout: concurrent callers share the failure, the next call starts afresh.
  process.env.NEXT_PUBLIC_API_REQUEST_TIMEOUT_MS = "20";
  respond = (call) => new Promise((_resolve, reject) => {
    call.signal.addEventListener("abort", () => reject(new DOMException("timed out", "AbortError")));
  });
  try {
    const results = await Promise.allSettled([getPlanCompletions("athlete-a", "plan-1"), getPlanCompletions("athlete-a", "plan-1")]);
    assert.deepEqual(results.map((result) => result.status), ["rejected", "rejected"]);
  } finally {
    delete process.env.NEXT_PUBLIC_API_REQUEST_TIMEOUT_MS;
  }
  const failedAttempts = reads("/api/plans/plan-1/completions");
  respond = (call) => json({ ok: true, path: call.path });
  assert.deepEqual(await getPlanCompletions("athlete-a", "plan-1"), { ok: true, path: "/api/plans/plan-1/completions" });
  assert.equal(reads("/api/plans/plan-1/completions"), failedAttempts + 1);
});

test("one consumer cancelling leaves the shared read running for the other", async () => {
  let release!: () => void;
  respond = (call) => new Promise((resolve, reject) => {
    release = () => resolve(json({ plan_id: "plan-1" }));
    call.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });
  const leaving = new AbortController();
  const first = getPlan("athlete-a", "plan-1", { signal: leaving.signal });
  const second = getPlan("athlete-a", "plan-1");
  await flush();
  leaving.abort();
  await assert.rejects(first, { name: "AbortError" });
  assert.equal(calls[0].signal.aborted, false);
  release();
  assert.deepEqual(await second, { plan_id: "plan-1" });
  assert.equal(calls.length, 1);
});

test("the last consumer cancelling aborts the request and a new caller starts again", async () => {
  respond = (call) => new Promise((_resolve, reject) => {
    call.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });
  const a = new AbortController(); const b = new AbortController();
  const first = getPlan("athlete-a", "plan-1", { signal: a.signal });
  const second = getPlan("athlete-a", "plan-1", { signal: b.signal });
  await flush();
  a.abort();
  assert.equal(calls[0].signal.aborted, false);
  b.abort();
  await Promise.allSettled([first, second]);
  assert.equal(calls[0].signal.aborted, true);
  respond = () => json({ plan_id: "plan-1" });
  assert.deepEqual(await getPlan("athlete-a", "plan-1"), { plan_id: "plan-1" });
  assert.equal(calls.length, 2);
  // An already-cancelled caller gets nothing and starts nothing.
  const done = new AbortController(); done.abort();
  await assert.rejects(getPlan("athlete-a", "plan-1", { signal: done.signal, ...REUSE }), { name: "AbortError" });
});

test("sign-out forgets held results and the number held stays bounded", async () => {
  await getToday("athlete-a");
  clearSharedReads();
  await getToday("athlete-a", REUSE);
  assert.equal(reads("/api/today"), 2);
  for (let index = 0; index < 20; index += 1) await getPlan("athlete-a", `plan-${index}`);
  await getPlan("athlete-a", "plan-19", REUSE);
  await getPlan("athlete-a", "plan-0", REUSE); // Evicted: the oldest held result went first.
  assert.equal(reads("/api/plans/plan-19"), 1);
  assert.equal(reads("/api/plans/plan-0"), 2);
});

test("an already-cancelled caller never starts a cold read", async () => {
  const unhandled: unknown[] = [];
  const onUnhandled = (reason: unknown) => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandled);
  try {
    const done = new AbortController(); done.abort();
    let loads = 0;
    // A load that would fail: it must not run, so nothing can go unhandled.
    const failing = async () => { loads += 1; throw new Error("unobserved failure"); };
    await assert.rejects(sharedRead("t", "/cold", failing, { signal: done.signal }), { name: "AbortError" });
    await assert.rejects(sharedRead("t", "/cold", failing, { signal: done.signal, hold: true, reuseForMs: SHARED_READ_MAX_AGE_MS }), { name: "AbortError" });
    assert.equal(loads, 0);
    // Through the API client: no network request for a cold plan or completions read.
    await assert.rejects(getPlan("athlete-a", "cold-plan", { signal: done.signal }), { name: "AbortError" });
    await assert.rejects(getPlan("athlete-a", "cold-plan", { signal: done.signal, ...REUSE }), { name: "AbortError" });
    assert.equal(calls.length, 0);
    // The key is not left behind: the next live caller starts a real read.
    respond = () => json({ plan_id: "cold-plan" });
    assert.deepEqual(await getPlan("athlete-a", "cold-plan"), { plan_id: "cold-plan" });
    assert.equal(calls.length, 1);
    await flush();
    assert.deepEqual(unhandled, []);
  } finally {
    process.off("unhandledRejection", onUnhandled);
  }
});

test("completed in-flight entries are removed promptly", async () => {
  let loads = 0;
  const load = async () => ++loads;
  assert.equal(await sharedRead("t", "/x", load), 1);
  assert.equal(await sharedRead("t", "/x", load), 2);
  await assert.rejects(sharedRead("t", "/y", async () => { throw new Error("boom"); }));
  assert.equal(await sharedRead("t", "/y", load), 3);
});
