import test from "node:test";
import assert from "node:assert/strict";
import {
  trackProductEvent,
  productEventIdentity,
} from "./api";
import { __resetConnectivityForTests, getConnectivitySnapshot } from "./connectivity";
import { percent } from "./product-analytics";

test("beta events send without a preference read and retry with the same identity", async () => {
  const original = globalThis.fetch;
  const bodies: string[] = [];
  globalThis.fetch = async (input, init) => {
    assert.ok(String(input).endsWith("/events"));
    assert.equal(init?.keepalive, true);
    bodies.push(String(init?.body));
    return new Response(JSON.stringify(bodies.length === 1 ? { detail: "temporary" } : { accepted: true }), { status: bodies.length === 1 ? 503 : 202 });
  };
  try {
    assert.equal(await trackProductEvent("no-choice", "timer_started", "timer"), true);
    assert.equal(bodies.length, 2);
    assert.equal(bodies[0], bodies[1]);
    assert.deepEqual(Object.keys(JSON.parse(bodies[0])).sort(), ["event_name", "feature", "request_id"]);
  } finally { globalThis.fetch = original; }
});
test("retry identities are stable, valid UUIDs and do not contain their input", async () => {
  const id = await productEventIdentity("flow:123:start");
  assert.equal(id, await productEventIdentity("flow:123:start"));
  assert.notEqual(id, await productEventIdentity("flow:123:finish"));
  assert.match(
    id,
    /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/,
  );
});
test("empty denominators remain distinct from measured zero", () => {
  assert.equal(percent(null), "Insufficient data");
  assert.equal(percent(0), "0.0%");
});

test("admin athlete identity uses trimmed name, email, then a UUID fallback", async () => {
  const { athleteIdentity } = await import("./product-analytics");
  const id = "00000000-0000-4000-8000-000000000001";
  assert.equal(
    athleteIdentity({
      id,
      full_name: " Example Athlete ",
      email: "name@example.test",
    }),
    "Example Athlete",
  );
  assert.equal(
    athleteIdentity({ id, full_name: "  ", email: " fallback@example.test " }),
    "fallback@example.test",
  );
  assert.equal(
    athleteIdentity({ id, full_name: null, email: " " }),
    "00000000",
  );
});

test("background plan, history and bodyweight reads do not manufacture feature opens", async () => {
  const {
    getPlan,
    getTrainingHistory,
    getNutritionCurrent,
    updateNutritionCurrent,
  } = await import("./api");
  const original = globalThis.fetch;
  const events: unknown[] = [];
  let fail = false;
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    if (path.endsWith("/events")) events.push(JSON.parse(String(init?.body)));
    return new Response(
      JSON.stringify({}),
      {
        status: fail && path.endsWith("/nutrition/current") ? 422 : 200,
        headers: { "Content-Type": "application/json" },
      },
    );
  };
  try {
    await getPlan("read-background", "00000000-0000-4000-8000-000000000001");
    await getTrainingHistory("read-background");
    await getNutritionCurrent("read-background");
    assert.equal(events.length, 0);
    fail = true;
    await assert.rejects(
      updateNutritionCurrent(
        "read-background",
        {} as Parameters<typeof updateNutritionCurrent>[1],
      ),
    );
    assert.equal(
      events.length,
      0,
      "a failed save is not a successful nutrition action",
    );
  } finally {
    globalThis.fetch = original;
  }
});


test("analytics network and gateway failures never degrade app connectivity", async () => {
  const original = globalThis.fetch;
  try {
    for (const status of [null, 502, 503, 504]) {
      __resetConnectivityForTests();
      let attempts = 0;
      globalThis.fetch = async () => {
        attempts++;
        if (status === null) throw new TypeError("Failed to fetch");
        return new Response("unavailable", { status });
      };
      assert.equal(await trackProductEvent("beta", "app_opened", "onboarding"), false);
      assert.equal(attempts, 3);
      assert.deepEqual(getConnectivitySnapshot(), { offline: false, degraded: false });
    }
    // Isolation is limited to background event delivery. Real app failures
    // must still activate the shared connection warning and recovery flow.
    const { getNutritionCurrent } = await import("./api");
    for (const status of [null, 503]) {
      __resetConnectivityForTests();
      globalThis.fetch = async () => {
        if (status === null) throw new TypeError("Failed to fetch");
        return new Response("unavailable", { status });
      };
      await assert.rejects(getNutritionCurrent("foreground"));
      assert.equal(getConnectivitySnapshot().degraded, true);
    }
  } finally {
    globalThis.fetch = original;
    __resetConnectivityForTests();
  }
});
