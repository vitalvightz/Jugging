import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { window as domWindow } from "../test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { InjuryCareStatus, InjuryClearance, DelayedRehabResponse, EffectiveClinicianClearanceStatus } from "./injury-care-status";
import type { InjuryFlagRecord } from "@/lib/types";

const injury: InjuryFlagRecord = {
  id: "injury-1", episode_id: "episode-1", athlete_id: "athlete-1", source: "checkin",
  body_area: "Chest", description: "Chest strain", severity: "mild", status: "open",
  surface_class: "non_surface", created_at: "", updated_at: "",
  rehab_decision: { outcome: "prescribed_rehab", summary: "Comfortable movement is available.", reason_codes: [],
    prescription: { sources: ["https://www.nhs.uk/conditions/sprains-and-strains/"] },
    schedule: { state: "recovery_day", reason: "Allow the recovery gap.", next_due_day: "2026-10-02" } },
};

test("a readiness hold pauses today's rehab without changing LOAD eligibility", () => {
  const html = renderToStaticMarkup(<InjuryCareStatus injury={{ ...injury, rehab_decision: {
    ...injury.rehab_decision!, stage: "load",
    progression: { entry_basis: "reported_clinician_permission" },
    schedule: { state: "held", reason: "Complete today's check-in and follow its training guidance." },
  } }} token="token" onRefresh={async () => {}} />);
  const body = new domWindow.DOMParser().parseFromString(html, "text/html").body;
  assert.match(body.textContent ?? "", /Today’s rehab on hold/);
  assert.match(body.textContent ?? "", /Complete today's check-in/);
  assert.match(body.textContent ?? "", /LOAD follows your reported clinician advice/);
  assert.doesNotMatch(body.textContent ?? "", /Progression is on hold|Progression · On hold/);
});

async function click(container: HTMLElement, label: string) {
  const surface = document.querySelector<HTMLElement>('[role="dialog"]') ?? container;
  const training = Array.from(surface.querySelectorAll<HTMLInputElement>('input[type="radio"]')).find(input => input.closest("label")?.textContent === label);
  if (training) {
    await act(async () => {
      training.click();
    });
    label = "Save changes";
  }
  const button = Array.from(surface.querySelectorAll("button")).find(b => b.textContent === (label === "Save clearance" ? "Save changes" : label) || b.getAttribute("aria-label") === label);
  assert.ok(button, label);
  await act(async () => { button.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true })); });
}

for (const policyId of ["achilles_tendonitis", "elbow_tendonitis"]) {
  test(`${policyId} clearance has only the existing two permissions and no assessment worksheet`, async () => {
    const container = document.createElement("div"); document.body.appendChild(container);
    const root = createRoot(container);
    const original = globalThis.fetch;
    const calls: Array<Record<string, unknown>> = [];
    globalThis.fetch = (async (_input, init) => {
      calls.push(JSON.parse(String(init?.body)));
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }) as typeof fetch;
    try {
      await act(async () => root.render(<InjuryClearance injury={{ ...injury,
        rehab_decision: { ...injury.rehab_decision!, policy_id: policyId } }} token="token" onRefresh={async () => {}} />));
      await click(container, "Report clearance");
      const dialog = document.querySelector('[role="dialog"]')!;
      assert.equal(dialog.querySelectorAll("fieldset").length, 2);
      assert.equal(dialog.querySelectorAll("form").length, 1);
      assert.equal(dialog.querySelectorAll('select, textarea, input[type="datetime-local"], input[type="date"], input[type="number"]').length, 0);
      assert.doesNotMatch(dialog.textContent ?? "", /Report clinician rehab assessment|assessor|heel-rise|grip task|assessment time/i);
      await click(container, "Strengthening / loading");
      assert.equal(calls.length, 1);
      assert.equal(calls[0].event_type, "clinician_clearance_report");
      assert.deepEqual(calls[0].rehabilitation_permission, { schema_version: 1, level: "loading" });
      assert.equal("assessment" in calls[0], false);
    } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
  });
}

