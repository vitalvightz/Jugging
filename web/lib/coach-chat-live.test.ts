// Regression for Sentry JAVASCRIPT-NEXTJS-2: on iOS Safari `new WebSocket()`
// can throw SecurityError ("The operation is insecure."), and realtime-js
// rethrows it synchronously from `channel.subscribe()`. Coach chat must treat
// that as Realtime being down, never as a crash.
import assert from "node:assert/strict";
import test from "node:test";

import { joinChannel, subscribeToChat, subscribeToInbox } from "./coach-chat-live.ts";
import { getSupabaseBrowserClient } from "./supabase.ts";

// The client reads these on first use, after the imports above.
process.env.NEXT_PUBLIC_SUPABASE_URL ??= "https://stub.supabase.co";
process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ??= "stub-anon-key";

const RealWebSocket = globalThis.WebSocket;
let constructed = 0;
class RefusedWebSocket {
  constructor() {
    constructed += 1;
    throw new DOMException("The operation is insecure.", "SecurityError");
  }
}

const ME = "00000000-0000-4000-8000-000000000001";
const CHAT = "00000000-0000-4000-8000-0000000000c1";
const TOKEN = "header.eyJzdWIiOiJ1c2VyIiwiZXhwIjo0MTAyNDQ0ODAwfQ.signature";

function refuseSockets(t: { after: (fn: () => void) => void }) {
  constructed = 0;
  (globalThis as { WebSocket: unknown }).WebSocket = RefusedWebSocket;
  t.after(() => { (globalThis as { WebSocket: unknown }).WebSocket = RealWebSocket; });
}

test("a refused socket makes the inbox report Realtime down instead of throwing", t => {
  refuseSockets(t);
  const states: boolean[] = [];
  let leave: (() => void) | undefined;
  assert.doesNotThrow(() => { leave = subscribeToInbox(TOKEN, ME, "coach", () => undefined, live => states.push(live)); });
  assert.ok(constructed >= 1, "the real realtime-js connect path ran");
  assert.deepEqual(states, [false]);
  assert.doesNotThrow(() => leave?.());
  assert.equal(getSupabaseBrowserClient().getChannels().length, 0, "no channel is left behind");
});

test("a refused socket never breaks a chat: both channels fail soft and close cleanly", t => {
  refuseSockets(t);
  const states: boolean[] = [];
  let chat: ReturnType<typeof subscribeToChat> | undefined;
  assert.doesNotThrow(() => {
    chat = subscribeToChat(TOKEN, CHAT, ME, {
      onMessage: () => undefined, onChat: () => undefined, onTyping: () => undefined, onLive: live => states.push(live),
    });
  });
  assert.deepEqual(states, [false], "only the message channel decides live");
  assert.doesNotThrow(() => chat?.typing());
  assert.doesNotThrow(() => chat?.close());
  assert.equal(getSupabaseBrowserClient().getChannels().length, 0);
});

type Status = "SUBSCRIBED" | "CHANNEL_ERROR" | "TIMED_OUT" | "CLOSED";
function fakeChannel() {
  let callback: ((status: Status) => void) | undefined;
  const removed: unknown[] = [];
  const channel = { subscribe(cb: (status: Status) => void) { callback = cb; return channel; } };
  const supabase = { removeChannel: async (c: unknown) => { removed.push(c); return "ok"; } };
  return { channel, supabase, removed, emit: (status: Status) => callback?.(status) };
}

test("only SUBSCRIBED counts as live, so a dropped connection falls back and a recovery is seen", () => {
  const { channel, supabase, emit } = fakeChannel();
  const states: boolean[] = [];
  // @ts-expect-error minimal stand-ins for the Supabase client and channel
  joinChannel(supabase, channel, live => states.push(live));
  assert.deepEqual(states, [], "calling subscribe() proves nothing on its own");
  emit("SUBSCRIBED");
  emit("CHANNEL_ERROR");
  emit("TIMED_OUT");
  emit("SUBSCRIBED");
  assert.deepEqual(states, [true, false, false, true]);
});

test("after leaving, late status callbacks are ignored and the channel is removed once", async () => {
  const { channel, supabase, removed, emit } = fakeChannel();
  const states: boolean[] = [];
  // @ts-expect-error minimal stand-ins for the Supabase client and channel
  const leave = joinChannel(supabase, channel, live => states.push(live));
  leave();
  leave();
  emit("CLOSED");
  emit("SUBSCRIBED");
  await Promise.resolve();
  assert.deepEqual(states, []);
  assert.equal(removed.length, 1);
});

test("a removeChannel failure on cleanup is swallowed, not thrown into React", async () => {
  const { channel } = fakeChannel();
  const supabase = { removeChannel: () => Promise.reject(new Error("socket gone")) };
  // @ts-expect-error minimal stand-ins for the Supabase client and channel
  const leave = joinChannel(supabase, channel);
  assert.doesNotThrow(leave);
  await new Promise(resolve => setTimeout(resolve, 0));
});
