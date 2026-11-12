import test from "node:test";
import assert from "node:assert/strict";
import { window as domWindow } from "../test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { RehabProgressStatus, RehabStageMeter, rehabNextStep, rehabEntryExplanation } from "./rehab-progress-status";
import { SHEET_EXIT_MS } from "@/lib/use-sheet-exit";
import type { InjuryFlagRecord } from "@/lib/types";

const decision: NonNullable<InjuryFlagRecord["rehab_decision"]> = {
  outcome: "prescribed_rehab", summary: "Reviewed rehab", reason_codes: [], stage: "restore",
  progression: { next_transition: { to_stage: "load", status: "blocked", target_stage_live: true,
    reason_codes: ["rehabilitation_loading_not_reported"], requirements: [
      { requirement_id: "history", kind: "complete_history", status: "pass", reason_code: "history_complete" },
      { requirement_id: "permission", kind: "functional_checkpoint", status: "unknown", reason_code: "rehabilitation_loading_not_reported" },
    ] } },
};

test("the collapsed injury summary distinguishes current stage from unpublished future content", () => {
  const current = { ...decision, progression: { next_transition: {
    ...decision.progression!.next_transition!, target_stage_live: false, status: "blocked",
    reason_codes: ["no_reviewed_stage_exposure", "rehabilitation_loading_not_reported"],
  } } };
  const html = renderToStaticMarkup(<RehabStageMeter decision={current} />);
  assert.match(html, /Current rehab stage: Restore/);
  assert.match(html, /CALM → RESTORE → LOAD → DYNAMIC → RETURN/);
  assert.match(html, /LOAD exercises aren’t available for this injury in UNLXCK yet/);
  assert.doesNotMatch(html, /Complete your|Confirm what|unlock|Closed/);
  assert.equal((html.match(/data-current="true"/g) ?? []).length, 1);
});

for (const [codes, message] of [
  [["no_reviewed_stage_exposure"], /Complete your current rehabilitation exercise in Today/],
  [["next_day_response_not_yet_known"], /Tomorrow, answer the follow-up/],
  [["reviewed_target_content_unavailable", "target_equipment_unavailable"], /equipment you haven’t listed/],
  [["rehabilitation_loading_not_reported", "fail_next_day_response_worse"], /Progression is on hold.*felt worse the next day/],
] as const) {
  test(`the first visible next action uses actual blockers: ${codes.join(", ")}`, () => {
    const current = { ...decision, progression: { next_transition: {
      ...decision.progression!.next_transition!, reason_codes: [...codes], requirements: [],
    } } };
    assert.match(rehabNextStep(current)!, message);
    assert.match(renderToStaticMarkup(<RehabStageMeter decision={current} />), message);
  });
}

test("a live but closed transition cannot offer a progression override", () => {
  assert.match(rehabNextStep({ ...decision, progression: { next_transition: {
    ...decision.progression!.next_transition!, status: "closed",
  } } })!, /Progression is on hold for this injury/);
});

for (const [reason, message] of [
  ["next_day_response_not_yet_known", /Tomorrow, answer the follow-up/],
  ["fail_next_day_response_worse", /felt worse the next day/],
  ["shoulder_nontraumatic_cuff_not_reported", /Edit injury/],
  ["target_equipment_unavailable", /equipment you haven’t listed/],
  ["unresolved_injury_checkin_setback", /reported this injury worse earlier/],
]) {
  test(`the actual runtime blocker ${reason} has a specific explanation`, async () => {
    const container = document.createElement("div"); document.body.appendChild(container);
    const root = createRoot(container);
    try {
      await act(async () => root.render(<RehabProgressStatus decision={{ ...decision, progression: {
        next_transition: { ...decision.progression!.next_transition!, reason_codes: [reason as string], requirements: [] },
      } }} />));
      await act(async () => container.querySelector("button")!.click());
      const dialog = document.querySelector('[role="dialog"]')!;
      assert.match(dialog.textContent ?? "", message as RegExp);
      assert.equal(dialog.querySelectorAll("form, input, select, textarea").length, 0);
    } finally { act(() => root.unmount()); container.remove(); }
  });
}

test("progression is one compact row, only when the engine supplies a stage", () => {
  assert.equal(renderToStaticMarkup(<RehabProgressStatus decision={{ ...decision, stage: undefined }} />), "");
  const html = renderToStaticMarkup(<RehabProgressStatus decision={decision} statusLine="Done for today · next Sat 10 Oct" />);
  assert.match(html, /<button[^>]*class="injury-progress"/);
  assert.match(html, /Load · On hold/);
  assert.match(html, /Confirm the rehab loading your clinician allows/);
  assert.match(html, /Done for today · next Sat 10 Oct/);
  // The summary row above already shows the stage and its meter.
  assert.doesNotMatch(html, /aria-current="step"|data-complete|Rehab stage<|\d+%/);
});