for (const [label, scopes] of [
  ["Rehab only", ["rehab"]],
  ["Non-contact training", ["rehab", "training"]],
  ["Contact training", ["rehab", "training", "contact"]],
] as const) {
  test(`clinician clearance submits unchanged scopes for ${label}`, async () => {
    const container = document.createElement("div"); document.body.appendChild(container);
    const root = createRoot(container);
    const original = globalThis.fetch;
    const calls: Array<Record<string, unknown>> = [];
    let refreshes = 0;
    globalThis.fetch = (async (_input, init) => {
      calls.push(JSON.parse(String(init?.body)));
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }) as typeof fetch;
    try {
      await act(async () => { root.render(<InjuryCareStatus injury={injury} token="token" onRefresh={async () => { refreshes++; }} />); });
      assert.match(container.textContent ?? "", /Rest day/);
      assert.match(container.textContent ?? "", /Next Fri 2 Oct/);
      assert.equal(container.querySelector("select"), null);
      assert.match(container.querySelector('[aria-expanded="false"]')?.textContent ?? "", /^Clearance/);
      assert.equal(document.querySelector('[role="dialog"]'), null);
      assert.equal(container.querySelector('a[href*="nhs"]'), null);
      await click(container, "Report clearance");
      assert.ok(document.querySelector('[role="dialog"] details a[href*="nhs"]'));
      assert.match(document.querySelector('[role="dialog"]')?.textContent ?? "", /Restrictions and safety holds still apply./);
      assert.equal(document.querySelector('[role="dialog"]')?.querySelectorAll("select").length, 0);
      assert.equal(document.querySelector('[role="dialog"]')?.querySelectorAll('input[type="radio"]').length, 7);
      await click(container, label);
      assert.match(String(calls[0].report_id), /^[a-f0-9-]{36}$/);
      const { report_id: firstReportId, ...body } = calls[0];
      assert.deepEqual(body, { injury_id: "injury-1", injury_episode_id: "episode-1", event_type: "clinician_clearance_report", scopes, rehabilitation_permission: { schema_version: 1, level: "not_cleared" } });
      assert.equal(refreshes, 1);
      assert.equal(document.querySelector('[role="dialog"]'), null);
      await click(container, "Report clearance");
      assert.equal(document.querySelector('[role="dialog"]')?.querySelectorAll("select").length, 0);
      assert.equal(document.querySelector('[role="dialog"]')?.querySelectorAll('input[type="radio"]').length, 7);
      await click(container, label);
      assert.notEqual(calls[1].report_id, firstReportId);
      assert.equal(container.querySelector('input[type="file"]'), null);
    } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
  });
}

for (const response of ["better", "same", "worse", "not_sure"] as const) {
  test(`next-day ${response} is saved separately from the during response`, async () => {
    const container = document.createElement("div"); document.body.appendChild(container);
    const root = createRoot(container);
    const original = globalThis.fetch;
    const calls: Array<Record<string, unknown>> = [];
    globalThis.fetch = (async (_input, init) => {
      calls.push(JSON.parse(String(init?.body)));
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }) as typeof fetch;
    try {
      await act(async () => { root.render(<DelayedRehabResponse token="token" onRefresh={async () => {}} prompt={{
        exposure_id: "exposure-1", injury_id: "injury-1", injury_episode_id: "episode-1", region: "chest",
        question: "How did this injury feel the day after rehab?", options: ["better", "same", "worse", "not_sure"],
      }} />); });
      assert.equal(container.querySelectorAll(".today-segment-row > .today-segment").length, 4);
      await click(container, { better: "Better", same: "Same", worse: "Worse", not_sure: "Not sure" }[response]);
      assert.equal(calls[0].event_type, "delayed_rehab_response");
      assert.deepEqual(calls[0], {
        injury_id: "injury-1", injury_episode_id: "episode-1", event_type: "delayed_rehab_response",
        exposure_id: "exposure-1", response,
      });
      assert.equal(calls[0].exposure_id, "exposure-1");
      assert.match(container.textContent ?? "", /Saved\. Thanks\./);
    } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
  });
}

