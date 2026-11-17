import test from "node:test";
import assert from "node:assert/strict";

import "./test-dom";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { publishGenerationTerminalJob } from "../lib/generation-terminal-event";
import type { GenerationJobResponse } from "../lib/types";
import { AppSessionContext } from "./auth-provider";
import { GenerationStatusProvider } from "./generation-status-provider";
import { GlobalGenerationStatus } from "./global-generation-status";

// A finished build must still say how long it took. The ribbon rendered its
// elapsed reading only on the live/completed content block, so every terminal
// branch — the failure ribbon and all five passive ones — dropped the number
// the moment it mattered. These tests mount the real ribbon and assert the
// total is present, and that it survives the active-to-passive handoff
// unchanged: the active path reads the provider's frozen endedAtMs, the
// passive path recovers the same window from the job row's own timestamps.

const TOKEN = "test-token";
const PENDING_PREFIX = "unlxck:pending-generation:";
const RIBBON_DISMISSED_PREFIX = "unlxck:generation-ribbon-dismissed";
const ELAPSED_MS = 292_000;
const TOTAL_OFFSET_MS = 279_000;
const EXPECTED_TOTAL = "4m 39s";

const fakeSession = {
  session: { access_token: TOKEN } as never,
  loading: false,
  me: null,
  previewAppearanceMode: () => {},
  refreshMe: async () => {},
  replaceMe: () => {},
  signOut: async () => {},
} as never;

type Fixture = {
  runningJob: GenerationJobResponse;
  terminalJob: (overrides: Partial<GenerationJobResponse>) => GenerationJobResponse;
};

function buildFixture(nowMs = Date.now()): Fixture {
  const startedAtMs = nowMs - ELAPSED_MS;
  const iso = (ms: number) => new Date(ms).toISOString();

  const runningJob: GenerationJobResponse = {
    job_id: "job-total-1",
    athlete_id: "athlete-1",
    client_request_id: "req-total-1",
    status: "running",
    created_at: iso(startedAtMs),
    updated_at: iso(nowMs - 5_000),
    started_at: iso(startedAtMs),
    heartbeat_at: iso(nowMs - 5_000),
    progress_milestones: [
      { code: "stage2_model_call", label: "Stage 2 model call", detail: "", at: iso(startedAtMs + 66_000) },
    ],
  };

  return {
    runningJob,
    terminalJob: (overrides) => ({
      ...runningJob,
      updated_at: iso(startedAtMs + TOTAL_OFFSET_MS),
      completed_at: iso(startedAtMs + TOTAL_OFFSET_MS),
      ...overrides,
    }),
  };
}

type BackendState = {
  active: GenerationJobResponse | null;
  job: GenerationJobResponse | null;
  latest: GenerationJobResponse | null;
};

function installFetchStub(state: BackendState): () => void {
  const originalFetch = globalThis.fetch;

  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(typeof input === "string" || input instanceof URL ? input : input.url);
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });

    if (url.includes("/api/generation-jobs/active")) {
      return json(state.active);
    }
    if (url.includes("/api/generation-jobs/latest")) {
      return json(state.latest);
    }
    if (url.includes("/api/generation-jobs/")) {
      return json(state.job);
    }
    if (url.includes("/api/plans/active")) {
      return json(null);
    }
    throw new Error(`unexpected request: ${url}`);
  }) as typeof fetch;

  return () => {
    globalThis.fetch = originalFetch;
  };
}

function clearRibbonStorage(): void {
  Object.keys(window.localStorage)
    .filter((key) => key.startsWith(PENDING_PREFIX) || key.startsWith(RIBBON_DISMISSED_PREFIX))
    .forEach((key) => window.localStorage.removeItem(key));
}