test("permission entry explains its authority without claiming observed recovery", () => {
  const html = renderToStaticMarkup(<RehabProgressStatus decision={{ ...decision, stage: "load",
    progression: { ...decision.progression, entry_basis: "reported_clinician_permission" } }} />);
  assert.match(html, /LOAD follows your reported clinician advice/);
  assert.match(html, /RESTORE completion isn’t needed/);
  assert.doesNotMatch(html, /clinically verified|recovery proven|three days/i);
});

test("the collapsed LOAD summary explains permission entry before future availability", () => {
  const current = { ...decision, stage: "load" as const, progression: {
    entry_basis: "reported_clinician_permission" as const,
    next_transition: { to_stage: "dynamic", status: "blocked", target_stage_live: false, reason_codes: [] },
  } };
  const html = renderToStaticMarkup(<RehabStageMeter decision={current} />);
  assert.match(html, /RESTORE completion isn’t needed/);
  assert.match(html, /DYNAMIC exercises aren’t available/);
  assert.ok(html.indexOf("RESTORE completion") < html.indexOf("DYNAMIC exercises"));
});

test("normal entry requires the engine's matching successful transition", () => {
  const current = { ...decision, stage: "load" as const, progression: {
    reason_codes: ["transition_met:restore->load"],
  } };
  assert.match(rehabEntryExplanation(current)!, /meet the checks for this LOAD exercise/);
  assert.match(renderToStaticMarkup(<RehabProgressStatus decision={current} />), /meet the checks/);
  assert.equal(rehabEntryExplanation({ ...current, progression: {} }), null);
  assert.equal(rehabEntryExplanation({ ...current, progression: { reason_codes: ["transition_met:load->dynamic"] } }), null);
  assert.equal(rehabEntryExplanation({ ...current, stage: "restore" }), null);
  assert.equal(rehabEntryExplanation({ ...current, outcome: "missing_information" }), null);
});

test("a blocked permission entry cannot claim that LOAD is permitted", () => {
  const attempted = { ...decision.progression!.next_transition!, entry_basis: "reported_clinician_permission" };
  assert.equal(rehabEntryExplanation({ ...decision, progression: {
    next_transition: attempted,
  } }), null);
});

test("a held schedule does not remove stage eligibility or create a progression hold", () => {
  const current = { ...decision, stage: "load" as const,
    schedule: { state: "held" as const, reason: "Complete today's check-in and follow its training guidance." },
    progression: { entry_basis: "reported_clinician_permission" as const } };
  assert.equal(rehabNextStep(current), null);
  assert.match(rehabEntryExplanation(current)!, /LOAD follows your reported clinician advice/);
  const html = renderToStaticMarkup(<RehabStageMeter decision={current} />);
  assert.match(html, /Current rehab stage: Load/);
  assert.doesNotMatch(html, /Progression is on hold|check-in/);
  assert.match(rehabEntryExplanation({ ...current, progression: {
    reason_codes: ["transition_met:restore->load"],
  } })!, /You meet the checks for this LOAD exercise/);
});

test("a real safety hold still blocks progression when today's schedule is also held", () => {
  const current = { ...decision, stage: "calm" as const,
    schedule: { state: "held" as const, reason: "Complete today's check-in and follow its training guidance." },
    progression: { reason_codes: ["episode_setback_loading_held"] } };
  assert.match(rehabNextStep(current)!, /Progression is on hold.*injury got worse/);
  assert.equal(rehabEntryExplanation(current), null);
});

test("expanded injury controls do not repeat the visible summary", () => {
  const html = renderToStaticMarkup(<RehabProgressStatus decision={decision} summaryVisible />);
  const body = new domWindow.DOMParser().parseFromString(html, "text/html").body;
  // One status line per card: the row is only the way into the checks.
  assert.match(body.textContent ?? "", /Progression checks/);
  assert.doesNotMatch(body.textContent ?? "", /On hold|Confirm the rehab loading/);
  const held = { ...decision, stage: "calm" as const,
    progression: { reason_codes: ["episode_setback_loading_held"] } };
  assert.equal(renderToStaticMarkup(<RehabProgressStatus decision={held} summaryVisible />), "");
});

test("the real CALM setback decision stays visible without a next transition", () => {
  const current = { ...decision, stage: "calm" as const,
    progression: { reason_codes: ["episode_setback_loading_held"] } };
  for (const html of [renderToStaticMarkup(<RehabStageMeter decision={current} />),
    renderToStaticMarkup(<RehabProgressStatus decision={current} />)]) {
    assert.match(html, /Progression is on hold/);
    assert.match(html, /injury got worse/);
    assert.doesNotMatch(html, /Future availability varies|Complete your|Confirm what|<button/);
  }
});