test("clearance retry keeps the report identity when refresh fails after saving", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const original = globalThis.fetch;
  const calls: Array<Record<string, unknown>> = [];
  let refreshes = 0;
  globalThis.fetch = (async (_input, init) => {
    calls.push(JSON.parse(String(init?.body)));
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    await act(async () => { root.render(<InjuryCareStatus injury={injury} token="token" onRefresh={async () => {
      if (++refreshes === 1) throw new Error("Refresh failed");
    }} />); });
    await click(container, "Report clearance");
    await click(container, "Rehab only");
    assert.match(document.querySelector('[role="dialog"]')?.textContent ?? "", /Refresh failed/);
    await click(container, "Rehab only");
    assert.equal(calls.length, 2);
    assert.equal(calls[0].report_id, calls[1].report_id);
  } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
});

test("unsupported guidance shows a repeated reason only once and keeps schedule state", () => {
  const summary = "No suitable rehab is available for this injury yet. Keep to your current restrictions.";
  for (const reason of [summary, "A separate schedule reason."]) {
    const html = renderToStaticMarkup(<InjuryCareStatus injury={{ ...injury, rehab_decision: {
      outcome: "unsupported_prescription", summary, reason_codes: [],
      schedule: { state: "unsupported", reason },
    } }} token="token" onRefresh={async () => {}} />);
    assert.match(html, /No rehab exercises available for this stage yet\./);
    assert.doesNotMatch(html, /Recovery monitoring|safety checks still apply/);
    assert.doesNotMatch(html, /No rehab yet/);
  }
});

for (const [reasonCodes, summary, expected] of [
  [["missing_injury_identity"], "Add the injury area and type so your rehab can be matched.", "Guided rehab could not be matched. Review your injury details."],
  [["stage_not_activated"], "More injury-specific evidence is needed before changing your rehab.", "More injury-specific evidence is needed before changing your rehab."],
  [["reviewed_readiness_dose_missing"], "Follow today's reduced-training guidance. No rehab is set for days like this.", "Follow today's reduced-training guidance. No rehab is set for days like this."],
  [[], "Your clinician must review the assessment first.", "Your clinician must review the assessment first."],
] as const) {
  test(`missing-information guidance preserves the meaning of ${reasonCodes.join(",") || "no reason code"}`, () => {
    for (const reason of [undefined, summary, "A separate schedule reason."]) {
      const html = renderToStaticMarkup(<InjuryCareStatus injury={{ ...injury, rehab_decision: {
        outcome: "missing_information", summary, reason_codes: [...reasonCodes],
        ...(reason ? { schedule: { state: "held", reason } } : {}),
      } }} token="token" onRefresh={async () => {}} />);
      const text = new domWindow.DOMParser().parseFromString(html, "text/html").body.textContent ?? "";
      assert.equal(text.split(expected).length - 1, 1);
      if (!reasonCodes.some((code) => code === "missing_injury_identity")) {
        assert.doesNotMatch(text, /Add injury area and type/);
      } else {
        assert.ok(!text.includes(summary));
      }
      if (reason) assert.match(text, /Today’s rehab on hold/);
      if (reason && reason !== summary) assert.ok(text.includes(reason));
    }
  });
}

for (const state of ["due", "recovery_day", "already_completed", "held", "deferred", "unsupported"] as const) {
  test(`${state} guidance shows its label once, the next day, and a reason only when it explains something`, () => {
    const summary = "Keep the current injury restrictions in place.";
    const html = renderToStaticMarkup(<InjuryCareStatus rehabHref="#rehab-row" injury={{ ...injury, rehab_decision: {
      outcome: "missing_information", summary, reason_codes: [],
      schedule: { state, reason: summary, next_due_day: "2026-10-09" },
    } }} token="token" onRefresh={async () => {}} />);
    const text = new domWindow.DOMParser().parseFromString(html, "text/html").body.textContent ?? "";
    assert.equal(text.split(summary).length - 1, 1);
    assert.ok(text.includes({ due: "Rehab due today", recovery_day: "Rest day", already_completed: "Done for today",
      held: "Today’s rehab on hold", deferred: "Rehab moved", unsupported: "No rehab yet" }[state]));
    assert.equal(text.includes("Next Fri 9 Oct"), state !== "due");
  });
}

