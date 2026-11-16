import assert from "node:assert/strict";
import test from "node:test";
import { sharePlanWithFighters, type ShareAttempts } from "./custom-plan-share";

const plan = { name: "Four weeks", schedule_mode: "dated" as const, start_date: "2026-10-01", end_date: "2026-10-28",
  sessions: [{ id: "source-session", name: "Sparring", weekday: "Friday" as const, scheduled_date: "2026-10-02", session_type: "sparring" as const, exercises: [] }] };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

test("assignment retry reuses the created draft; other fighters succeed independently", async t => {
  const attempts: ShareAttempts = new Map();
  const calls: string[] = [];
  let fail = true;
  t.mock.method(globalThis, "fetch", async (input: string | URL | Request) => {
    const url = String(input); calls.push(url);
    if (url.endsWith("/assign")) return fail && url.includes("/alice/") ? json({}, 500) : json({ id: url.includes("/alice/") ? "alice-copy" : "bob-copy" });
    return json({ ...plan, id: url.includes("/alice/") ? "alice-copy" : "bob-copy" });
  });
  const first = await sharePlanWithFighters("token", plan, ["alice", "bob"], true, attempts);
  assert.equal(first[0].error, true);
  assert.equal(first[1].plan?.id, "bob-copy");
  assert.equal(attempts.get("alice")?.copy?.id, "alice-copy");
  assert.equal(attempts.has("bob"), false);
  fail = false;
  const retry = await sharePlanWithFighters("token", plan, ["alice"], true, attempts);
  assert.equal(retry[0].plan?.id, "alice-copy");
  assert.equal(calls.filter(url => url.includes("request_id=")).length, 2);
  assert.equal(calls.filter(url => url.endsWith("/alice-copy/assign")).length, 2);
  assert.equal(attempts.size, 0);
});

test("lost creation response retries the same request ID and identical copy payload", async t => {
  const attempts: ShareAttempts = new Map();
  const calls: { url: string; body: string | undefined | null }[] = [];
  t.mock.method(globalThis, "fetch", async (input: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(input), body: init?.body as string | undefined });
    if (calls.length === 1) throw new Error("Response lost after creation");
    return json({ ...plan, id: "existing-copy" });
  });
  assert.equal((await sharePlanWithFighters("token", plan, ["alice"], false, attempts))[0].error, true);
  assert.equal((await sharePlanWithFighters("token", plan, ["alice"], false, attempts))[0].plan?.id, "existing-copy");
  assert.deepEqual(calls[1], calls[0]);
  const copy = JSON.parse(calls[0].body!);
  assert.notEqual(copy.sessions[0].id, plan.sessions[0].id);
  assert.equal(copy.start_date, plan.start_date);
  assert.equal(copy.end_date, plan.end_date);
  assert.equal(copy.sessions[0].scheduled_date, plan.sessions[0].scheduled_date);
});