// The ribbon resolves its dismiss key on a `setTimeout(0)`, and React defers
// updates made outside an act() scope until that scope exits — so flushing
// microtasks is not enough to see the settled DOM. Two macrotask turns: the
// first lands the queued state and runs effects, the second lets the 0ms timer
// those effects scheduled fire.
async function settle() {
  for (let turn = 0; turn < 2; turn += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

async function wait(ms: number) {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
  await settle();
}

type Harness = { text: () => string; unmount: () => Promise<void> };

async function mountRibbon(): Promise<Harness> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root: Root = createRoot(container);

  await act(async () => {
    root.render(
      <AppSessionContext.Provider value={fakeSession}>
        <GenerationStatusProvider token={TOKEN}>
          <GlobalGenerationStatus />
        </GenerationStatusProvider>
      </AppSessionContext.Provider>,
    );
  });
  // Let the provider's one-shot mount check land before assertions.
  await wait(1_200);

  return {
    text: () => container.textContent ?? "",
    unmount: async () => {
      await act(async () => {
        root.unmount();
      });
      container.remove();
    },
  };
}

// Each case renders a different passive branch of the ribbon.
const passiveCases: { name: string; job: Partial<GenerationJobResponse>; expectText: string }[] = [
  {
    name: "review-required plan",
    job: { status: "review_required", plan_id: "plan-1" },
    expectText: "Review saved plan",
  },
  {
    name: "protected triage hold with no plan",
    job: {
      status: "review_required",
      plan_id: null,
      latest_plan_id: null,
      requires_admin_resume: true,
      stage2_status: "triage_blocked",
    },
    // The harness session carries no profile, so the ribbon renders for a
    // non-admin viewer: they get the waiting notice, not the admin link.
    expectText: "Your plan is with an admin for review.",
  },
  {
    name: "retryable failure with no plan",
    job: {
      status: "failed",
      plan_id: null,
      latest_plan_id: null,
      can_retry: true,
      error: "Stage 2 worker exited.",
    },
    expectText: "Retry",
  },
  {
    name: "failure that still saved a plan",
    job: { status: "failed", plan_id: "plan-recovered-1", error: "Stage 2 worker exited." },
    expectText: "Your plan is saved and ready.",
  },
  {
    name: "completed job whose plan only exists as latest_plan_id",
    job: { status: "completed", plan_id: null, latest_plan_id: "plan-latest-1" },
    expectText: "Your plan is saved and ready.",
  },
];
// Not covered here: the ribbon's "finished but could not be opened" branch.
// shouldRetainLatestJob() drops a completed job with no plan id anywhere, so
// the provider never hands that job to the ribbon. The branch still renders a
// total, but there is no route to it to assert against.

for (const passiveCase of passiveCases) {
  test(`the passive ribbon shows the frozen total for a ${passiveCase.name}`, async () => {
    clearRibbonStorage();
    const { terminalJob } = buildFixture();
    const job = terminalJob(passiveCase.job);
    const restoreFetch = installFetchStub({ active: null, job, latest: job });
    const harness = await mountRibbon();

    try {
      const text = harness.text();
      assert.ok(
        text.includes(passiveCase.expectText),
        `expected the ${passiveCase.name} ribbon, got: ${text}`,
      );
      assert.ok(
        text.includes(EXPECTED_TOTAL),
        `expected total build time ${EXPECTED_TOTAL} in: ${text}`,
      );
    } finally {
      await harness.unmount();
      restoreFetch();
      clearRibbonStorage();
    }
  });
}

test("the total survives the active-to-passive handoff unchanged", async () => {
  clearRibbonStorage();
  const { runningJob, terminalJob } = buildFixture();
  const reviewJob = terminalJob({ status: "review_required", plan_id: "plan-1" });
  const state: BackendState = { active: runningJob, job: runningJob, latest: null };
  const restoreFetch = installFetchStub(state);
  const harness = await mountRibbon();

  try {
    assert.ok(harness.text().includes("Generating plan..."), harness.text());
    assert.equal(harness.text().includes("Open"), false, `building ribbon showed an Open action: ${harness.text()}`);

    // The controller resolves the job and clears the shared pending record.
    state.active = null;
    state.job = reviewJob;
    state.latest = reviewJob;
    Object.keys(window.localStorage)
      .filter((key) => key.startsWith(PENDING_PREFIX))
      .forEach((key) => window.localStorage.removeItem(key));

    await act(async () => {
      publishGenerationTerminalJob(reviewJob);
    });
    await settle();

    // Active terminal ribbon: total read from the provider's frozen endedAtMs.
    const activeText = harness.text();
    assert.ok(activeText.includes("Plan ready for review."), activeText);
    assert.ok(activeText.includes(EXPECTED_TOTAL), `active ribbon lost the total: ${activeText}`);

    // The provider hands the job off to the passive ribbon after its dwell.
    await wait(5_600);

    const passiveText = harness.text();
    assert.ok(passiveText.includes("Review saved plan"), `expected the passive ribbon: ${passiveText}`);
    // Same number, now recovered from the job row's own timestamps.
    assert.ok(
      passiveText.includes(EXPECTED_TOTAL),
      `the total vanished across the handoff: ${passiveText}`,
    );
  } finally {
    await harness.unmount();
    restoreFetch();
    clearRibbonStorage();
  }
});

test("an active failure keeps the total next to the failure copy", async () => {
  clearRibbonStorage();
  const { runningJob, terminalJob } = buildFixture();
  const failedJob = terminalJob({
    status: "failed",
    plan_id: null,
    latest_plan_id: null,
    can_retry: true,
    error: "Stage 2 worker exited.",
  });
  const state: BackendState = { active: runningJob, job: runningJob, latest: null };
  const restoreFetch = installFetchStub(state);
  const harness = await mountRibbon();

  try {
    assert.ok(harness.text().includes("Generating plan..."), harness.text());

    state.active = null;
    state.job = failedJob;
    state.latest = failedJob;

    await act(async () => {
      publishGenerationTerminalJob(failedJob);
    });
    await settle();

    const text = harness.text();
    assert.ok(text.includes("Your plan build stopped."), text);
    assert.ok(text.includes(EXPECTED_TOTAL), `the failure ribbon lost the total: ${text}`);
  } finally {
    await harness.unmount();
    restoreFetch();
    clearRibbonStorage();
  }
});

// Closing an outcome ribbon used to erase it outright. For a build held for
// admin review that left the athlete with no trace of it anywhere; the reopen
// pill keeps one, while a finished plan they closed stays closed.
const dismissedCases: { name: string; job: Partial<GenerationJobResponse>; expectPill: string | null }[] = [
  {
    name: "protected triage hold",
    job: { status: "review_required", plan_id: null, latest_plan_id: null, requires_admin_resume: true },
    expectPill: "Show plan build",
  },
  {
    name: "retryable failure",
    job: { status: "failed", plan_id: null, latest_plan_id: null, can_retry: true, error: "Stage 2 worker exited." },
    expectPill: "Show build error",
  },
  {
    name: "saved plan",
    job: { status: "review_required", plan_id: "plan-1" },
    expectPill: null,
  },
];

for (const dismissedCase of dismissedCases) {
  test(`closing the ribbon for a ${dismissedCase.name} ${dismissedCase.expectPill ? "leaves the reopen pill" : "stays closed"}`, async () => {
    clearRibbonStorage();
    const { terminalJob } = buildFixture();
    const job = terminalJob(dismissedCase.job);
    window.localStorage.setItem(`${RIBBON_DISMISSED_PREFIX}:${job.job_id}`, "1");
    const restoreFetch = installFetchStub({ active: null, job, latest: job });
    const harness = await mountRibbon();

    try {
      const text = harness.text();
      if (dismissedCase.expectPill) {
        assert.ok(text.includes(dismissedCase.expectPill), `expected the reopen pill, got: ${text}`);
      } else {
        assert.equal(text.trim(), "", `expected nothing, got: ${text}`);
      }
    } finally {
      await harness.unmount();
      restoreFetch();
      clearRibbonStorage();
    }
  });
}