test("a safety hold wins over unpublished content and successful entry copy", () => {
  const current = { ...decision, stage: "load" as const, reason_codes: ["current_report_worse"], progression: {
    entry_basis: "reported_clinician_permission" as const,
    next_transition: { ...decision.progression!.next_transition!, to_stage: "dynamic", target_stage_live: false },
  } };
  assert.match(rehabNextStep(current)!, /Progression is on hold/);
  assert.equal(rehabEntryExplanation(current), null);
  const html = renderToStaticMarkup(<RehabProgressStatus decision={current} />);
  assert.match(html, /Progression · On hold/);
  assert.doesNotMatch(html, /Unavailable in UNLXCK|without prior RESTORE/);
});

test("unneeded entry evidence is explained as not required, never completed", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RehabProgressStatus decision={{ ...decision, progression: {
      next_transition: { ...decision.progression!.next_transition!, reason_codes: [], requirements: [{
        requirement_id: "prior_work", kind: "completed_reviewed_exposure", status: "not_applicable",
        reason_code: "prior_stage_work_not_required_for_reported_permission",
      }] },
    } }} />));
    await act(async () => container.querySelector("button")!.click());
    const dialog = document.querySelector('[role="dialog"]')!;
    assert.match(dialog.textContent ?? "", /Current-stage rehabNot needed/);
    assert.doesNotMatch(dialog.textContent ?? "", /Completed current-stage rehabComplete/);
    assert.equal(dialog.querySelectorAll("form, input, select, textarea").length, 0);
  } finally { act(() => root.unmount()); container.remove(); }
});

test("a closed next stage says so, and a stage with no next stage and no status renders nothing", () => {
  const closed = renderToStaticMarkup(<RehabProgressStatus decision={{ ...decision, progression: { next_transition: {
    ...decision.progression!.next_transition!, status: "closed", target_stage_live: false } } }} />);
  assert.match(closed, /Load · Unavailable in UNLXCK/);
  assert.match(closed, /LOAD exercises aren’t available for this injury in UNLXCK yet/);
  assert.doesNotMatch(closed, /Confirm what|Complete your|Closed|unlock/);
  assert.equal(renderToStaticMarkup(<RehabProgressStatus decision={{ ...decision, progression: {} }} />), "");
});

for (const closed of [false, true]) {
  test(`progression details preserve engine holds and closed=${closed}`, async () => {
    const container = document.createElement("div"); document.body.appendChild(container);
    const root = createRoot(container);
    const current = closed ? { ...decision, stage: "load" as const, progression: { next_transition: {
      ...decision.progression!.next_transition!, to_stage: "dynamic", status: "closed", target_stage_live: false,
    } } } : decision;
    try {
      await act(async () => root.render(<RehabProgressStatus decision={current} />));
      const trigger = container.querySelector("button")!;
      trigger.focus();
      await act(async () => trigger.click());
      const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
      assert.ok(dialog);
      assert.equal(document.body.style.position, "fixed");
      if (closed) {
        assert.match(dialog.textContent ?? "", /DYNAMIC exercises aren’t available for this injury in UNLXCK yet/);
        assert.equal(dialog.querySelector(".injury-requirements"), null);
      } else {
        assert.match(dialog.textContent ?? "", /Complete rehab historyDone/);
        assert.match(dialog.textContent ?? "", /Waiting/);
        assert.match(dialog.textContent ?? "", /Confirm the rehab loading your clinician allows/);
        assert.doesNotMatch(dialog.textContent ?? "", /medical clearance issued|heel-rise|assessor/);
      }
      await act(async () => document.dispatchEvent(new domWindow.KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
      // The sheet plays its closing animation before it is removed.
      await act(async () => { await new Promise(resolve => setTimeout(resolve, SHEET_EXIT_MS + 50)); });
      // Compared as a boolean: a failing assert on a DOM node makes node:assert
      // inspect the whole jsdom graph, which exhausts memory instead of failing.
      assert.equal(document.querySelector('[role="dialog"]') === null, true);
      assert.equal(document.activeElement, trigger);
      assert.equal(document.body.style.position, "");
    } finally { act(() => root.unmount()); container.remove(); }
  });
}


test("missing rehab responses direct athletes to rehab records, not the daily check-in", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RehabProgressStatus decision={{ ...decision, progression: { next_transition: {
      ...decision.progression!.next_transition!, reason_codes: ["during_response_not_reported", "next_day_response_not_reported"], requirements: [],
    } } }} />));
    await act(async () => container.querySelector("button")!.click());
    const text = document.querySelector('[role="dialog"]')!.textContent ?? "";
    // One next action: answer how the rehab felt today; the next-day check waits.
    assert.match(text, /Next: After your rehab, tap how it feels under How’s your injury/);
    assert.doesNotMatch(text, /next-day rehab follow-up/);
    assert.doesNotMatch(text, /Better, Same or Worse/);
  } finally { act(() => root.unmount()); container.remove(); }
});

