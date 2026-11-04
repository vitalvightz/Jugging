import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import { AppSessionContext } from "@/components/auth-provider";
import { sparringSummary } from "@/components/session-timer/session-timer";
import { ToastProvider } from "@/components/toast-provider";
import { createTimerState } from "@/lib/session-timer/engine";
import type { TimerItem } from "@/lib/session-timer/plan";

import type { SparringLogRecord } from "@/lib/types";

import { defaultIntensity, SparringLogPrompt, type SparringDraft } from "./sparring-log-prompt";

const DRAFT: SparringDraft = {
  source: "contact",
  planId: "plan-1",
  sessionId: null,
  plannedIntensity: "hard",
  title: "Hard sparring",
  rounds: 5,
  roundSeconds: 180,
};

function render(consent: boolean): string {
  const me = { profile: { health_consent_granted: consent } };
  return renderToStaticMarkup(
    <AppSessionContext.Provider value={{ session: null, me } as never}>
      <ToastProvider>
        <SparringLogPrompt token="t" draft={DRAFT} onDismiss={() => undefined} />
      </ToastProvider>
    </AppSessionContext.Provider>,
  );
}

test("the log opens pre-filled from the plan and the timer", () => {
  const html = render(true);
  assert.match(html, /Log your sparring/);
  assert.match(html, /Hard sparring · Takes 15 seconds/);
  // Planned hard sparring starts on Hard; the athlete corrects it if needed.
  assert.match(html, /aria-pressed="true"[^>]*>Hard</);
  // Rounds come from the timer.
  assert.match(html, /aria-live="polite">5</);
  assert.match(html, /Got rocked or dropped\?/);
  // "No" is the default, so no safety message until "Yes" is picked.
  assert.doesNotMatch(html, /No more contact today/);
});

test("without health-data consent the sparring log is never shown", () => {
  const html = render(false);
  assert.doesNotMatch(html, /Log your sparring|rocked/);
  assert.doesNotMatch(html, /<section/);
});

test("planned intensity maps to the starting answer", () => {
  assert.equal(defaultIntensity("hard"), "hard");
  assert.equal(defaultIntensity("light"), "light");
  assert.equal(defaultIntensity("technical"), "light");
  assert.equal(defaultIntensity("contact"), "medium");
  assert.equal(defaultIntensity(null), undefined);
});

test("the sparring summary counts only completed sparring rounds", () => {
  const items: TimerItem[] = [
    {
      kind: "interval",
      id: "spar",
      title: "Hard sparring",
      detail: null,
      blockType: "sparring",
      rounds: 6,
      workSec: 180,
      restSec: 60,
      sparring: true,
      needsSetup: false,
    },
    {
      kind: "interval",
      id: "bag",
      title: "Bag",
      detail: null,
      blockType: "conditioning",
      rounds: 3,
      workSec: 120,
      restSec: 60,
      sparring: false,
      needsSetup: false,
    },
  ];
  const state = { ...createTimerState(items), completed: [4, 3] };
  assert.deepEqual(sparringSummary(state), { rounds: 4, roundSeconds: 180, plannedIntensity: null });
  const planned = { ...state, items: [{ ...items[0], plannedIntensity: "hard" as const }, items[1]] };
  assert.equal(sparringSummary(planned)?.plannedIntensity, "hard");
  assert.equal(sparringSummary(createTimerState([items[1]])), null);
});

const MANUAL: SparringDraft = {
  source: "free", planId: null, sessionId: null, plannedIntensity: null, title: "Sparring",
  rounds: null, roundSeconds: null, externalSessionId: "manual-1", suggestedIntensity: "hard",
};

