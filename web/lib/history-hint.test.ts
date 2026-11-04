import test from "node:test";
import assert from "node:assert/strict";

import { announceSessionLogged, armHistoryHint, historyHintKey, markHistoryHintSeen, readStoredFlag } from "./history-hint.ts";

function storage() {
  const values = new Map<string, string>();
  return { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => void values.set(key, value), removeItem: (key: string) => void values.delete(key) };
}

test("the hint is armed and retired per athlete", () => {
  const previous = (globalThis as { window?: unknown }).window;
  (globalThis as { window?: unknown }).window = { localStorage: storage(), sessionStorage: storage(), dispatchEvent: () => true };
  try {
    announceSessionLogged();
    armHistoryHint("athlete-a");
    assert.equal(readStoredFlag(historyHintKey("athlete-a")), "armed");
    // Another athlete on the same device keeps their own first-time hint.
    assert.equal(readStoredFlag(historyHintKey("athlete-b")), null);
    markHistoryHintSeen("athlete-a");
    announceSessionLogged();
    armHistoryHint("athlete-a");
    assert.equal(readStoredFlag(historyHintKey("athlete-a")), "seen");
    armHistoryHint("athlete-b");
    assert.equal(readStoredFlag(historyHintKey("athlete-b")), null, "the pending save was consumed by athlete-a");
    announceSessionLogged();
    armHistoryHint("athlete-b");
    assert.equal(readStoredFlag(historyHintKey("athlete-b")), "armed");
  } finally {
    (globalThis as { window?: unknown }).window = previous;
  }
});
