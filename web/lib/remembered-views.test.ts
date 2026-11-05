import assert from "node:assert/strict";
import test, { afterEach } from "node:test";

import {
  REMEMBERED_VIEW_MAX_AGE_MS,
  beginSharedReadWrite,
  clearSharedReads,
  readEpoch,
  recallView,
  rememberView,
  sharedRead,
} from "./shared-reads.ts";

const realNow = performance.now.bind(performance);
afterEach(() => {
  performance.now = realNow;
  clearSharedReads();
});

const read = (token: string, path: string, value: unknown, remember = true) =>
  sharedRead(token, path, async () => value, { remember });

test("a remembered view is recalled for the same token and path only", async () => {
  await read("token-a", "/api/today", { day: 1 });
  assert.deepEqual(recallView("token-a", "/api/today"), { day: 1 });
  assert.equal(recallView("token-b", "/api/today"), null);
  assert.equal(recallView("token-a", "/api/training/history"), null);
});

test("reads that do not ask to be remembered are never recalled", async () => {
  await read("token-a", "/api/plans", [1], false);
  assert.equal(recallView("token-a", "/api/plans"), null);
});

test("any write drops remembered views, including one in flight", async () => {
  await read("token-a", "/api/today", { day: 1 });
  const endWrite = beginSharedReadWrite();
  assert.equal(recallView("token-a", "/api/today"), null);
  // A read that ran while the write was in flight is not remembered either.
  await read("token-a", "/api/today", { day: 2 });
  endWrite();
  assert.equal(recallView("token-a", "/api/today"), null);
  await read("token-a", "/api/today", { day: 3 });
  assert.deepEqual(recallView("token-a", "/api/today"), { day: 3 });
});

test("a read that started before a write is not remembered", async () => {
  let resolve!: (value: unknown) => void;
  const pending = sharedRead("token-a", "/api/today", () => new Promise(done => { resolve = done; }), { remember: true });
  beginSharedReadWrite()();
  resolve({ day: "pre-write" });
  await pending;
  assert.equal(recallView("token-a", "/api/today"), null);
});

test("sign-out clears remembered views", async () => {
  await read("token-a", "/api/today", { day: 1 });
  clearSharedReads();
  assert.equal(recallView("token-a", "/api/today"), null);
});

test("remembered views expire", async () => {
  let now = 1_000;
  performance.now = () => now;
  await read("token-a", "/api/today", { day: 1 });
  now += REMEMBERED_VIEW_MAX_AGE_MS + 1;
  assert.equal(recallView("token-a", "/api/today"), null);
});

test("a failed read is never remembered", async () => {
  await read("token-a", "/api/today", { day: 1 });
  await assert.rejects(sharedRead("token-a", "/api/today", async () => { throw new Error("down"); }, { remember: true }));
  assert.deepEqual(recallView("token-a", "/api/today"), { day: 1 });
});

test("a screen view is remembered only if no write started since its reads began", () => {
  const before = readEpoch();
  rememberView("token-a", "screen:plans", { plans: [1] }, before);
  assert.deepEqual(recallView("token-a", "screen:plans"), { plans: [1] });

  const started = readEpoch();
  beginSharedReadWrite()();
  rememberView("token-a", "screen:plans", { plans: ["pre-write"] }, started);
  assert.equal(recallView("token-a", "screen:plans"), null);
});

test("no screen view is remembered while a write is in flight", () => {
  const endWrite = beginSharedReadWrite();
  assert.equal(readEpoch(), -1);
  rememberView("token-a", "screen:plans", { plans: [1] }, readEpoch());
  endWrite();
  assert.equal(recallView("token-a", "screen:plans"), null);
});
