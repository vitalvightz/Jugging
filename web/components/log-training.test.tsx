import assert from "node:assert/strict";
import test from "node:test";
import "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { AppSessionContext } from "./auth-provider";
import { LogTraining } from "./log-training";
import type { TrainingHistorySession } from "@/lib/training-history";
import type { MeResponse } from "@/lib/types";

// jsdom has no modal dialogs; the sheet only needs `open` to render its form.
const dialogProto = window.HTMLDialogElement.prototype as unknown as { showModal: () => void };
dialogProto.showModal ??= function (this: HTMLDialogElement) { this.setAttribute("open", ""); };

const me = (technical_style: string[]) => ({ profile: { role: "athlete", health_consent_granted: true, athlete_timezone: "UTC", technical_style } }) as unknown as MeResponse;

type Call = { url: string; method?: string; body: Record<string, unknown> };

async function mount(sports: string[], props: Partial<Parameters<typeof LogTraining>[0]> = {}, respond?: (call: Call, index: number) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const call = { url: String(input), method: init?.method, body: JSON.parse(String(init?.body ?? "{}")) };
    calls.push(call);
    if (respond) return respond(call, calls.length - 1);
    const body = call.body as { activity: string; sport?: string; session_type?: string; duration_minutes: number; effort: string; training_day?: string };
    return new Response(JSON.stringify({
      id: `manual-${calls.length}`, source: "external", status: "done", plan_id: null, training_day: body.training_day ?? "2026-10-19",
      title: body.sport ?? body.activity, activity: body.activity, sport: body.sport ?? null, session_type: body.session_type ?? null,
      duration_minutes: body.duration_minutes, effort: body.effort, takeaway: "",
    }), { status: 201, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  const saved: TrainingHistorySession[] = [];
  await act(async () => root.render(<AppSessionContext.Provider value={{
    session: { access_token: "token" } as never, me: me(sports), isReady: true, isMeHydrated: true,
    hasTransientMeError: false, refreshMe: async () => {}, signOut: async () => {}, replaceMe: () => {}, previewAppearanceMode: () => {},
  }}><LogTraining accessToken="token" onSaved={row => saved.push(row)} {...props} /></AppSessionContext.Provider>));
  const group = (name: string) => container.querySelector<HTMLElement>(`fieldset[aria-label="${name}"], [role="group"][aria-label="${name}"]`);
  const buttons = (name: string) => [...(group(name)?.querySelectorAll("button") ?? [])];
  const pick = async (name: string, text: string) => {
    const button = buttons(name).find(item => item.textContent === text);
    assert.ok(button, `Missing ${text} in ${name}`);
    await act(async () => button.click());
  };
  const click = async (text: string) => {
    const button = [...container.querySelectorAll("button")].find(item => item.textContent?.trim() === text || item.getAttribute("aria-label") === text);
    assert.ok(button, `Missing ${text}`);
    await act(async () => button.click());
  };
  const pressed = (name: string) => buttons(name).filter(item => item.getAttribute("aria-pressed") === "true").map(item => item.textContent);
  const save = async () => {
    await act(async () => container.querySelector("form")!.requestSubmit());
    await act(async () => { await Promise.resolve(); });
  };
  return {
    container, calls, saved, group, buttons, pick, click, pressed, save,
    cleanup: () => { act(() => root.unmount()); container.remove(); globalThis.fetch = originalFetch; },
  };
}

const labels = (ui: Awaited<ReturnType<typeof mount>>) => ui.buttons("Activity").filter(item => item.hasAttribute("data-activity")).map(item => item.textContent);
const sent = (call: Call) => ({ ...call.body, client_request_id: undefined });

test("without a known sport the original six activities and payload are unchanged", async () => {
  const ui = await mount([]);
  try {
    await ui.click("Log session");
    assert.deepEqual(labels(ui), ["Boxing", "Sparring", "Strength", "Conditioning", "Running", "Other"]);
    assert.deepEqual(ui.pressed("Activity"), []);
    await ui.pick("Activity", "Running");
    await ui.pick("Duration", "45");
    await ui.pick("Effort", "Easy");
    await ui.save();
    assert.deepEqual(Object.keys(ui.calls[0].body).sort(), ["activity", "client_request_id", "duration_minutes", "effort"]);
  } finally { ui.cleanup(); }
});

test("a boxer sees boxing session types in the same single grid, with nothing preselected", async () => {
  const ui = await mount(["boxing"]);
  try {
    await ui.click("Log session");
    assert.deepEqual(labels(ui), ["Pads", "Bag work", "Shadowboxing", "Technical drills", "Sparring", "Strength", "Conditioning", "Running", "Other"]);
    assert.deepEqual(ui.pressed("Activity"), []);
    assert.equal(ui.group("Sport"), null);
    assert.equal(ui.group("Session type"), null);
    await ui.pick("Activity", "Pads");
    await ui.pick("Duration", "60");
    await ui.pick("Effort", "Moderate");
    await ui.save();
    assert.deepEqual(sent(ui.calls[0]), { activity: "boxing", sport: "boxing", session_type: "pads", duration_minutes: 60, effort: "moderate", client_request_id: undefined });
  } finally { ui.cleanup(); }
});

for (const [sport, pick, minutes, expected] of [
  ["bjj", "Rolling", "90", { activity: "other", sport: "bjj", session_type: "rolling", duration_minutes: 90 }],
  ["muay_thai", "Clinch", "45", { activity: "other", sport: "muay_thai", session_type: "clinch", duration_minutes: 45 }],
  ["mma", "Striking", "60", { activity: "other", sport: "mma", session_type: "striking", duration_minutes: 60 }],
  ["mma", "Wrestling", "30", { activity: "other", sport: "mma", session_type: "wrestling", duration_minutes: 30 }],
  ["kickboxing", "Bag work", "30", { activity: "other", sport: "kickboxing", session_type: "bag", duration_minutes: 30 }],
  ["wrestling", "Live wrestling", "60", { activity: "other", sport: "wrestling", session_type: "live", duration_minutes: 60 }],
] as const) {
  test(`${sport}: ${pick} saves as logged, never as Boxing, with no sparring follow-up`, async () => {
    const ui = await mount([sport]);
    try {
      await ui.click("Log session");
      assert.ok(!labels(ui).includes("Boxing"));
      await ui.pick("Activity", pick);
      await ui.pick("Duration", minutes);
      await ui.pick("Effort", "Hard");
      await ui.save();
      assert.deepEqual(sent(ui.calls[0]), { ...expected, effort: "hard", client_request_id: undefined });
      assert.doesNotMatch(ui.container.textContent ?? "", /Head contact/);
    } finally { ui.cleanup(); }
  });
}

test("strength from a sport grid is stored exactly as before, without a sport", async () => {
  const ui = await mount(["bjj"]);
  try {
    await ui.click("Log session");
    await ui.pick("Activity", "Rolling");
    await ui.pick("Activity", "Strength");
    assert.deepEqual(ui.pressed("Activity"), ["Strength"]);
    await ui.pick("Duration", "45");
    await ui.pick("Effort", "Moderate");
    await ui.save();
    assert.deepEqual(Object.keys(ui.calls[0].body).sort(), ["activity", "client_request_id", "duration_minutes", "effort"]);
    assert.equal(ui.calls[0].body.activity, "strength");
  } finally { ui.cleanup(); }
});

test("striking sparring keeps the sparring follow-up; it asks about head contact rather than assuming it", async () => {
  const ui = await mount(["kickboxing"]);
  try {
    await ui.click("Log session");
    await ui.pick("Activity", "Sparring");
    await ui.pick("Duration", "30");
    await ui.pick("Effort", "Hard");
    await ui.save();
    assert.deepEqual([ui.calls[0].body.activity, ui.calls[0].body.sport, ui.calls[0].body.session_type], ["sparring", "kickboxing", "sparring"]);
    assert.match(ui.container.textContent ?? "", /Head contact/);
  } finally { ui.cleanup(); }
});

const ROLLING: TrainingHistorySession = {
  id: "rolling", source: "external", training_day: "2026-10-12", status: "done", title: "BJJ · Rolling",
  activity: "other", sport: "bjj", session_type: "rolling", duration_minutes: 90, effort: "hard", plan_id: null,
};

test("repeat keeps sport, session type and duration, and asks only for today's effort", async () => {
  const ui = await mount(["bjj"], { repeatEntry: ROLLING });
  try {
    await ui.click("Repeat BJJ · Rolling");
    assert.match(ui.container.querySelector("dialog h2")!.textContent!, /Log BJJ · Rolling/);
    assert.deepEqual(ui.pressed("Effort"), []);
    await ui.save();
    assert.equal(ui.calls.length, 0);
    assert.match(ui.container.querySelector('[role="alert"]')!.textContent!, /Select an activity and effort/);
    await ui.pick("Effort", "Easy");
    await ui.save();
    assert.deepEqual(sent(ui.calls[0]), { activity: "other", sport: "bjj", session_type: "rolling", duration_minutes: 90, effort: "easy", client_request_id: undefined });
  } finally { ui.cleanup(); }
});

test("a session from another sport edits in its own sport's grid", async () => {
  const ui = await mount(["boxing"], { entry: ROLLING }, call => new Response(JSON.stringify({ ...ROLLING, ...call.body }), { status: 200 }));
  try {
    await ui.click("Edit BJJ · Rolling");
    assert.deepEqual(ui.pressed("Activity"), ["Rolling"]);
    await ui.pick("Activity", "Positional");
    await ui.save();
    assert.deepEqual(ui.calls[0].body, { activity: "other", sport: "bjj", session_type: "positional", duration_minutes: 90, effort: "hard" });
  } finally { ui.cleanup(); }
});

test("an older Sparring record edits in the original grid and is saved unchanged", async () => {
  const legacy: TrainingHistorySession = { ...ROLLING, id: "legacy", title: "Sparring", activity: "sparring", sport: undefined, session_type: undefined };
  const ui = await mount(["boxing"], { entry: legacy }, call => new Response(JSON.stringify({ ...legacy, ...call.body }), { status: 200 }));
  try {
    await ui.click("Edit Sparring");
    assert.deepEqual(labels(ui), ["Boxing", "Sparring", "Strength", "Conditioning", "Running", "Other"]);
    assert.deepEqual(ui.pressed("Activity"), ["Sparring"]);
    await ui.pick("Duration", "60");
    await ui.save();
    assert.equal(ui.calls[0].method, "PATCH");
    assert.deepEqual(ui.calls[0].body, { activity: "sparring", duration_minutes: 60, effort: "hard" });
  } finally { ui.cleanup(); }
});

test("a lost response retries the same request ID; double submits send one request", async () => {
  const ui = await mount(["bjj"], {}, (call, index) => index === 0
    ? new Response(JSON.stringify({ detail: "Saved, but response lost. Retry." }), { status: 503 })
    : new Response(JSON.stringify({ id: "m", source: "external", status: "done", plan_id: null, training_day: "2026-10-19",
      title: "BJJ · Drilling", activity: "other", sport: "bjj", session_type: "drilling", duration_minutes: 60, effort: "hard" }), { status: 201 }));
  try {
    await ui.click("Log session");
    await ui.pick("Activity", "Drilling");
    await ui.pick("Duration", "60");
    await ui.pick("Effort", "Hard");
    await ui.save();
    assert.equal(ui.calls.length, 1);
    await act(async () => { const form = ui.container.querySelector("form")!; form.requestSubmit(); form.requestSubmit(); });
    await act(async () => { await Promise.resolve(); });
    assert.equal(ui.calls.length, 2);
    assert.equal(ui.calls[1].body.client_request_id, ui.calls[0].body.client_request_id);
    assert.equal(ui.saved.length, 1);
  } finally { ui.cleanup(); }
});

test("Change sport switches the choices for this session only, then defaults back to the saved sport", async () => {
  const ui = await mount(["boxing"]);
  try {
    await ui.click("Log session");
    const toggle = ui.container.querySelector<HTMLButtonElement>("dialog legend button")!;
    assert.equal(toggle.textContent, "Boxing · Change");
    assert.equal(toggle.getAttribute("aria-expanded"), "false");
    assert.equal(ui.group("Sport for this session"), null);
    await act(async () => toggle.click());
    assert.deepEqual(ui.buttons("Sport for this session").map(item => item.textContent),
      ["Boxing", "Muay Thai", "Kickboxing", "MMA", "BJJ", "Wrestling", "No sport"]);
    assert.deepEqual(ui.pressed("Sport for this session"), ["Boxing"]);
    await ui.pick("Sport for this session", "BJJ");
    // The menu closes; the grid is BJJ's and the toggle says so.
    assert.equal(ui.group("Sport for this session"), null);
    assert.equal(toggle.textContent, "BJJ · Change");
    assert.deepEqual(labels(ui), ["Drilling", "Positional", "Rolling", "Strength", "Conditioning", "Running", "Other"]);
    await ui.pick("Activity", "Rolling");
    await ui.pick("Duration", "90");
    await ui.pick("Effort", "Hard");
    await ui.save();
    assert.deepEqual(sent(ui.calls[0]), { activity: "other", sport: "bjj", session_type: "rolling", duration_minutes: 90, effort: "hard", client_request_id: undefined });
    // Nothing is written to the profile.
    assert.ok(ui.calls.every(call => !call.url.includes("/api/me")));
    await ui.click("Log session");
    assert.equal(ui.container.querySelector("dialog legend button")!.textContent, "Boxing · Change");
    assert.equal(labels(ui)[0], "Pads");
  } finally { ui.cleanup(); }
});

test("switching sport clears a session type that no longer fits, but keeps Strength; No sport shows the original six", async () => {
  const ui = await mount(["muay_thai"]);
  try {
    await ui.click("Log session");
    const toggle = () => ui.container.querySelector<HTMLButtonElement>("dialog legend button")!;
    await ui.pick("Activity", "Clinch");
    await act(async () => toggle().click());
    await ui.pick("Sport for this session", "Kickboxing");
    assert.deepEqual(ui.pressed("Activity"), []);
    await ui.pick("Activity", "Strength");
    await act(async () => toggle().click());
    await ui.pick("Sport for this session", "No sport");
    assert.equal(toggle().textContent, "No sport · Change");
    assert.deepEqual(labels(ui), ["Boxing", "Sparring", "Strength", "Conditioning", "Running", "Other"]);
    assert.deepEqual(ui.pressed("Activity"), ["Strength"]);
  } finally { ui.cleanup(); }
});

/** Text a person can still read or reach: a closing section fades out inert. */
function liveText(container: HTMLElement): string {
  const copy = container.cloneNode(true) as HTMLElement;
  copy.querySelectorAll("[inert]").forEach((node) => node.remove());
  return copy.textContent ?? "";
}

test("saving needs only activity, duration and effort; exercises are a dismissible offer afterwards", async () => {
  const ui = await mount([]);
  try {
    await ui.click("Log session");
    await ui.pick("Activity", "Strength");
    await ui.pick("Duration", "60");
    await ui.pick("Effort", "Hard");
    await ui.save();
    assert.equal(ui.calls.length, 1, "one save request, no exercise step");
    assert.match(ui.container.textContent!, /Session saved/);
    const offer = [...ui.container.querySelectorAll("button")].find(item => item.textContent?.startsWith("Add exercises"));
    assert.ok(offer, "Add exercises is offered beside the takeaway");
    // The log sheet itself may still be sliding away.
    assert.equal(ui.container.querySelectorAll("dialog[open]:not([data-closing])").length, 0, "nothing opens until the athlete asks");
    await ui.click("Optional — skip anytime");
    assert.doesNotMatch(liveText(ui.container), /Add exercises/);
    assert.equal(ui.calls.length, 1);
  } finally { ui.cleanup(); }
});

test("sparring keeps its own follow-up without an exercise offer", async () => {
  const ui = await mount([]);
  try {
    await ui.click("Log session");
    await ui.pick("Activity", "Sparring");
    await ui.pick("Duration", "30");
    await ui.pick("Effort", "Hard");
    await ui.save();
    assert.doesNotMatch(ui.container.textContent!, /Add exercises/);
  } finally { ui.cleanup(); }
});
