import test from "node:test";
import assert from "node:assert/strict";

import { needsInviteCheck } from "./signup-invite-guard";

test("new and existing accounts are not rechecked solely because of their age", () => {
  assert.equal(needsInviteCheck({ created_at: new Date().toISOString() }), false);
  assert.equal(needsInviteCheck({ created_at: "2025-01-01T00:00:00Z" }), false);
});

test("an account previously turned away still needs an invite", () => {
  assert.equal(needsInviteCheck({ invite_pending: true }), true);
});