test("missing elbow suitability shows the actual blocker once without another questionnaire", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RehabProgressStatus decision={{ ...decision, policy_id: "elbow_tendonitis",
      progression: { next_transition: { ...decision.progression!.next_transition!,
        reason_codes: ["assessment_observation_missing", "elbow_function_assessment_incomplete"], requirements: [
          { requirement_id: "function", kind: "functional_checkpoint", status: "unknown", reason_code: "assessment_observation_missing" },
          { requirement_id: "observations", kind: "input_availability", status: "unknown", reason_code: "elbow_function_assessment_incomplete" },
        ] } } }} />));
    await act(async () => container.querySelector("button")!.click());
    const dialog = document.querySelector('[role="dialog"]')!;
    const text = dialog.textContent ?? "";
    assert.equal(dialog.querySelectorAll(".injury-requirements li").length, 1);
    assert.match(text, /LOAD is on hold/);
    assert.match(text, /Your elbow records do not confirm grip, movement or exercise tolerance/);
    assert.doesNotMatch(text, /Report your clinician|Leave unassessed|timestamp|date|questionnaire|rehab engine/i);
    assert.equal(dialog.querySelectorAll("form, input, select, textarea").length, 0);
  } finally { act(() => root.unmount()); container.remove(); }
});

for (const [state, codes, expected] of [
  ["due", ["unresolved_injury_checkin_setback"], /^Progression is on hold\. Do today’s rehab, then tap how it feels/],
  ["already_completed", ["during_response_not_reported"], /^Rehab done\. Tap how it feels/],
  ["already_completed", ["next_day_response_not_yet_known"], /^Rehab done\. Tomorrow, answer how it felt the day after/],
  ["already_completed", ["unresolved_injury_checkin_setback"], /Rehab before your last Better doesn’t count\. Next rehab Sat 24 Oct/],
  ["recovery_day", ["unresolved_injury_checkin_setback"], /Next rehab Sat 24 Oct\. After it, say how it felt/],
] as const) {
  test(`the next step matches today's rehab (${state}: ${codes.join(",")})`, () => {
    const current = { ...decision, schedule: { state, reason: "", next_due_day: "2026-10-24" },
      progression: { next_transition: { ...decision.progression!.next_transition!, reason_codes: [...codes], requirements: [] } } };
    const step = rehabNextStep(current)!;
    assert.match(step, expected);
    assert.doesNotMatch(step, /Log today’s rehab/);
    // The header meter shows the same sentence.
    assert.match(renderToStaticMarkup(<RehabStageMeter decision={current} />), new RegExp(expected.source.replace(/^\^/, "")));
  });
}

test("the progression sheet shows the recent injury log, and nothing when empty", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RehabProgressStatus decision={{ ...decision, recent_log: [
      { date: "2026-10-22", kind: "rehab", label: "Seated heel raise", felt: "not answered", next_day: "waiting", counts: false },
      { date: "2026-10-22", kind: "checkin", label: "Better" },
    ] }} />));
    await act(async () => container.querySelector("button")!.click());
    const text = document.querySelector('[role="dialog"]')!.textContent ?? "";
    assert.match(text, /Recent/);
    assert.match(text, /Thu 22 Oct Seated heel raise · Felt: not answered · Next day: waiting · doesn’t count/);
    assert.match(text, /Injury: Better/);
  } finally { act(() => root.unmount()); container.remove(); }
  const empty = renderToStaticMarkup(<RehabProgressStatus decision={decision} />);
  assert.doesNotMatch(empty, /injury-recent-log/);
});

test("stage definitions stay in a disclosure and distinguish stages from training clearance", () => {
  const html = renderToStaticMarkup(<RehabStageMeter decision={decision} />);
  assert.match(html, /<details class="injury-stage-guide"><summary>What do the stages mean/);
  assert.doesNotMatch(html, /<details class="injury-stage-guide" open/);
  assert.match(html, /Restore<\/dt><dd>Comfortable movement/);
  assert.match(html, /Stages are not clearance to train/);
  assert.match(html, /Some stages are not available for every injury/);
  assert.equal(renderToStaticMarkup(<RehabStageMeter decision={null} />), "");
});
