import assert from "node:assert/strict";
import test from "node:test";

import {
  cachedInbox, cachedThread, cacheInbox, cacheThread, clearChatCache, freshLinks, mediaShape, photoBox, rememberLinks,
} from "./chat-cache.ts";
import type { ChatMessage, InboxRow } from "./coach-chat.ts";

class MemoryStorage {
  private data = new Map<string, string>();
  get length() { return this.data.size; }
  key(index: number) { return [...this.data.keys()][index] ?? null; }
  getItem(key: string) { return this.data.get(key) ?? null; }
  setItem(key: string, value: string) { this.data.set(key, value); }
  removeItem(key: string) { this.data.delete(key); }
  clear() { this.data.clear(); }
}
const storage = new MemoryStorage();
// Object.keys(localStorage) lists stored keys in browsers; mirror that.
const proxy = new Proxy(storage, {
  ownKeys: target => [...(target as unknown as { data: Map<string, string> }).data.keys()],
  getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true }),
});
(globalThis as unknown as { window: unknown }).window = { localStorage: proxy };

const row = { chat_id: "c1", other_id: "o1", name: "Coach" } as InboxRow;
function message(id: number, extra: Partial<ChatMessage> = {}): ChatMessage {
  return { id, chat_id: "c1", client_id: `k${id}`, created_at: new Date(id * 1000).toISOString(), ...extra } as ChatMessage;
}
const settle = () => new Promise(resolve => setTimeout(resolve, 350));

test("keeps the inbox and a chat's confirmed messages per account, across reloads", async () => {
  clearChatCache();
  cacheInbox("me", [row]);
  cacheThread("me", "c1", [message(1), message(2), message(-5, { status: "sending" })]);
  await settle();
  assert.ok(storage.getItem("unlxck.chat.v1.me"));

  clearChatCacheMemoryOnly();
  assert.deepEqual(cachedInbox("me"), [row]);
  assert.deepEqual(cachedThread("me", "c1")?.map(m => m.id), [1, 2]);
  assert.equal(cachedInbox("someone-else"), null);
});

test("remembers file shapes, keeps links in memory only, and forgets everything at sign-out", async () => {
  clearChatCache();
  rememberLinks("me", [{ message_id: 7, kind: "photo", url: "u", thumb_url: "t", width: 1080, height: 1920, duration_ms: null }]);
  assert.equal(freshLinks("me", 100_000)[7]?.url, "u");
  assert.deepEqual(freshLinks("me", -1), {});
  await settle();
  assert.ok(!storage.getItem("unlxck.chat.v1.me")!.includes("\"url\""));

  clearChatCacheMemoryOnly();
  assert.equal(mediaShape("me", 7)?.height, 1920);
  assert.deepEqual(freshLinks("me", 100_000), {});

  clearChatCache();
  assert.equal(storage.length, 0);
  assert.equal(cachedInbox("me"), null);
});

test("sizes photos the same before and after they load", () => {
  assert.deepEqual(photoBox(1080, 1920), { width: 180, height: 320 });
  assert.deepEqual(photoBox(1920, 1080), { width: 240, height: 135 });
  assert.deepEqual(photoBox(null, null), { width: 240, height: 240 });
});

// Simulate a fresh page load: drop the in-memory copy, keep storage.
function clearChatCacheMemoryOnly() {
  const saved = new Map<string, string>();
  for (let i = 0; i < storage.length; i += 1) saved.set(storage.key(i)!, storage.getItem(storage.key(i)!)!);
  clearChatCache();
  for (const [key, value] of saved) storage.setItem(key, value);
}
