import test from "node:test";
import assert from "node:assert/strict";

import {
  isExpiredPendingGeneration,
  isStaleVisibleGenerationJob,
  normalizeLegacyGenerationJobStatus,
  resolveGenerateAutoStartDecision,
  resolveMatchingPayloadGenerationAction,
  resolvePostGenerationTarget,
  shouldBlockGenerateAutoStartForMatchingPayload,
} from "./generation-status-guards.ts";
import type { GenerationJobResponse } from "@/lib/types";

const NOW_MS = Date.parse("2026-05-23T00:00:00.000Z");

function buildJob(partial: Partial<GenerationJobResponse>): GenerationJobResponse {
  return {
    job_id: "job-1",
    athlete_id: "athlete-1",
    client_request_id: "request-1",
    status: "running",
    created_at: "2026-05-22T23:00:00.000Z",
    updated_at: "2026-05-22T23:30:00.000Z",
    started_at: "2026-05-22T23:00:00.000Z",
    heartbeat_at: "2026-05-22T23:30:00.000Z",
    ...partial,
  };
}

test("pending generation created 25 days ago is expired", () => {
  const oldPending = "2026-04-28T00:00:00.000Z";
  assert.equal(isExpiredPendingGeneration(oldPending, NOW_MS), true);
});

test("running job older than stale window is treated as stale and not active", () => {
  const staleJob = buildJob({
    status: "running",
    created_at: "2026-04-28T00:00:00.000Z",
    started_at: "2026-04-28T00:00:00.000Z",
    heartbeat_at: "2026-04-28T00:00:00.000Z",
  });
  assert.equal(isStaleVisibleGenerationJob(staleJob, NOW_MS), true);
});

test("fresh running job remains active", () => {
  const freshJob = buildJob({
    status: "running",
    created_at: "2026-05-22T23:00:00.000Z",
    updated_at: "2026-05-22T23:59:00.000Z",
    started_at: "2026-05-22T23:00:00.000Z",
    heartbeat_at: "2026-05-22T23:59:00.000Z",
  });
  assert.equal(isStaleVisibleGenerationJob(freshJob, NOW_MS), false);
});

test("recent progress milestone keeps a running job visible even if heartbeat is old", () => {
  const jobWithRecentPlannerActivity = buildJob({
    status: "running",
    created_at: "2026-05-22T23:00:00.000Z",
    updated_at: "2026-05-22T23:00:00.000Z",
    started_at: "2026-05-22T23:00:00.000Z",
    heartbeat_at: "2026-05-22T23:00:00.000Z",
    progress_milestones: [
      {
        code: "stage2_finalizer_drafting",
        label: "Stage 2 finalizer drafting",
        detail: "Sending the planning brief to the AI finalizer.",
        at: "2026-05-22T23:59:12.000Z",
      },
    ],
  });

  assert.equal(isStaleVisibleGenerationJob(jobWithRecentPlannerActivity, NOW_MS), false);
});

test("generate auto-start is blocked only when payload hash matches completed marker", () => {
  assert.equal(shouldBlockGenerateAutoStartForMatchingPayload("hash_a", "hash_a"), true);
  assert.equal(shouldBlockGenerateAutoStartForMatchingPayload("hash_a", "hash_b"), false);
  assert.equal(shouldBlockGenerateAutoStartForMatchingPayload("hash_a", null), false);
});

test("legacy generation statuses normalize to supported generation lifecycle values", () => {
  assert.equal(normalizeLegacyGenerationJobStatus("held_for_review"), "review_required");
  assert.equal(normalizeLegacyGenerationJobStatus("publishable_with_flags"), "completed");
});

test("matching payload with completed local generation redirects to existing plan", () => {
  assert.deepEqual(
    resolveMatchingPayloadGenerationAction("hash_a", { planId: "plan_123", payloadHash: "hash_a" }),
    { type: "redirect", planId: "plan_123" },
  );
});

test("matching payload without an openable plan id proceeds with a fresh generation", () => {
  assert.deepEqual(
    resolveMatchingPayloadGenerationAction("hash_a", { planId: null, payloadHash: "hash_a" }),
    { type: "proceed" },
  );
  assert.deepEqual(
    resolveMatchingPayloadGenerationAction("hash_a", { planId: "   ", payloadHash: "hash_a" }),
    { type: "proceed" },
  );
});

test("non-matching payload proceeds with a fresh generation", () => {
  assert.deepEqual(
    resolveMatchingPayloadGenerationAction("hash_a", { planId: "plan_123", payloadHash: "hash_b" }),
    { type: "proceed" },
  );
  assert.deepEqual(resolveMatchingPayloadGenerationAction("hash_a", null), { type: "proceed" });
});

test("active in-flight job is always recovered regardless of intent", () => {
  assert.equal(
    resolveGenerateAutoStartDecision({ hasActiveJob: true, hasIntent: false }),
    "recover",
  );
  assert.equal(
    resolveGenerateAutoStartDecision({ hasActiveJob: true, hasIntent: true }),
    "recover",
  );
});

test("explicit intent with no active job starts a new generation", () => {
  assert.equal(
    resolveGenerateAutoStartDecision({ hasActiveJob: false, hasIntent: true }),
    "start",
  );
});

test("no intent and no active job redirects instead of auto-starting", () => {
  assert.equal(
    resolveGenerateAutoStartDecision({ hasActiveJob: false, hasIntent: false }),
    "redirect",
  );
});

test("a finished generation opens Today only for the published, active plan", () => {
  const planId = "plan-1";
  assert.equal(resolvePostGenerationTarget({ planId, activePlanId: planId }), "/today?plan=plan-1");
  assert.equal(resolvePostGenerationTarget({ planId, activePlanId: planId, recovered: true }), "/today?plan=plan-1");
  // Not active (another saved plan is), or unknown: the plan page and its Set active.
  assert.equal(resolvePostGenerationTarget({ planId, activePlanId: "plan-0" }), "/plans/plan-1");
  assert.equal(resolvePostGenerationTarget({ planId, activePlanId: null, recovered: true }), "/plans/plan-1?recovered=1");
  // Held outcomes keep their plan-page banners even when the plan reads active.
  assert.equal(
    resolvePostGenerationTarget({ planId, activePlanId: planId, status: "review_required" }),
    "/plans/plan-1?review_required=1",
  );
  assert.equal(
    resolvePostGenerationTarget({ planId, activePlanId: planId, requiresAdminResume: true, stage2Status: "triage_blocked" }),
    "/plans/plan-1?protected_triage=1&stage2_status=triage_blocked",
  );
});
