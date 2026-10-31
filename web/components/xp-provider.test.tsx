import "./test-dom";

import test from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { AppSessionContext } from "./auth-provider";
import { XpProvider, useXp } from "./xp-provider";
import { requestXpRefresh } from "../lib/xp-events";

type Identity = {
  athleteId: string;
  accessToken: string;
} | null;

type ApiAward = {
  id: string;
  action: string;
  amount: number;
  awarded_at: string;
};

type FetchStub = {
  calls: Array<{ url: string; method: string; authorization: string | null }>;
  activityCalls: Array<{ url: string; method: string; authorization: string | null }>;
  restore: () => void;
};

type Harness = {
  read: () => ReturnType<typeof useXp>;
  refresh: () => Promise<void>;
  renderIdentity: (identity: Identity) => Promise<void>;
  unmount: () => Promise<void>;
};

function progressResponse(totalXp: number, awards: ApiAward[] = []): Response {
  return new Response(
    JSON.stringify({
      state: {
        total_xp: totalXp,
        last_daily_login_date: null,
        recent_awards: awards,
      },
      streaks: {
        login: { current: 1, best: 1, last_active_date: "2026-08-18" },
        adherence: { current: 0, best: 0, last_qualifying_day: null },
      },
      opportunities: [],
      current_week: null,
      major_milestones: [],
    }),
    {
      status: 200,
      headers: { "content-type": "application/json" },
    },
  );
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function installFetchStub(
  handler: (index: number) => Promise<Response> | Response,
): FetchStub {
  const originalFetch = globalThis.fetch;
  const calls: FetchStub["calls"] = [];
  const activityCalls: FetchStub["activityCalls"] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(typeof input === "string" || input instanceof URL ? input : input.url);
    const call = {
      url,
      method: init?.method ?? "GET",
      authorization: new Headers(init?.headers).get("authorization"),
    };
    if (url === "/api/xp/activity") {
      activityCalls.push(call);
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }
    calls.push(call);
    return handler(calls.length - 1);
  }) as typeof fetch;
  return {
    calls,
    activityCalls,
    restore: () => {
      globalThis.fetch = originalFetch;
    },
  };
}

function sessionValue(identity: Identity) {
  if (!identity) {
    return { session: null, me: null } as never;
  }
  return {
    session: { access_token: identity.accessToken },
    me: {
      profile: {
        role: "athlete",
        athlete_id: identity.athleteId,
      },
    },
  } as never;
}

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function waitFor(predicate: () => boolean, message: string): Promise<void> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if (predicate()) return;
    await settle();
  }
  assert.fail(message);
}