test("a matched routine drops the boilerplate summary and the routine reason", () => {
  for (const [state, reason, label] of [
    ["already_completed", "You have already logged rehab for this injury today.", "Done for today"],
    ["already_completed", "Today's rehab allocation is reserved by your started session.", "In today's session"],
    ["recovery_day", "Allow the configured recovery gap before repeating this routine.", "Rest day"],
    ["due", "Your baseline rehab is due today; missed work adds no extra volume.", "Rehab due today"],
  ] as const) {
    const html = renderToStaticMarkup(<InjuryCareStatus rehabHref="#rehab-row" injury={{ ...injury, rehab_decision: {
      outcome: "prescribed_rehab", summary: "Your rehab is matched to this injury's current recovery stage.", reason_codes: [],
      schedule: { state, reason, next_due_day: "2026-10-10" },
    } }} token="token" onRefresh={async () => {}} />);
    const text = new domWindow.DOMParser().parseFromString(html, "text/html").body.textContent ?? "";
    assert.ok(text.includes(label), label);
    assert.ok(!text.includes("current recovery stage"));
    assert.ok(!text.includes(reason));
  }
  const held = renderToStaticMarkup(<InjuryCareStatus rehabHref="#rehab-row" injury={{ ...injury, rehab_decision: {
    outcome: "prescribed_rehab", summary: "Your rehab is matched to this injury's current recovery stage.", reason_codes: [],
    schedule: { state: "deferred", reason: "Demanding training already loads this region today.", next_due_day: "2026-10-10" },
  } }} token="token" onRefresh={async () => {}} />);
  assert.match(held, /Rehab moved/);
  assert.match(held, /Demanding training already loads this region today\./);
});

test("a next-day question that is no longer open disappears instead of showing a server error", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const original = globalThis.fetch;
  let refreshes = 0;
  globalThis.fetch = (async () => new Response(JSON.stringify({ detail: "The next-day response opens on a later training day." }),
    { status: 409, headers: { "content-type": "application/json", "x-request-id": "9fb3b8a7" } })) as typeof fetch;
  try {
    await act(async () => { root.render(<DelayedRehabResponse token="token" onRefresh={async () => { refreshes += 1; }} prompt={{
      exposure_id: "exposure-1", injury_id: "injury-1", injury_episode_id: "episode-1", region: "Chest",
      question: "How did this injury feel the day after rehab?", options: ["better", "same", "worse", "not_sure"],
    }} />); });
    assert.match(container.textContent ?? "", /How's your chest after yesterday's rehab\?/);
    await click(container, "Better");
    assert.equal(container.textContent, "");
    assert.equal(refreshes, 1);
  } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
});

for (const [level, scopes, label] of [
  ["train_no_contact", ["rehab", "training"], "Non-contact training"],
  ["train_contact", ["rehab", "training", "contact"], "Contact training"],
] as const) {
  test(`overall clearance preserves the ${label} wording`, () => {
    const html = renderToStaticMarkup(<EffectiveClinicianClearanceStatus clearance={{ level, scopes: [...scopes],
      requires_update: false, limited_by: [{ injury_id: injury.id, injury_episode_id: injury.episode_id!, label: "Chest strain" }],
    }} />);
    assert.ok(html.includes(`Reported clearance: ${label}`));
    assert.doesNotMatch(html, /Train, no contact|Train \+ contact/);
  });
}

for (const [scopes, label] of [
  [["rehab"], "Rehab only"],
  [["rehab", "training"], "Non-contact training"],
  [["rehab", "training", "contact"], "Contact training"],
] as const) {
  test(`current clearance shows ${label} and an update action`, () => {
    const html = renderToStaticMarkup(<InjuryCareStatus injury={{ ...injury, clinician_clearance: {
      episode_id: injury.episode_id!, scopes: [...scopes], source: "athlete_reported", externally_verified: false,
    } }} token="token" onRefresh={async () => {}} />);
    assert.ok(html.includes(`${label}`));
    assert.ok(html.includes("Change clearance"));
    assert.ok(!html.includes("Report clearance"));
  });
}

