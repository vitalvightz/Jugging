import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import { TodayDecisionPanel } from "./today-decision-panel";
import type { TodayDecisionBanner } from "@/lib/today";

const BANNER: TodayDecisionBanner = {
  state: "modify",
  displayState: "adjust",
  chip: "ADJUST",
  title: "Session reduced",
  detail: "Poor sleep means your body has less room to recover today.",
  action: "Cut 1 round and do not add extra conditioning.",
  tone: "amber",
};

const STOP_BANNER: TodayDecisionBanner = {
  state: "not_checked_in",
  displayState: "stop",
  chip: "STOP",
  title: "Stop today",
  detail: "A safety restriction is blocking training today.",
  action: "Do not start today's planned session. Follow the injury and safety guidance below.",
  tone: "red",
};

/** React escapes apostrophes in static markup, so compare against plain text. */
function render(props: Parameters<typeof TodayDecisionPanel>[0]): string {
  return renderToStaticMarkup(<TodayDecisionPanel {...props} />).replace(/&#x27;/g, "'");
}

test("the action reads before the reason", () => {
  // An athlete in a gym scans for what to do first. If the reason lands above the
  // instruction, the instruction is what gets skipped.
  const html = render({ banner: BANNER });
  assert.ok(html.indexOf(BANNER.action!) < html.indexOf(BANNER.detail));
});

test("a live session uses the same current-day card styling as post-session guidance", () => {
  const live = render({ banner: BANNER, tier: "modify", triggers: ["Poor sleep"] });
  const afterSession = render({
    banner: { ...BANNER, action: undefined },
    tier: "modify",
    triggers: ["Poor sleep"],
  });

  for (const html of [live, afterSession]) {
    assert.match(html, /data-current-guidance="true"/);
    assert.match(html, /TODAY'S GUIDANCE/);
    assert.match(html, /Why this decision/);
    assert.match(html, /Poor sleep/);
  }
  assert.match(live, /Cut 1 round/);
});

test("green guidance keeps the same compact shell when a session is live", () => {
  const html = render({
    banner: {
      ...BANNER,
      displayState: "go",
      chip: "GO",
      tone: "green",
      action: "Start session and keep the work clean.",
    },
    tier: "green",
    sources: ["today's check-in"],
  });

  assert.match(html, /data-current-guidance="true"/);
  assert.match(html, /TODAY'S GUIDANCE/);
  assert.match(html, /Start session and keep the work clean/);
  assert.ok(!/<details[^>]*\sopen/.test(html));
});

test("an adjusted day keeps its evidence in a closed native disclosure with separate trigger and context", () => {
  const html = render({
    banner: BANNER,
    triggers: ["Poor sleep for 3 days", "Feeling flat"],
    context: ["Fight week / taper", "Hard session planned"],
  });
  assert.ok(html.includes("Trigger"));
  assert.ok(html.includes("Poor sleep for 3 days"));
  assert.ok(html.includes("Feeling flat"));
  assert.ok(html.includes("Context"));
  assert.ok(html.includes("<details"));
  assert.ok(html.includes("<summary>Why this decision?</summary>"));
  assert.ok(!/<details[^>]*\sopen/.test(html));
  assert.equal((html.match(/today-decision-values/g) ?? []).length, 2);
  assert.ok(html.includes('data-evidence-count="2"'));
  assert.ok(!html.includes(" · "));
});

test("dense evidence uses one command region and a three-group evidence rail", () => {
  const html = render({
    banner: BANNER,
    triggers: ["Poor sleep", "Feeling flat"],
    context: ["Fight week"],
    sources: ["today's check-in", "today's planned session"],
  });

  assert.equal((html.match(/today-decision-command/g) ?? []).length, 1);
  assert.equal((html.match(/today-decision-evidence/g) ?? []).length, 1);
  assert.ok(html.includes('data-evidence-count="3"'));
});

test("context never renders inside the trigger row", () => {
  // The whole point of the split: a flat list made "Fight week" a peer of
  // "High pain", which reads as though being close to a fight were a symptom.
  const html = render({
    banner: BANNER,
    triggers: ["Poor sleep"],
    context: ["Fight week"],
  });
  const triggerRow = html.slice(html.indexOf("Trigger"), html.indexOf("Context"));
  assert.ok(triggerRow.includes("Poor sleep"));
  assert.ok(!triggerRow.includes("Fight week"));
});

test("a row with nothing in it does not render", () => {
  const html = render({ banner: BANNER, triggers: ["Poor sleep"] });
  assert.ok(html.includes("Trigger"));
  assert.ok(!html.includes("Context"));
});

test("empty evidence does not render a disclosure", () => {
  const html = render({ banner: BANNER });
  assert.ok(!html.includes("<details"));
  assert.ok(!html.includes("Why this decision?"));
});

test("blank labels never render an empty row", () => {
  const html = render({ banner: BANNER, triggers: ["", "   "], context: [""] });
  assert.ok(!html.includes("Trigger"));
  assert.ok(!html.includes("Context"));
});

test("decision based on always lists the available sources", () => {
  const html = render({
    banner: BANNER,
    sources: ["today's check-in", "your recent sessions"],
  });
  assert.ok(html.includes("Decision based on"));
  assert.ok(html.includes("today-decision-inputs"));
  assert.ok(html.includes("today's check-in"));
  assert.ok(html.includes("your recent sessions"));
  assert.ok(!html.includes("Confidence"));
});

test("preview evidence names only the next session and drops today's readiness context", () => {
  const html = render({
    banner: {
      ...BANNER,
      displayState: "preview",
      chip: "PREVIEW",
      detail: "Mobility Reset is next on your plan.",
      action: "Review the mobility and recovery work before it opens.",
      tone: "neutral",
    },
    triggers: ["Pain reported today"],
    context: ["Taper phase"],
    sources: ["today's check-in", "today's planned session"],
    confidenceNote: "Less to go on today.",
  });

  assert.ok(html.includes("next planned session"));
  assert.ok(!html.includes("Pain reported today"));
  assert.ok(!html.includes("Taper phase"));
  assert.ok(!html.includes("today's check-in"));
  assert.ok(!html.includes("today's planned session"));
  assert.ok(!html.includes("Less to go on today"));
});

test("preview above a named session keeps its instruction without repeating the session title", () => {
  const html = render({
    banner: {
      ...BANNER,
      displayState: "preview",
      chip: "PREVIEW",
      detail: "Mobility Reset is next on your plan.",
      action: "Review the mobility and recovery work before it opens.",
      tone: "neutral",
    },
    compactPreview: true,
  });

  assert.match(html, /Review the mobility and recovery work before it opens/);
  assert.doesNotMatch(html, /Mobility Reset is next on your plan|next planned session/);
  assert.match(html, /data-compact="true"/);
});

test("today's guidance retains injury evidence while a future session is previewed", () => {
  const html = render({
    banner: { ...STOP_BANNER, action: undefined },
    tier: "stop",
    triggers: ["Active severe injury: Knee"],
    safetyChecks: [{
      code: "surface_injury",
      label: "Skin injury",
      result: "medical_review",
      result_label: "Medical review needed",
    }],
    sources: ["your tracked injuries"],
  });

  assert.match(html, /TODAY'S GUIDANCE/);
  assert.match(html, /Active severe injury: Knee/);
  assert.match(html, /Skin injury: Medical review needed/);
  assert.match(html, /your tracked injuries/);
  assert.match(html, /<details[^>]*\sopen/);
  assert.doesNotMatch(html, /Do not start today's planned session/);
});

test("a clear check-in keeps its explanation reachable without a future-session command", () => {
  const html = render({
    banner: { ...BANNER, displayState: "go", chip: "GO", tone: "green", action: undefined },
    triggers: ["Good sleep"],
    sources: ["today's check-in"],
  });

  assert.match(html, /TODAY'S GUIDANCE/);
  assert.match(html, /Why this decision/);
  assert.match(html, /Good sleep/);
  assert.doesNotMatch(html, /Cut 1 round/);
  assert.ok(!/<details[^>]*\sopen/.test(html));
});

test("the status chip is the only decision-state label", () => {
  const html = render({ banner: STOP_BANNER, tier: "stop" });

  assert.ok(html.includes('data-state="stop"'));
  assert.ok(html.includes('data-tone="red"'));
  assert.ok(html.includes(">STOP<"));
  assert.ok(!html.includes("today-decision-title"));
  assert.ok(!html.includes(">Stop today<"));
  assert.ok(html.includes(STOP_BANNER.action!));
  assert.ok(html.includes(STOP_BANNER.detail));
  assert.ok(!html.includes("PULL BACK"));
});

test("a missing-data note renders beneath the available sources", () => {
  const html = render({
    banner: BANNER,
    sources: ["today's check-in"],
    confidenceNote: "Less to go on today: today's session isn't resolved yet.",
  });
  assert.ok(html.includes("Decision based on"));
  assert.ok(html.includes("today's session isn't resolved yet"));
  assert.ok(html.indexOf("today-decision-inputs") < html.indexOf("today-decision-gap"));
});

test("a missing-data note still renders when no source was available", () => {
  const html = render({
    banner: BANNER,
    confidenceNote: "Safety history is unavailable.",
  });
  assert.ok(html.includes("Decision based on"));
  assert.ok(html.includes("Safety history is unavailable."));
});

test("confidence bands are never rendered", () => {
  const html = render({ banner: BANNER, sources: ["today's check-in"] });
  for (const band of ["Confidence", "High", "Moderate", "Low", "data-band"]) {
    assert.ok(!html.includes(band), `must not render confidence band: ${band}`);
  }
});

test("the panel stays null before check-in even with an explanation supplied", () => {
  const html = render({ banner: null, triggers: ["Poor sleep"] });
  assert.equal(html, "");
});

test("the command hides only the superseded-readiness presentation message", () => {
  const superseded = render({
    banner: {
      ...STOP_BANNER,
      safety: " Previous readiness guidance is superseded by the injury warning. ",
    },
  });
  assert.ok(!superseded.includes("Previous readiness guidance"));
  assert.ok(!superseded.includes("today-decision-safety"));

  for (const safety of [
    "Seek medical advice.",
    "Stop if it opens or bleeds.",
    "Monitor worsening symptoms.",
    "Keep the wound clean and covered.",
  ]) {
    const html = render({ banner: { ...STOP_BANNER, safety } });
    assert.ok(html.includes(safety));
    assert.ok(html.includes("today-decision-safety"));
  }
});

test("the explanation never claims a signal caused the change", () => {
  const html = render({
    banner: BANNER,
    triggers: ["Poor sleep"],
    context: ["Fight week"],
    sources: ["today's check-in"],
  }).toLowerCase();
  for (const causal of ["caused", "because of", "due to"]) {
    assert.ok(!html.includes(causal), `explanation must not claim cause: ${causal}`);
  }
});

test("nothing on the card calls context a warning", () => {
  const html = render({
    banner: BANNER,
    triggers: ["Poor sleep"],
    context: ["Fight week / taper"],
    confidenceNote: "Less to go on today: today's session isn't resolved yet.",
  }).toLowerCase();
  assert.ok(!html.includes("warning"));
});

test("a cleared safety check renders apart from the triggers, never as one", () => {
  // A stable blister was assessed and changed nothing. Showing it under Trigger
  // would tell the athlete their skin injury reduced the session.
  const html = render({
    banner: BANNER,
    triggers: ["Poor sleep"],
    safetyChecks: [
      {
        code: "surface_injury",
        label: "Skin injury",
        result: "no_session_change",
        result_label: "No session change",
      },
    ],
  });
  const checkedIndex = html.indexOf("Checked");
  assert.ok(checkedIndex > -1);
  assert.ok(html.includes("Skin injury: No session change"));
  // The trigger list holds only the cause.
  assert.ok(html.indexOf("Poor sleep") < checkedIndex);
  assert.ok(html.includes('data-evidence-count="2"'));
});

test("safety checks are omitted on a preview card", () => {
  const html = render({
    banner: BANNER,
    tier: "preview",
    safetyChecks: [
      {
        code: "surface_injury",
        label: "Skin injury",
        result: "no_contact",
        result_label: "No contact today",
      },
    ],
  });
  assert.ok(!html.includes("Skin injury"));
  assert.ok(html.includes("<details"));
  assert.ok(!/<details[^>]*\sopen/.test(html));
});

test("a current skin-care message stays authoritative over preview timing", () => {
  const html = render({
    banner: {
      state: "train_as_planned",
      displayState: "safety_notice",
      chip: "SKIN CARE",
      title: "Skin care active",
      action: "Keep the left eyebrow cut clean and covered.",
      detail: "Stop if it opens, reopens, bleeds, or rubs.",
      tone: "amber",
    },
    tier: "preview",
    triggers: ["Poor sleep"],
    context: ["Taper phase"],
    sources: ["next planned session"],
    safetyChecks: [
      {
        code: "surface_injury",
        label: "Skin injury",
        result: "no_session_change",
        result_label: "No session change",
      },
    ],
  });

  assert.ok(html.includes(">SKIN CARE<"));
  assert.ok(html.includes("Keep the left eyebrow cut clean and covered."));
  assert.ok(html.includes("Why this message?"));
  assert.ok(/<details[^>]*\sopen/.test(html));
  assert.ok(html.includes("Message based on"));
  assert.ok(html.includes("your tracked injuries"));
  assert.ok(html.includes("Skin injury"));
  assert.ok(html.includes("No session change"));
  assert.ok(!html.includes("Poor sleep"));
  assert.ok(!html.includes("Taper phase"));
  assert.ok(!html.includes("next planned session"));
});

test("a short lead before a colon becomes the title over the specifics", () => {
  const html = render({
    banner: { ...BANNER, action: "Keep it controlled: skip sparring, clinch pressure, and all-out rounds." },
  });
  assert.match(
    html,
    /<p class="today-decision-action">Keep it controlled\.<span class="today-decision-action-rest">Skip sparring, clinch pressure, and all-out rounds\.<\/span><\/p>/,
  );
  const plain = render({ banner: BANNER });
  assert.match(plain, /<p class="today-decision-action">Cut 1 round and do not add extra conditioning\.<\/p>/);
});

test("a trigger the reason line already names is not repeated under Why", () => {
  const html = render({
    banner: { ...BANNER, detail: "Your right achilles tendonitis limits hard combat work today." },
    triggers: ["Right achilles tendonitis — restricts today's training", "Poor sleep"],
    sources: ["today's check-in"],
  });
  assert.equal(html.match(/achilles tendonitis/gi)?.length, 1);
  assert.match(html, /Poor sleep/);
});

test("a trigger never shows internal injury markers", () => {
  const html = render({ banner: STOP_BANNER, tier: "stop", triggers: ["Bruise [training_impact:not_limiting] — getting worse"] });
  assert.match(html, /Bruise — getting worse/);
  assert.doesNotMatch(html, /training_impact|not_limiting|\[/);
});

for (const triggers of [undefined, [], ["Active injury"], ["Poor sleep"],
  ["Active injury", "Recent hard session", "Injury getting worse"], ["Low readiness lately"]]) {
  test(`contributors never prove sleep or body clearance: ${JSON.stringify(triggers)}`, () => {
    const html = render({ banner: BANNER, triggers, sources: ["today's check-in"] });
    assert.doesNotMatch(html, /Sleep:|Body and pain:|No concerns reported|No new concerns reported|data-result="clear"/);
  });
}

test("missing and incomplete structured checks cannot claim clearance", () => {
  for (const safetyChecks of [undefined, [], [
    { code: "surface_injury", label: "Skin injury", result_label: "No session change" },
    { code: "", label: "Skin injury", result: "no_session_change", result_label: "No session change" },
    { code: "surface_injury", label: "Skin injury", result: "no_session_change", result_label: "" },
    { code: "surface_injury", label: "Skin injury", result: "unknown", result_label: "No session change" },
  ]]) {
    const html = render({ banner: BANNER, sources: ["today's check-in"],
      safetyChecks: safetyChecks as Parameters<typeof TodayDecisionPanel>[0]["safetyChecks"] });
    assert.doesNotMatch(html, /data-result="clear"|Skin injury:/);
  }
});

test("conflicting safety evidence keeps the restriction and omits the clearance in either order", () => {
  const clear = { code: "surface_injury", label: "Skin injury", result: "no_session_change" as const, result_label: "No session change" };
  const restricted = { ...clear, result: "no_contact" as const, result_label: "No contact today" };
  for (const safetyChecks of [[clear, restricted], [restricted, clear], [clear, clear, restricted, restricted]]) {
    const html = render({ banner: BANNER, safetyChecks, triggers: ["Poor sleep"], sources: ["today's check-in"] });
    assert.doesNotMatch(html, /data-result="clear"|No session change|Sleep:|Body and pain:/);
    assert.equal(html.match(/Skin injury: No contact today/g)?.length, 1);
  }
});

test("explicit structured clearance is independent of capped unrelated contributors", () => {
  const html = render({ banner: BANNER,
    triggers: ["Poor sleep", "High pain", "Recent hard session"],
    safetyChecks: [{ code: "surface_injury", label: "Skin injury", result: "no_session_change", result_label: "No session change" }],
  });
  assert.match(html, /data-result="clear">Skin injury: No session change/);
  assert.match(html, /Poor sleep/);
  assert.match(html, /High pain/);
  assert.doesNotMatch(html, /Sleep:|Body and pain:/);
});

test("safety checks carry their outcome marker", () => {
  const html = render({
    banner: BANNER,
    safetyChecks: [
      { code: "surface_injury", label: "Skin injury", result: "no_session_change", result_label: "No change to session" },
      { code: "surface_contact", label: "Open wound", result: "no_contact", result_label: "Contact removed" },
    ],
  });

  assert.match(html, /<li data-result="clear">Skin injury: No change to session<\/li>/);
  assert.match(html, /<li data-result="flag">Open wound: Contact removed<\/li>/);
});


for (const displayState of ["adjust", "pull_back"] as const) {
  test(`${displayState} keeps named restrictions and distinct warnings without generic duplication`, () => {
    const html = render({ banner: { ...BANNER, displayState }, triggers: [
      "Left shoulder instability — restricts today's training", "Current injury restrictions", "High pain",
    ] });
    assert.match(html, /Left shoulder instability/);
    assert.match(html, /High pain/);
    assert.doesNotMatch(html, /Current injury restrictions/);
    assert.match(render({ banner: { ...BANNER, displayState }, triggers: ["Current injury restrictions"] }), /Current injury restrictions/);
  });
}


test("repeated guidance is shown once while distinct safety instructions remain", () => {
  const html = render({ banner: { ...BANNER, action: "Avoid contact.", detail: "Avoid contact.", safety: "Keep the wound covered." } });
  assert.equal((html.match(/Avoid contact\./g) ?? []).length, 1);
  assert.match(html, /Keep the wound covered/);
  const repeated = render({ banner: { ...BANNER, action: "Keep it covered.", detail: "Different reason.", safety: "Keep it covered." } });
  assert.equal((repeated.match(/Keep it covered\./g) ?? []).length, 1);
  assert.match(repeated, /Different reason/);
});
