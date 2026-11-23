import assert from "node:assert/strict";
import test from "node:test";

import {
  CHAT_REACTIONS, chatErrorMessage, chatLink, describeDays, EVERY_DAY, hasDay, initials, mergeMessages,
  parseQuoteParam, reactionSummary, receipt, toggleDay, WEEKDAYS, type ChatMessage,
} from "./coach-chat.ts";

const ME = "00000000-0000-4000-8000-000000000001";
const THEM = "00000000-0000-4000-8000-000000000002";
const SESSION = "00000000-0000-4000-8000-0000000000aa";

function message(changes: Partial<ChatMessage>): ChatMessage {
  return {
    id: 1, chat_id: "chat", coach_id: ME, athlete_id: THEM, sender_id: ME, client_id: "c1", kind: "text", body: "hi",
    asset_id: null, quote: null, reply_to_id: null, reactions: {}, created_at: "2026-11-23T10:00:00Z", ...changes,
  };
}

test("a confirmed send replaces its pending bubble and a Realtime echo shows once", () => {
  const pending = message({ id: -5, client_id: "c2", status: "sending", created_at: "2026-11-23T10:05:00Z" });
  const confirmed = message({ id: 7, client_id: "c2", created_at: "2026-11-23T10:05:01Z" });
  const first = message({ id: 3, client_id: "c1" });
  let list = mergeMessages([], [first, pending]);
  assert.deepEqual(list.map(m => m.id), [3, -5]);
  list = mergeMessages(list, [confirmed]);
  list = mergeMessages(list, [confirmed]);
  assert.deepEqual(list.map(m => [m.id, m.status]), [[3, undefined], [7, undefined]]);
});

test("older pages and live rows sort by id, with unsent messages last", () => {
  const failed = message({ id: -9, client_id: "f", status: "failed" });
  const list = mergeMessages([message({ id: 9, client_id: "b" }), failed], [message({ id: 2, client_id: "a" })]);
  assert.deepEqual(list.map(m => m.client_id), ["a", "b", "f"]);
});

test("a reaction update merges into the message already on screen", () => {
  const list = mergeMessages([message({ id: 4, client_id: "x" })], [message({ id: 4, client_id: "x", reactions: { [THEM]: "🔥" } })]);
  assert.deepEqual(list[0].reactions, { [THEM]: "🔥" });
});

test("receipts follow the other side's read time", () => {
  const sent = message({ created_at: "2026-11-23T10:00:00Z" });
  assert.equal(receipt(sent, null), "Delivered");
  assert.equal(receipt(sent, "2026-11-23T09:59:00Z"), "Delivered");
  assert.equal(receipt(sent, "2026-11-23T10:00:00Z"), "Read");
  assert.equal(receipt({ ...sent, status: "sending" }, null), "Sending…");
  assert.equal(receipt({ ...sent, status: "failed" }, null), "Not sent");
});

test("only well-formed session links become a training card", () => {
  assert.deepEqual(parseQuoteParam(`unlxck:${SESSION}`), { source: "unlxck", target_id: SESSION });
  assert.equal(parseQuoteParam(`plans:${SESSION}`), null);
  assert.equal(parseQuoteParam("unlxck:../../admin"), null);
  assert.equal(parseQuoteParam(null), null);
  assert.equal(chatLink("coach", THEM, { source: "external", target_id: SESSION }),
    `/coach/chats?with=${THEM}&quote=external%3A${SESSION}`);
  assert.equal(chatLink("athlete", ME), `/chat?with=${ME}`);
});

test("reactions count per emoji in the fixed order and mark the viewer's own", () => {
  const summary = reactionSummary({ [ME]: "🔥", [THEM]: "👍", other: "🔥" }, ME);
  assert.deepEqual(summary, [{ emoji: "👍", count: 1, mine: false }, { emoji: "🔥", count: 2, mine: true }]);
  assert.equal(CHAT_REACTIONS.length, 6);
});

test("only the database's own messages reach the screen", () => {
  assert.equal(chatErrorMessage(400, { code: "22023", message: "Write a message" }), "Write a message");
  assert.equal(chatErrorMessage(429, { code: "PT429", message: "Too many messages. Wait a moment." }), "Too many messages. Wait a moment.");
  assert.equal(chatErrorMessage(403, { code: "42501", message: "permission denied for table coach_messages" }), "This chat is unavailable.");
  assert.equal(chatErrorMessage(500, { code: "XX000", message: "internal detail" }), "Something went wrong. Please retry.");
  assert.equal(chatErrorMessage(401, null), "Your session expired. Sign in again.");
});

test("nudge days are a Monday-first bit mask", () => {
  assert.equal(describeDays(EVERY_DAY), "Every day");
  assert.equal(describeDays(WEEKDAYS), "Weekdays");
  const monWedFri = toggleDay(toggleDay(toggleDay(0, 0), 2), 4);
  assert.equal(describeDays(monWedFri), "Mon, Wed, Fri");
  assert.equal(hasDay(monWedFri, 1), false);
  assert.equal(toggleDay(monWedFri, 0) & 1, 0);
});

test("initials come from the first and last name", () => {
  assert.equal(initials("Leo Martins"), "LM");
  assert.equal(initials("  cher "), "C");
  assert.equal(initials(""), "?");
});