async function mountProvider(identity: Identity): Promise<Harness> {
  let latestValue: ReturnType<typeof useXp> | null = null;
  const container = document.createElement("div");
  document.body.append(container);
  const root: Root = createRoot(container);

  function Probe() {
    latestValue = useXp();
    return null;
  }

  const renderIdentity = async (nextIdentity: Identity) => {
    await act(async () => {
      root.render(
        <AppSessionContext.Provider value={sessionValue(nextIdentity)}>
          <XpProvider>
            <Probe />
          </XpProvider>
        </AppSessionContext.Provider>,
      );
    });
    await settle();
  };

  await renderIdentity(identity);

  const read = () => {
    if (!latestValue) throw new Error("XP provider never rendered.");
    return latestValue;
  };

  return {
    read,
    refresh: async () => {
      await act(async () => {
        await read().refresh();
      });
      await settle();
    },
    renderIdentity,
    unmount: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

const sessionAwards: ApiAward[] = [
  {
    id: "planned-1",
    action: "planned_session_completed",
    amount: 50,
    awarded_at: "2026-08-03T15:00:01Z",
  },
  {
    id: "logged-1",
    action: "training_logged",
    amount: 25,
    awarded_at: "2026-08-03T15:00:00Z",
  },
];

test("first athlete load records activity explicitly before reading progress", async () => {
  const fetchStub = installFetchStub(() => progressResponse(620, sessionAwards));
  const harness = await mountProvider({ athleteId: "athlete-1", accessToken: "token-1" });

  try {
    await waitFor(() => harness.read().isHydrated, "initial progress read did not finish");
    assert.equal(fetchStub.calls.length, 1);
    assert.deepEqual(fetchStub.calls[0], {
      url: "/api/xp/progress",
      method: "GET",
      authorization: "Bearer token-1",
    });
    assert.equal(fetchStub.activityCalls.length, 1);
    assert.equal(fetchStub.activityCalls[0]?.method, "POST");
    assert.equal(harness.read().progress.state.totalXp, 620);
    assert.equal(harness.read().feedback, null);
  } finally {
    await harness.unmount();
    fetchStub.restore();
  }
});

test("two session awards are aggregated into one +75 routine event", async () => {
  const responses = [progressResponse(620), progressResponse(695, sessionAwards)];
  const fetchStub = installFetchStub((index) => responses[index] ?? responses.at(-1)!);
  const harness = await mountProvider({ athleteId: "athlete-1", accessToken: "token-1" });

  try {
    await waitFor(() => harness.read().isHydrated, "baseline progress read did not finish");
    await harness.refresh();
    assert.deepEqual(harness.read().feedback, {
      kind: "routine",
      amount: 75,
      label: "Session logged. Work done.",
      awardIds: ["planned-1", "logged-1"],
      actions: ["planned_session_completed", "training_logged"],
    });
  } finally {
    await harness.unmount();
    fetchStub.restore();
  }
});

test("milestone feedback outranks the session label in a reconciled batch", async () => {
  const milestoneBatch: ApiAward[] = [
    {
      id: "phase-1",
      action: "phase_completed",
      amount: 200,
      awarded_at: "2026-08-03T15:00:04Z",
    },
    {
      id: "week-1",
      action: "full_training_week_completed",
      amount: 100,
      awarded_at: "2026-08-03T15:00:03Z",
    },
    ...sessionAwards,
  ];
  const responses = [
    progressResponse(1_000),
    progressResponse(1_375, milestoneBatch),
  ];
  const fetchStub = installFetchStub((index) => responses[index] ?? responses.at(-1)!);
  const harness = await mountProvider({ athleteId: "athlete-1", accessToken: "token-1" });

  try {
    await waitFor(() => harness.read().isHydrated, "baseline progress read did not finish");
    await harness.refresh();
    assert.deepEqual(harness.read().feedback, {
      kind: "routine",
      amount: 375,
      label: "Training phase complete",
      awardIds: ["phase-1", "week-1", "planned-1", "logged-1"],
      actions: ["phase_completed", "full_training_week_completed", "planned_session_completed", "training_logged"],
    });
  } finally {
    await harness.unmount();
    fetchStub.restore();
  }
});

test("level-up feedback wins over the routine award message", async () => {
  const levelAward: ApiAward = {
    id: "week-1",
    action: "full_training_week_completed",
    amount: 100,
    awarded_at: "2026-08-03T15:10:00Z",
  };
  const responses = [progressResponse(700), progressResponse(800, [levelAward])];
  const fetchStub = installFetchStub((index) => responses[index] ?? responses.at(-1)!);
  const harness = await mountProvider({ athleteId: "athlete-1", accessToken: "token-1" });

  try {
    await waitFor(() => harness.read().isHydrated, "baseline progress read did not finish");
    await harness.refresh();
    assert.deepEqual(harness.read().feedback, {
      kind: "level_up",
      level: 3,
      title: "Amateur",
      message: "Built through consistent work.",
      awardIds: ["week-1"],
    });
  } finally {
    await harness.unmount();
    fetchStub.restore();
  }
});

test("the global refresh event rereads progress immediately", async () => {
  const responses = [
    progressResponse(100),
    progressResponse(110, [
      {
        id: "checkin-1",
        action: "readiness_checkin_completed",
        amount: 10,
        awarded_at: "2026-08-03T15:20:00Z",
      },
    ]),
  ];
  const fetchStub = installFetchStub((index) => responses[index] ?? responses.at(-1)!);
  const harness = await mountProvider({ athleteId: "athlete-1", accessToken: "token-1" });

  try {
    await waitFor(() => harness.read().isHydrated, "baseline progress read did not finish");
    await act(async () => requestXpRefresh());
    await waitFor(() => fetchStub.calls.length === 2, "XP refresh event did not read progress");
    assert.equal(harness.read().progress.state.totalXp, 110);
  } finally {
    await harness.unmount();
    fetchStub.restore();
  }
});

test("a refresh event during an active read queues a trailing GET", async () => {
  const initialRead = deferred<Response>();
  const checkinAward: ApiAward = {
    id: "checkin-queued",
    action: "readiness_checkin_completed",
    amount: 10,
    awarded_at: "2026-08-03T15:30:00Z",
  };
  const fetchStub = installFetchStub((index) =>
    index === 0
      ? initialRead.promise
      : progressResponse(110, [checkinAward]),
  );
  const harness = await mountProvider({ athleteId: "athlete-1", accessToken: "token-1" });

  try {
    await waitFor(() => fetchStub.calls.length === 1, "initial XP read did not start");
    await act(async () => requestXpRefresh());
    assert.equal(fetchStub.calls.length, 1, "trailing read started before the active read settled");

    await act(async () => {
      initialRead.resolve(progressResponse(100));
      await Promise.resolve();
    });

    await waitFor(() => fetchStub.calls.length === 2, "queued XP refresh did not issue a second GET");
    await waitFor(
      () => harness.read().progress.state.totalXp === 110,
      "queued XP refresh did not load the updated total",
    );
    assert.deepEqual(harness.read().feedback, {
      kind: "routine",
      amount: 10,
      label: "Check-in complete",
      awardIds: ["checkin-queued"],
      actions: ["readiness_checkin_completed"],
    });
  } finally {
    await harness.unmount();
    fetchStub.restore();
  }
});

test("temporary failure preserves the last valid athlete state", async () => {
  const fetchStub = installFetchStub((index) => {
    if (index === 0) return progressResponse(620);
    return Promise.reject(new Error("offline"));
  });
  const harness = await mountProvider({ athleteId: "athlete-1", accessToken: "token-1" });

  try {
    await waitFor(() => harness.read().isHydrated, "baseline progress read did not finish");
    await harness.refresh();
    assert.equal(harness.read().progress.state.totalXp, 620);
    assert.equal(harness.read().error, "XP progress is temporarily unavailable.");
  } finally {
    await harness.unmount();
    fetchStub.restore();
  }
});

test("switching athletes clears progress and feedback before loading the new account", async () => {
  const fetchStub = installFetchStub((index) =>
    index === 0 ? progressResponse(620) : progressResponse(25),
  );
  const harness = await mountProvider({ athleteId: "athlete-1", accessToken: "token-1" });

  try {
    await waitFor(() => harness.read().progress.state.totalXp === 620, "first athlete did not load");
    await harness.renderIdentity({ athleteId: "athlete-2", accessToken: "token-2" });
    await waitFor(() => harness.read().progress.state.totalXp === 25, "second athlete did not load");
    assert.equal(harness.read().feedback, null);
    assert.deepEqual(
      fetchStub.calls.map((call) => call.authorization),
      ["Bearer token-1", "Bearer token-2"],
    );
  } finally {
    await harness.unmount();
    fetchStub.restore();
  }
});
