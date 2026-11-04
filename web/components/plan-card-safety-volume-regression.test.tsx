import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";

import { ExerciseRow, StructuredPlanRenderer } from "./structured-plan-renderer";
import type { StructuredPlan } from "@/lib/types";

function countOccurrences(text: string, needle: string): number {
  return text.split(needle).length - 1;
}

async function openedPlan(plan: StructuredPlan, today: Date): Promise<string> {
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  try {
    await act(async () => root.render(<StructuredPlanRenderer plan={plan} today={today} />));
    await act(async () => (host.querySelector(".ex-row-toggle") as HTMLButtonElement).click());
    return document.body.innerHTML;
  } finally { await act(async () => root.unmount()); host.remove(); }
}

// Anonymous reproduction of the linked D-31 stored plan. The root safety owner
// matters: rendering an isolated exercise did not reproduce this regression.
test("live swing keeps stop without stacked coaching or an empty Progress line", async () => {
  const plan = {
    schema_version: "1.0",
    raw_markdown_fallback: `D-31 (Tuesday): Strength
- Kettlebell Swing — 3 x 4 reps, rest 90 sec, explosive.
  Stop: slower speed, lost trunk control or pain.`,
    red_flag_rules: [{ rule_id: "pain", severity: "red", when: "during_session",
      display_text: "Stop training if pain develops.", action: "Stop training." }],
    weeks: [{week_index: 1, days: [{date: "2026-10-20", countdown_label: "D-31",
      sessions: [{session_id: "strength", session_type: "strength_power", title: "Strength",
        objective: "Build general strength and ballistic power with crisp reps",
        mindset_anchor: {focus_cue: "Explosive intent on swings and hops",
          reset_cue: "Full brace and reset between sets",
          intent: "Quality strength and speed on reps",
          confidence_anchor: "Quality reps matter more than load",
          context: "Keep trunk control and stop if pain"},
        blocks: [{block_id: "swing", display_name: "Kettlebell Swing", sets: 3, reps: 4,
          rest: {value: 90, unit: "sec"},
          coaching_cues: ["Explosive hip hinge, snap at top"],
          regression_options: ["Reduce sets to 2."],
          stop_rules: ["Stop if speed slows or pain develops."]}],
      }],
    }]}],
  } as StructuredPlan;
  const html = await openedPlan(plan, new Date(2026, 9, 20));
  const expanded = renderToStaticMarkup(<ExerciseRow
    block={plan.weeks![0].days![0].sessions![0].blocks![0]} open onToggle={() => {}} />);
  assert.equal(countOccurrences(expanded, "Explosive hip hinge, snap at top"), 1);
  for (const removed of ["Explosive intent on swings and hops", "Full brace and reset",
    "Quality strength and speed", "Quality reps matter more", "sp-coaching-list"]) {
    assert.equal(html.includes(removed), false, removed);
  }
  assert.equal(html.includes("Keep trunk control and stop if pain"), true);
  assert.equal(html.includes("slower speed, lost trunk control or pain"), true);
  assert.equal(expanded.includes(">Progress</span>"), false);
  const block = plan.weeks![0].days![0].sessions![0].blocks![0];
  const withProgress = renderToStaticMarkup(<ExerciseRow
    block={{...block, progression_rule: "At the same dose, refine hinge control."}} open onToggle={() => {}} />);
  assert.equal(countOccurrences(withProgress, ">Progress</span>"), 1);
  assert.equal(withProgress.includes("At the same dose, refine hinge control."), true);
  assert.equal(expanded.includes("Reduce sets to 2."), true);
  assert.equal(html.includes("3 sets × 4 reps"), true);
  assert.equal(expanded.includes("90 sec"), true);
});

