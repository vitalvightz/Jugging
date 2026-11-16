import "./test-dom";
import assert from "node:assert/strict";
import test from "node:test";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { SharePlan } from "./coach-plans";
import type { CoachPlan } from "@/lib/api-schema.generated";
import type { Fighter } from "@/lib/coach";

test("retrying a failed assignment from the panel never creates another draft", async t => {
  const plan = { id: "source", name: "Weekly plan", schedule_mode: "weekly", sessions: [] } as unknown as CoachPlan;
  let creates = 0, assigns = 0;
  t.mock.method(globalThis, "fetch", async (input: string | URL | Request) => {
    if (String(input).endsWith("/assign")) {
      assigns++;
      assert.match(String(input), /\/saved-copy\/assign$/);
      return new Response(JSON.stringify({ ...plan, id: "saved-copy" }), { status: assigns === 1 ? 500 : 200 });
    }
    creates++;
    return new Response(JSON.stringify({ ...plan, id: "saved-copy" }));
  });
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const button = (label: string) => [...container.querySelectorAll("button")].find(row => row.textContent === label)!;
  try {
    await act(async () => root.render(<SharePlan token="token" plan={plan} others={[{ id: "alice", name: "Alice", today: "2026-11-16" } as Fighter]} />));
    await act(async () => button("Send to other fighters").click());
    await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
    await act(async () => button("Send to 1").click());
    assert.match(container.querySelector('[role="status"]')!.textContent!, /Couldn't send to Alice/);
    // Closing and reopening the panel must also retain the failed attempt.
    await act(async () => button("Cancel").click());
    await act(async () => button("Send to other fighters").click());
    await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
    await act(async () => button("Send to 1").click());
    assert.equal(creates, 1);
    assert.equal(assigns, 2);
    assert.match(container.querySelector('[role="status"]')!.textContent!, /Assigned to Alice/);
  } finally { await act(async () => root.unmount()); container.remove(); }
});

test("expired copies show original dates and require confirmation before any request", async t => {
  const plan = { id: "source", name: "Four weeks", schedule_mode: "dated", start_date: "2026-10-01", end_date: "2026-10-28", sessions: [] } as unknown as CoachPlan;
  const others = [{ id: "alice", name: "Alice", today: "2026-10-29" }] as Fighter[];
  let confirmed = false, requests = 0;
  const confirm = t.mock.method(window, "confirm", (message: string) => {
    assert.match(message, /Expired schedule/);
    assert.match(message, /Oct 2026/);
    return confirmed;
  });
  t.mock.method(globalThis, "fetch", async () => { requests++; return new Response(JSON.stringify({ ...plan, id: "copy" })); });
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const button = (label: string) => [...container.querySelectorAll("button")].find(row => row.textContent === label)!;
  try {
    await act(async () => root.render(<SharePlan token="token" plan={plan} others={others} />));
    await act(async () => button("Send to other fighters").click());
    await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
    assert.match(container.querySelector('[role="alert"]')!.textContent!, /Expired schedule:.*Oct 2026.*Alice/);
    await act(async () => button("Send to 1").click());
    assert.equal(requests, 0);
    assert.equal(confirm.mock.callCount(), 1);
    confirmed = true;
    await act(async () => button("Send to 1").click());
    assert.equal(requests, 2);
    assert.match(container.querySelector('[role="status"]')!.textContent!, /Assigned to Alice/);
  } finally { await act(async () => root.unmount()); container.remove(); }
});

test("a plan ending today is still current in the recipient's training day", async t => {
  const plan = { id: "source", name: "Four weeks", schedule_mode: "dated", start_date: "2026-10-01", end_date: "2026-10-28", sessions: [] } as unknown as CoachPlan;
  const confirm = t.mock.method(window, "confirm", () => false);
  t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({ ...plan, id: "copy" })));
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<SharePlan token="token" plan={plan} others={[{ id: "alice", name: "Alice", today: "2026-10-28" } as Fighter]} />));
    await act(async () => container.querySelector<HTMLButtonElement>("button")!.click());
    await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
    assert.equal(container.querySelector('[role="alert"]'), null);
    await act(async () => [...container.querySelectorAll("button")].find(row => row.textContent === "Send to 1")!.click());
    assert.equal(confirm.mock.callCount(), 0);
  } finally { await act(async () => root.unmount()); container.remove(); }
});