test("updating clearance displays the refreshed scope, and worsening removes current clearance", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const original = globalThis.fetch;
  let current: InjuryFlagRecord = { ...injury, clinician_clearance: {
    episode_id: injury.episode_id!, scopes: ["rehab", "training"], source: "athlete_reported", externally_verified: false,
  } };
  const calls: Array<Record<string, unknown>> = [];
  const render = () => root.render(<InjuryCareStatus injury={current} token="token" onRefresh={async () => {
    render();
  }} />);
  globalThis.fetch = (async (_input, init) => {
    const body = JSON.parse(String(init?.body)); calls.push(body);
    current = { ...current, clinician_clearance: { ...current.clinician_clearance!, scopes: body.scopes } };
    return new Response("{}", { status: 200 });
  }) as typeof fetch;
  try {
    await act(async () => { render(); });
    assert.match(container.textContent ?? "", /Non-contact training/);
    await click(container, "Change clearance");
    await click(container, "Contact training");
    assert.deepEqual(calls[0].scopes, ["rehab", "training", "contact"]);
    assert.equal(calls[0].event_type, "clinician_clearance_report");
    assert.ok(!("status" in calls[0]));
    assert.equal(current.status, "open");
    assert.match(container.textContent ?? "", /Contact training/);
    assert.doesNotMatch(container.textContent ?? "", /Non-contact training/);
    current = { ...current, clinician_clearance: null, latest_reported_status: "worse" };
    await act(async () => { render(); });
    assert.equal(container.querySelector('[aria-label="Change clearance"]'), null);
    assert.ok(container.querySelector('[aria-label="Report clearance"]'));
  } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
});

test("no current clearance uses the report action without claiming a current scope", () => {
  const html = renderToStaticMarkup(<InjuryCareStatus injury={injury} token="token" onRefresh={async () => {}} />);
  assert.ok(html.includes("Report clearance"));
  assert.ok(!html.includes("Non-contact training"));
});


for (const scopes of [["contact"], ["training"], ["rehab", "contact"], [], ["rehab", "rehab"]] as const) {
  test(`noncanonical clearance ${scopes.join(",")} never displays a training grant`, () => {
    const html = renderToStaticMarkup(<InjuryCareStatus injury={{ ...injury, clinician_clearance: {
      episode_id: injury.episode_id!, scopes: [...scopes], source: "athlete_reported", externally_verified: false,
    } }} token="token" onRefresh={async () => {}} />);
    assert.match(html, /Scope unclear/);
    assert.doesNotMatch(html, /Train/);
  });
}

test("effective clearance remains authoritative above a permissive individual report", () => {
  const html = renderToStaticMarkup(<>
    <EffectiveClinicianClearanceStatus clearance={{ level: "rehab_only", scopes: ["rehab"], requires_update: false,
      limited_by: [{ injury_id: injury.id, injury_episode_id: injury.episode_id!, label: "Chest strain" }] }} />
    <InjuryCareStatus injury={{ ...injury, id: "ankle", clinician_clearance: {
      episode_id: injury.episode_id!, scopes: ["rehab", "training", "contact"], source: "athlete_reported", externally_verified: false,
    } }} token="token" onRefresh={async () => {}} />
  </>);
  assert.match(html, /<strong>Reported clearance: Rehab only<\/strong>/);
  assert.match(html, /limited by Chest strain/);
  assert.match(html, /Contact training/);
  assert.doesNotMatch(html, /Reported clearance: Train/);
});

test("unclear effective scope explains its conservative ceiling", () => {
  const html = renderToStaticMarkup(<EffectiveClinicianClearanceStatus clearance={{
    level: "rehab_only", scopes: ["rehab"], requires_update: true,
    limited_by: [{ injury_id: injury.id, injury_episode_id: injury.episode_id!, label: "Chest strain" }],
  }} />);
  assert.match(html, /Reported clearance: Rehab only/);
  assert.match(html, /Rehab only until clarified/);
  assert.equal(renderToStaticMarkup(<EffectiveClinicianClearanceStatus clearance={null} />), "");
});

