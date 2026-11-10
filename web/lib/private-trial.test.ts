import test from "node:test";
import assert from "node:assert/strict";

import { getAthleteWorkspaceHref, getAuthenticatedLandingHref } from "@/lib/auth-routing";
import {
  PRIVATE_TRIAL_CHECKS,
  PRIVATE_TRIAL_DUTIES,
  requiresPrivateTrialAcknowledgement,
} from "@/lib/private-trial";
import type { MeResponse, UserRole } from "@/lib/types";

function meFixture({
  role = "athlete" as UserRole,
  ack = null as string | null,
  planId = null as string | null,
}): MeResponse {
  return {
    profile: {
      role,
      private_trial_ack_at: ack,
      // A signed-up athlete has already given their date of birth and accepted
      // the Terms — the consent gate sits ahead of the trial briefing, so these
      // must be satisfied for the briefing to be the next stop.
      date_of_birth: "1996-05-04",
      meets_minimum_age: true,
      terms_accepted: true,
    },
    latest_plan: planId ? { plan_id: planId } : null,
  } as unknown as MeResponse;
}

test("an athlete who has not acknowledged the briefing is gated", () => {
  assert.equal(requiresPrivateTrialAcknowledgement(meFixture({})), true);
});

test("an acknowledged athlete is never gated again", () => {
  const me = meFixture({ ack: "2026-08-06T09:00:00Z" });
  assert.equal(requiresPrivateTrialAcknowledgement(me), false);
});

test("non-athlete roles are never gated", () => {
  for (const role of ["admin", "coach", "gym_owner"] as UserRole[]) {
    assert.equal(requiresPrivateTrialAcknowledgement(meFixture({ role })), false);
  }
});

test("an unresolved profile is not gated, so loading never stalls behind the screen", () => {
  assert.equal(requiresPrivateTrialAcknowledgement(null), false);
});

test("the briefing is not a gate: a fresh sign-up goes straight to Quick Build", () => {
  assert.equal(getAuthenticatedLandingHref(meFixture({})), "/quick-build");
});

test("an athlete with a plan lands on Today, briefed or not", () => {
  assert.equal(getAuthenticatedLandingHref(meFixture({ planId: "plan-9" })), "/today");
  const acknowledged = meFixture({ planId: "plan-9", ack: "2026-08-06T09:00:00Z" });
  assert.equal(getAuthenticatedLandingHref(acknowledged), "/today");
});

test("the workspace for an athlete with no plan yet is Quick Build", () => {
  assert.equal(getAthleteWorkspaceHref(meFixture({})), "/quick-build");
});

test("acknowledging continues to Today when a plan exists", () => {
  assert.equal(getAthleteWorkspaceHref(meFixture({ planId: "plan-9" })), "/today");
});

test("role destinations still win over the trial gate", () => {
  assert.equal(getAuthenticatedLandingHref(meFixture({ role: "admin" })), "/admin");
  assert.equal(getAuthenticatedLandingHref(meFixture({ role: "coach" })), "/coach");
  assert.equal(getAuthenticatedLandingHref(meFixture({ role: "gym_owner" })), "/gym-owner");
});

test("the briefing covers what the tester must do and what they must check", () => {
  assert.equal(PRIVATE_TRIAL_DUTIES.length, 5);
  assert.equal(PRIVATE_TRIAL_CHECKS.length, 5);
  assert.ok(PRIVATE_TRIAL_DUTIES.some((duty) => duty.includes("feedback after completed sessions")));
  assert.ok(PRIVATE_TRIAL_CHECKS.some((check) => check.includes("difficulty")));
});
