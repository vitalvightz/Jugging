import test from "node:test";
import assert from "node:assert/strict";
import "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { AppSessionContext } from "./auth-provider";
import { GenerationStatusProvider } from "./generation-status-provider";
import { ProgrammeCompletionReceipt } from "./generation-status-shell";
import { publishGenerationTerminalJob } from "../lib/generation-terminal-event";
import { hasProgrammeArrival, consumeProgrammeArrival } from "../lib/programme-arrival";

for (const scenario of ["background", "recovered", "inactive", "held"]) test(`completion receipt: ${scenario}`, async () => {
  const original = globalThis.fetch;
  window.localStorage.clear(); window.sessionStorage.clear();
  globalThis.fetch = (async () => new Response(JSON.stringify({plan_id: scenario === "inactive" ? "old" : "new"}), {status: 200})) as typeof fetch;
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<AppSessionContext.Provider value={{me: {profile: {role: "athlete"}}, session: {access_token: "fixture"}} as never}>
      <GenerationStatusProvider token="fixture"><ProgrammeCompletionReceipt /></GenerationStatusProvider>
    </AppSessionContext.Provider>));
    await act(async () => {
      publishGenerationTerminalJob({job_id: "job", athlete_id: "athlete", client_request_id: "request", created_at: new Date().toISOString(), updated_at: new Date().toISOString(), status: scenario === "recovered" ? "failed" : "completed", plan_id: "new",
        requires_admin_resume: scenario === "held"});
    });
    await act(async () => { await Promise.resolve(); });
    const eligible = scenario === "background" || scenario === "recovered";
    assert.equal(hasProgrammeArrival("new"), eligible);
    if (eligible) {
      assert.equal(consumeProgrammeArrival("new"), true);
      await act(async () => publishGenerationTerminalJob({job_id: "job", athlete_id: "athlete", client_request_id: "request", created_at: new Date().toISOString(), updated_at: new Date().toISOString(), status: "completed", plan_id: "new"}));
      assert.equal(hasProgrammeArrival("new"), false);
    }
  } finally {
    await act(async () => root.unmount());
    globalThis.fetch = original;
    window.localStorage.clear(); window.sessionStorage.clear();
  }
});

for (const old of [false, true]) test(`reload recovers latest completion only when recent (old: ${old})`, async () => {
  const original = globalThis.fetch;
  window.localStorage.clear(); window.sessionStorage.clear();
  const now = new Date(Date.now() - (old ? 86400000 : 1000)).toISOString();
  globalThis.fetch = (async (input: string) => new Response(JSON.stringify(
    String(input).endsWith("/generation-jobs/active") ? null :
    String(input).endsWith("/generation-jobs/latest") ? {job_id: "reload", status: "completed", latest_plan_id: "new", created_at: now, completed_at: now} : {plan_id: "new"}
  ), {status: 200})) as typeof fetch;
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<GenerationStatusProvider token="reload"><span /></GenerationStatusProvider>));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 1200)); });
    assert.equal(hasProgrammeArrival("new"), !old);
  } finally {
    await act(async () => root.unmount()); globalThis.fetch = original;
    window.localStorage.clear(); window.sessionStorage.clear();
  }
});