test("full clinician clearance names its report without implying an injury limitation", () => {
  const html = renderToStaticMarkup(<EffectiveClinicianClearanceStatus clearance={{
    level: "train_contact", scopes: ["rehab", "training", "contact"], requires_update: false,
    limited_by: [{ injury_id: injury.id, injury_episode_id: injury.episode_id!, label: "Chest strain" }],
  }} />);
  assert.match(html, /Reported clearance: Contact training/);
  assert.match(html, /based on Chest strain/);
  assert.doesNotMatch(html, /limited by/);
});


test("Clinical Clearance has two simple selectors and sends versioned self-reported permissions", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const original = globalThis.fetch;
  const calls: Array<Record<string, unknown>> = [];
  globalThis.fetch = (async (_input, init) => { calls.push(JSON.parse(String(init?.body))); return new Response("{}", { status: 200 }); }) as typeof fetch;
  try {
    await act(async () => root.render(<InjuryCareStatus injury={injury} token="token" onRefresh={async () => {}} />));
    assert.equal(container.querySelector("select"), null);
    assert.doesNotMatch(container.textContent ?? "", /does not issue or verify/);
    await click(container, "Report clearance");
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
    assert.match(dialog.textContent ?? "", /Clinical Clearance · Self-reported/);
    assert.match(dialog.textContent ?? "", /does not issue or verify medical clearance/);
    assert.equal(dialog.querySelectorAll('input[type="radio"]').length, 7);
    assert.equal(dialog.querySelectorAll("textarea, input[type=text]").length, 0);
    await act(async () => { dialog.querySelector<HTMLInputElement>('input[value="loading"]')!.click(); });
    const training = dialog.querySelector<HTMLInputElement>('input[value="training"]')!;
    await act(async () => {
      training.click();
    });
    await click(container, "Save changes");
    assert.deepEqual(calls[0].rehabilitation_permission, { schema_version: 1, level: "loading" });
    assert.deepEqual(calls[0].scopes, ["rehab", "training"]);
  } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
});


test("compact clearance shows only the training level; the rest is in its sheet", () => {
  const html = renderToStaticMarkup(<InjuryCareStatus injury={{ ...injury, clinician_clearance: {
    episode_id: "episode-1", scopes: ["rehab", "training"], source: "athlete_reported", externally_verified: false,
  } }} token="token" onRefresh={async () => {}} />);
  assert.match(html, /<span>Clearance<\/span><span class="injury-clearance-value">Non-contact training<\/span>/);
  assert.doesNotMatch(html, /Not sure \/ not cleared|Self-reported/);
});

test("a routine state folds under the stage block, which opens progression requirements itself", () => {
  const html = renderToStaticMarkup(<InjuryCareStatus injury={{ ...injury, rehab_decision: {
    outcome: "prescribed_rehab", summary: "Your rehab is matched to this injury's current recovery stage.", reason_codes: [], stage: "restore",
    progression: { next_transition: { to_stage: "load", status: "blocked", target_stage_live: true, reason_codes: [], requirements: [] } },
    schedule: { state: "already_completed", reason: "You have already logged rehab for this injury today.", next_due_day: "2026-10-10" },
  } }} token="token" onRefresh={async () => {}} />);
  const body = new domWindow.DOMParser().parseFromString(html, "text/html").body;
  const stage = body.querySelector<HTMLButtonElement>("button.injury-progress");
  assert.ok(stage);
  assert.equal(stage.getAttribute("aria-haspopup"), "dialog");
  assert.equal(stage.querySelector(".injury-progress-schedule")?.textContent?.replace(/\u00a0/g, " "), "Done for today · next Sat 10 Oct");
  assert.match(stage.getAttribute("aria-label") ?? "", /Progression requirements/);
  // No separate requirements row and no separate schedule row.
  assert.equal(body.querySelector(".injury-progress-link"), null);
  assert.equal(body.querySelector(".injury-rehab-label"), null);
  assert.equal((body.textContent ?? "").split("Done for today").length - 1, 1);
});