test("reported D-13 band primer keeps exact dose and one non-duplicated stop rule", async () => {
  const raw = `Left shoulder surface abrasion. No open wound or infection reported. Keep the area clean and covered during training; stop and seek care if bleeding, spreading redness, or increased pain.
- Target weight is not set, so weight-cut guidance is not applied.

D-13 (Monday): Neural speed touch
- Band-Resisted Jab-Cross Primer — 2-3 sets x 6 punches per set (focus on intent and snap), full recovery between sets 90-120 sec, RPE 6-7.
  Easier: do 1-2 sets of unresisted shadow jab-cross at the same tempo.
  Stop: any sharp pain at the left shoulder, wound irritation, or if speed degrades and form breaks.`;

  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    raw_markdown_fallback: raw,
    plan_notes: [
      {
        category: "injury",
        label: "Left shoulder abrasion",
        text: "Left shoulder surface abrasion. No open wound or infection reported. Keep the area clean and covered during training; stop and seek care if bleeding, spreading redness, or increased pain.",
      },
    ],
    red_flag_rules: [
      {
        rule_id: "shoulder-surface",
        severity: "red",
        when: "during_session",
        display_text: "Stop and seek care for bleeding, spreading redness, or increased pain.",
        action: "Seek care if these signs appear.",
      },
    ],
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "TAPER",
        days: [
          {
            date: "2026-08-10",
            countdown_label: "D-13",
            day_type: "low",
            sessions: [
              {
                session_id: "neural-speed",
                session_type: "skill",
                title: "Neural speed touch",
                blocks: [
                  {
                    block_id: "band-primer",
                    block_type: "skill",
                    display_name: "Band-Resisted Jab-Cross Primer",
                    sets: 2,
                    reps: "6 punches per set",
                    effort: { method: "RPE", value: 6.5 },
                    regression_options: [
                      "Do 1-2 sets of unresisted shadow jab-cross at the same tempo.",
                    ],
                    stop_rules: [
                      "Any sharp pain at the left shoulder",
                      "Wound irritation",
                      "If speed degrades and form breaks",
                    ],
                  },
                ],
              },
            ],
          },
        ],
      },
    ],
  } as StructuredPlan;

  const html = await openedPlan(plan, new Date(2026, 7, 10));

  assert.equal(html.includes("2-3 sets × 6 punches"), true);
  assert.equal(html.includes("RPE 6-7"), true);
  assert.equal(html.includes("RPE 6.5"), false);

  assert.equal(countOccurrences(html, 'class="ex-detail-stop"'), 1);
  assert.equal(html.includes("if speed degrades and form breaks"), true);
  // A compound source rule must retain its execution limit and symptoms.
  assert.equal(html.includes("any sharp pain at the left shoulder"), true);
  assert.equal(html.includes(">Wound irritation<"), false);

  // Plan notes are not shown in the plan view; Safety Priority owns escalation.
  assert.equal(html.includes("Keep the area clean and covered during training"), false);
  assert.equal(countOccurrences(html, "stop and seek care if bleeding"), 0);
  assert.equal(countOccurrences(html, "Stop and seek care for bleeding"), 1);
});

test("compound source stop rule stays complete when structured conversion splits it", async () => {
  const raw = `- Saturday — Power and first-step speed
Why: Train fast, short bursts to improve punch speed and reaction.
- Explosive first-step cue: 2 x 5 s maximal first steps from stance. Rest 120 s between reps.
  Cue: Drive the front foot and stop quickly after the step, keep hands ready.
  Progress: Add one more rep to the set when it feels crisp.
  Easier: Do 2 x 3 s instead of 5 s.
  Stop: Loss of control or pain in the arm or knee.`;

  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Speed plan", sport: "boxing", plan_type: "open_block" },
    raw_markdown_fallback: raw,
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        days: [
          {
            date: "2026-09-26",
            weekday: "Sat",
            day_type: "high",
            sessions: [
              {
                session_id: "speed",
                session_type: "strength_power",
                title: "Power and first-step speed",
                blocks: [
                  {
                    block_id: "first-step",
                    block_type: "plyometric_power",
                    display_name: "Explosive first-step cue",
                    sets: 2,
                    reps: "5 s maximal first steps",
                    stop_rules: ["Loss of control", "Joint or arm pain"],
                  },
                ],
              },
            ],
          },
        ],
      },
    ],
  } as StructuredPlan;

  const html = await openedPlan(plan, new Date(2026, 8, 26));

  assert.equal(countOccurrences(html, 'class="ex-detail-stop"'), 1);
  assert.equal(html.includes("Loss of control or pain in the arm or knee."), true);
  assert.equal(html.includes("Loss of control; Joint or arm pain"), false);
});