async function mountInteractive(draft: SparringDraft, responses: Array<{ status: number; body: unknown }>) {
  await import("../test-dom");
  const { act } = await import("react");
  const { createRoot } = await import("react-dom/client");
  const bodies: Array<Record<string, unknown>> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    // Usage analytics fire beside the report; they are not report saves.
    if (String(input).includes("/api/analytics/")) return new Response(JSON.stringify({ accepted: true }), { status: 202, headers: { "content-type": "application/json" } });
    bodies.push(JSON.parse(String(init?.body ?? "{}")));
    const next = responses.shift() ?? { status: 201, body: {} };
    return new Response(JSON.stringify(next.body), { status: next.status, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  let saved = 0;
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(
    <AppSessionContext.Provider value={{ session: null, me: { profile: { health_consent_granted: true } } } as never}>
      <ToastProvider>
        <SparringLogPrompt token="t" draft={draft} onDismiss={() => undefined} onSaved={() => { saved += 1; }} />
      </ToastProvider>
    </AppSessionContext.Provider>,
  ));
  const button = (text: string, scope: ParentNode = container) => {
    const match = Array.from(scope.querySelectorAll("button")).find(b => b.textContent?.trim() === text || b.getAttribute("aria-label") === text);
    assert.ok(match, `expected "${text}"`);
    return match as HTMLButtonElement;
  };
  const group = (legend: string) => {
    const label = Array.from(container.querySelectorAll("fieldset > legend")).find(p => p.textContent === legend)!;
    return label.parentElement!;
  };
  const click = async (element: HTMLElement) => {
    await act(async () => { element.click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  };
  return {
    container, bodies, button, group, click, saved: () => saved,
    cleanup: async () => { await act(async () => root.unmount()); container.remove(); globalThis.fetch = originalFetch; },
  };
}

const LOG = { id: "log-1", athlete_id: "a", plan_id: null, session_id: null, training_day: "2026-10-19", source: "free",
  external_session_id: "manual-1", planned_intensity: null, intensity: "hard", rounds_completed: 1, round_seconds: null,
  head_contact: "light", rocked: false, notes: "", created_at: "" };

test("a manual sparring follow-up never invents rounds and starts from the logged effort", async () => {
  const ui = await mountInteractive(MANUAL, [{ status: 201, body: { log: LOG, review_created: false, safety_notice: null } }]);
  try {
    assert.match(ui.container.textContent ?? "", /Session saved. This is optional/);
    assert.equal(ui.container.querySelector(".sparring-log-rounds-value")?.textContent, "–");
    assert.equal(ui.button("Hard", ui.group("How hard was it?")).getAttribute("aria-pressed"), "true");
    await ui.click(ui.button("Light", ui.group("Head contact")));
    await ui.click(ui.button("Save"));
    assert.equal(ui.bodies.length, 0);
    assert.match(ui.container.textContent ?? "", /Add how many rounds you sparred/);
    await ui.click(ui.button("More rounds"));
    await ui.click(ui.button("Save"));
    assert.equal(ui.bodies.length, 1);
    assert.equal(ui.bodies[0].external_session_id, "manual-1");
    assert.equal(ui.bodies[0].rounds_completed, 1);
    assert.equal(ui.bodies[0].planned_intensity, null);
    assert.equal(ui.bodies[0].plan_id, null);
    assert.equal(ui.saved(), 1);
  } finally { await ui.cleanup(); }
});

test("a retry reuses its request id; a changed answer becomes a new report", async () => {
  const fail = { status: 503, body: { detail: "Your sparring log was not saved. Please try again." } };
  const ui = await mountInteractive({ ...MANUAL, rounds: null }, [fail, fail,
    { status: 201, body: { log: { ...LOG, rocked: true }, review_created: true, safety_notice: "You logged being rocked or dropped. No more contact today." } }]);
  try {
    await ui.click(ui.button("More rounds"));
    await ui.click(ui.button("Light", ui.group("Head contact")));
    await ui.click(ui.button("Save"));
    await ui.click(ui.button("Save"));
    assert.equal(ui.bodies.length, 2);
    assert.equal(ui.bodies[1].client_request_id, ui.bodies[0].client_request_id);
    await ui.click(ui.button("Yes", ui.group("Got rocked or dropped?")));
    assert.match(ui.container.textContent ?? "", /No more contact today/);
    await ui.click(ui.button("Save"));
    assert.equal(ui.bodies[2].rocked, true);
    assert.notEqual(ui.bodies[2].client_request_id, ui.bodies[0].client_request_id);
    // The same answers keep their revision; changed answers outrank them.
    assert.equal(ui.bodies[1].detail_revision, ui.bodies[0].detail_revision);
    assert.ok(Number(ui.bodies[2].detail_revision) > Number(ui.bodies[0].detail_revision));
    assert.match(ui.container.querySelector('[aria-label="Sparring logged"]')?.textContent ?? "", /No more contact today/);
    assert.equal(ui.saved(), 1);
  } finally { await ui.cleanup(); }
});

test("a reopened report starts from its saved answers and continues its revision", async () => {
  const existing: SparringLogRecord = { ...(LOG as SparringLogRecord), rounds_completed: 6, round_seconds: 180, intensity: "medium", notes: "Slipped well", detail_revision: 3 };
  const ui = await mountInteractive({ ...MANUAL, existing },
    [{ status: 201, body: { log: { ...existing, rounds_completed: 5, detail_revision: 4 }, review_created: false, safety_notice: null } }]);
  try {
    assert.match(ui.container.textContent ?? "", /Update sparring report/);
    assert.equal(ui.container.querySelector(".sparring-log-rounds-value")?.textContent, "6");
    assert.equal(ui.button("Medium", ui.group("How hard was it?")).getAttribute("aria-pressed"), "true");
    assert.equal((ui.container.querySelector("textarea") as HTMLTextAreaElement).value, "Slipped well");
    // Light head contact was recorded: "None" can no longer be chosen.
    assert.equal(ui.button("None", ui.group("Head contact")).disabled, true);
    assert.equal(ui.button("A lot", ui.group("Head contact")).disabled, false);
    await ui.click(ui.button("Fewer rounds"));
    await ui.click(ui.button("Save changes"));
    assert.equal(ui.bodies.length, 1);
    assert.equal(ui.bodies[0].rounds_completed, 5);
    assert.equal(ui.bodies[0].detail_revision, 4);
    assert.equal(ui.bodies[0].round_seconds, 180);
    assert.equal(ui.bodies[0].external_session_id, "manual-1");
    assert.equal(ui.saved(), 1);
  } finally { await ui.cleanup(); }
});

test("a reopened rocked report keeps rocked locked and does not repeat today's no-contact notice", async () => {
  const existing: SparringLogRecord = { ...(LOG as SparringLogRecord), rocked: true, head_contact: "heavy", detail_revision: 1 };
  const ui = await mountInteractive({ ...MANUAL, existing },
    [{ status: 201, body: { log: { ...existing, rounds_completed: 2, detail_revision: 2 }, review_created: true,
      safety_notice: "You logged being rocked or dropped. No more contact today." } }]);
  try {
    assert.equal(ui.button("No", ui.group("Got rocked or dropped?")).disabled, true);
    assert.equal(ui.button("Yes", ui.group("Got rocked or dropped?")).getAttribute("aria-pressed"), "true");
    assert.match(ui.container.textContent ?? "", /stay on your record for safety/);
    assert.doesNotMatch(ui.container.textContent ?? "", /No more contact today/);
    await ui.click(ui.button("More rounds"));
    await ui.click(ui.button("Save changes"));
    assert.equal(ui.bodies[0].rocked, true);
    assert.equal(ui.bodies[0].head_contact, "heavy");
    assert.equal(ui.container.querySelector('[aria-label="Sparring logged"]'), null);
    assert.equal(ui.saved(), 1);
  } finally { await ui.cleanup(); }
});