test("rehab due today keeps its own row: it links to the exercises", () => {
  const html = renderToStaticMarkup(<InjuryCareStatus rehabHref="#actual-rehab-exercise" injury={{ ...injury, rehab_decision: {
    outcome: "prescribed_rehab", summary: "Matched.", reason_codes: [], stage: "restore",
    schedule: { state: "due", reason: "Due.", next_due_day: "2026-10-10" },
  } }} token="token" onRefresh={async () => {}} />);
  assert.match(html, /class="injury-rehab-action" href="#actual-rehab-exercise"/);
  assert.doesNotMatch(html, /injury-progress-status/);
});

test("surface guidance sources remain available without a clearance sheet", () => {
  const html = renderToStaticMarkup(<InjuryCareStatus injury={{ ...injury, surface_class: "stable_surface", rehab_decision: {
    ...injury.rehab_decision!, outcome: "wound_care",
  } }} token="token" onRefresh={async () => {}} />);
  assert.match(html, /Guidance sources/);
  assert.match(html, /https:\/\/www.nhs.uk\/conditions\/sprains-and-strains\//);
  assert.doesNotMatch(html, /Report clearance/);
});


test("unsupported elbow monitoring does not imply a route to Load, even with permission", () => {
  for (const outcome of ["unsupported_prescription", "missing_information"]) {
    const html = renderToStaticMarkup(<InjuryCareStatus injury={{ ...injury, body_area: "Right elbow", description: "tightness", clinician_clearance: {
      episode_id: "episode-1", scopes: ["rehab", "training", "contact"], source: "athlete_reported", externally_verified: false,
      rehabilitation_permission: { schema_version: 1, level: "sport_specific" },
    }, rehab_decision: { outcome, summary: "Add the injury area and type so your rehab can be matched.", reason_codes: outcome === "missing_information" ? ["missing_injury_identity"] : [], stage: "restore",
      progression: { next_transition: { to_stage: "load", status: "closed", target_stage_live: false, reason_codes: [] } }, schedule: { state: "unsupported", reason: "No matching exercise." },
    } }} token="token" onRefresh={async () => {}} />);
    assert.match(html, outcome === "unsupported_prescription" ? /No rehab exercises available for this stage yet\./ : /Review your injury details/);
    assert.match(html, /injury-clearance-value">Contact training</);
    assert.doesNotMatch(html, /Rehabilitation stages|Next:|Progression requirements|unlock rehab|Add injury area/);
  }
});

test("resolved injuries do not need to traverse rehab stages", () => {
  const html = renderToStaticMarkup(<InjuryCareStatus injury={{ ...injury, rehab_decision: { outcome: "no_rehab_indicated", summary: "Resolved", reason_codes: ["resolved_episode"], stage: "return" } }} token="token" onRefresh={async () => {}} />);
  assert.match(html, /Recovered/);
  assert.doesNotMatch(html, /Rehabilitation stages/);
});


test("a due decision without a rendered exercise target never promises access", () => {
  const html = renderToStaticMarkup(<InjuryCareStatus injury={{ ...injury, rehab_decision: {
    ...injury.rehab_decision!, outcome: "prescribed_rehab",
    schedule: { state: "due", reason: "Due." },
  } }} token="token" onRefresh={async () => {}} />);
  assert.doesNotMatch(html, /Rehab due today|injury-rehab-action|#today-session/);
  assert.match(html, /Rehab unavailable/);
});

test("medical-review explanations follow the engine gate rather than injury severity", () => {
  const render = (outcome: string, reason_codes: string[]) => renderToStaticMarkup(<InjuryCareStatus injury={{ ...injury,
    severity: "severe", rehab_decision: { outcome, reason_codes, summary: "Follow today’s guidance." },
  }} token="token" onRefresh={async () => {}} />);
  assert.match(render("medical_review", ["urgent_injury_type"]), /injury type you reported needs medical review/);
  assert.doesNotMatch(render("prescribed_rehab", ["red_flag_medical_gate"]), /reported warning sign needs medical review/);
  assert.doesNotMatch(render("medical_review", ["unknown_gate"]), /urgent_injury_type|reported warning sign|unknown_gate/);
});
