import assert from "node:assert/strict";
import test from "node:test";
import { RETRYABLE_NETWORK_MESSAGE } from "./connectivity";
import {
  clearVideoQueue, flushVideoQueue, queuedVideos, queueVideo, setVideoQueueStorage, tokenSubject,
  type QueuedVideo, type VideoQueueStorage,
} from "./video-queue";

function memory(): VideoQueueStorage & { rows: Map<string, QueuedVideo> } {
  const rows = new Map<string, QueuedVideo>();
  return {
    rows,
    all: async () => [...rows.values()],
    put: async entry => { rows.set(entry.id, entry); },
    remove: async id => { rows.delete(id); },
    clear: async () => { rows.clear(); },
  };
}
const jwt = (sub: string) => `x.${Buffer.from(JSON.stringify({ sub })).toString("base64url")}.y`;
const file = (name: string) => new File([name], `${name}.mp4`, { type: "video/mp4" });
const squat = { source: "external" as const, target_id: "session", exercise_ref: "squat" };

test("the user id comes from the access token, and junk tokens give none", () => {
  assert.equal(tokenSubject(jwt("athlete-1")), "athlete-1");
  assert.equal(tokenSubject("not-a-token"), null);
});

test("held videos belong to one athlete and one record", async () => {
  setVideoQueueStorage(memory());
  await queueVideo("a", file("one"), squat);
  await queueVideo("a", file("two"), { source: "sparring", target_id: "report" });
  await queueVideo("b", file("three"), squat);
  assert.equal((await queuedVideos("a", squat)).length, 1);
  assert.equal((await queuedVideos("a")).length, 2);
  assert.equal((await queuedVideos("b", squat)).length, 1);
  await clearVideoQueue();
  assert.equal((await queuedVideos("a")).length, 0);
});

test("back online, held videos upload oldest first and leave the device", async () => {
  const store = memory(); setVideoQueueStorage(store);
  await queueVideo("a", file("first"), squat);
  await new Promise(resolve => setTimeout(resolve, 2));
  await queueVideo("a", file("second"), squat);
  await queueVideo("b", file("other athlete"), squat);
  const sent: string[] = [];
  await flushVideoQueue(jwt("a"), async (_token, blob, type, target) => {
    sent.push(`${await blob.text()}:${type}:${target.exercise_ref}`);
    return {} as never;
  });
  assert.deepEqual(sent, ["first:video/mp4:squat", "second:video/mp4:squat"]);
  assert.deepEqual([...store.rows.values()].map(row => row.user_id), ["b"]);
});

test("a dropped connection keeps the rest for later; a refusal is kept with its reason", async () => {
  const store = memory(); setVideoQueueStorage(store);
  await queueVideo("a", file("refused"), squat);
  await new Promise(resolve => setTimeout(resolve, 2));
  await queueVideo("a", file("dropped"), squat);
  await queueVideo("a", file("never tried"), squat);
  await flushVideoQueue(jwt("a"), async (_token, blob) => {
    if (await blob.text() === "refused") throw new Error("That video could not be used. Use a clip of 30 seconds or less.");
    throw new Error(RETRYABLE_NETWORK_MESSAGE);
  });
  const rows = await queuedVideos("a");
  assert.equal(rows.length, 3);
  assert.match(rows[0].failed ?? "", /30 seconds/);
  assert.equal(rows[1].failed, undefined);
  assert.equal(rows[2].failed, undefined);
});
