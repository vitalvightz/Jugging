import test from "node:test";
import assert from "node:assert/strict";

import {
  buildGuideScript,
  elapsedBefore,
  fightVisualisationFromSession,
  firstNameOf,
  isFightVisualisationSession,
  type FightVisualisation,
} from "./script.ts";
import { crowdLevel, fightLevel } from "./crowd.ts";
import { pickVoice } from "./narrator.ts";
import type { StructuredSession } from "../types.ts";

// The shape the locked merge writes for a D-7 Fight Visualisation.
const d7Session: StructuredSession = {
  session_id: "locked-d-7-fight-visualization",
  session_type: "skill",
  title: "Fight Visualisation",
  objective: "Rehearse building and rebuilding your range against a fighter who wants to close it.",
  mindset_anchor: { confidence_anchor: "Score, move, see again." },
  blocks: [
    {
      block_type: "mindset",
      display_name: "Tactical Picture",
      duration: { value: 8, unit: "minutes" },
      purpose: "Rehearse building and rebuilding your range against a fighter who wants to close it.",
      coaching_cues: [
        "See the opponent starting to shorten the distance.",
        "Score on the way in with your trained lead, then move off line before they set.",
        "Rebuild your range immediately and see the picture clearly again.",
        "Run it again with them entering from the other side.",
        "Cue: Score, move, see again.",
      ],
    },
  ],
};

const d0: FightVisualisation = {
  name: "Trust → Compete",
  why: "Stop conscious over-analysis and hand the fight to your trained reactions.",
  steps: [
    "See the ring and feel your feet underneath you.",
    "See the opponent clearly. One breath.",
    "Bring up your fight cue.",
    "Read what is actually there and trust your reactions.",
  ],
  cue: "See it, trust it, go.",
  preBout: "Twenty seconds: one breath, feet under you, fight cue up, go.",
  durationSec: 3 * 60,
  optional: false,
  blockId: null,
};

test("the locked Fight Visualisation session is recognised by title or id", () => {
  assert.equal(isFightVisualisationSession(d7Session), true);
  assert.equal(isFightVisualisationSession({ session_id: "locked-d-1-fight-visualization" }), true);
  assert.equal(isFightVisualisationSession({ title: "Tactical Focus" }), false);
  assert.equal(isFightVisualisationSession({ title: "Strength & Power" }), false);
});

test("bank instructions become steps; the cue and pre-bout lines are split out", () => {
  const visualisation = fightVisualisationFromSession(d7Session);
  assert.ok(visualisation);
  assert.equal(visualisation.name, "Tactical Picture");
  assert.equal(visualisation.steps.length, 4);
  assert.equal(visualisation.cue, "Score, move, see again.");
  assert.equal(visualisation.preBout, null);
  assert.equal(visualisation.durationSec, 480);

  const fightDay = fightVisualisationFromSession({
    ...d7Session,
    blocks: [
      {
        block_type: "mindset",
        display_name: "Trust → Compete",
        coaching_cues: [...d0.steps, "Cue: See it, trust it, go.", `Pre-bout: ${d0.preBout}`],
      },
    ],
  });
  assert.equal(fightDay?.preBout, d0.preBout);
  assert.equal(fightDay?.steps.length, 4);
});

test("a visualisation session with no steps has nothing to guide", () => {
  assert.equal(
    fightVisualisationFromSession({ ...d7Session, blocks: [{ block_type: "mindset", display_name: "Familiar & Ready" }] }),
    null,
  );
  assert.equal(fightVisualisationFromSession({ title: "Tactical Focus", blocks: d7Session.blocks }), null);
});

test("every bank line is spoken verbatim, in order", () => {
  const visualisation = fightVisualisationFromSession(d7Session)!;
  const spoken = buildGuideScript(visualisation)
    .segments.filter((item) => item.kind === "say")
    .map((item) => item.text)
    .join("\n");
  let cursor = 0;
  for (const step of visualisation.steps) {
    const at = spoken.indexOf(step, cursor);
    assert.ok(at >= cursor, `missing or out of order: ${step}`);
    cursor = at + step.length;
  }
  assert.match(spoken, /Your cue\. Score, move, see again\./);
});

test("every step is followed by silence for the athlete to run it", () => {
  const { segments } = buildGuideScript(fightVisualisationFromSession(d7Session)!);
  segments.forEach((item, index) => {
    if (item.kind === "say" && item.phase === "rehearse") {
      const next = segments[index + 1];
      assert.equal(next?.kind, "hold");
      assert.ok(next.kind === "hold" && next.seconds >= 5);
    }
  });
});

test("the guided run fits the prescribed dose", () => {
  for (const durationSec of [60, 120, 180, 300, 480, 600, 720]) {
    const script = buildGuideScript({ ...d0, durationSec });
    // Short doses have a floor (settle + minimum gaps); longer ones land close.
    const ceiling = Math.max(durationSec * 1.1, 150);
    assert.ok(script.totalSec <= ceiling, `${durationSec}s dose ran ${script.totalSec}s`);
    if (durationSec >= 300) {
      assert.ok(script.totalSec >= durationSec * 0.8, `${durationSec}s dose ran only ${script.totalSec}s`);
    }
  }
});

test("long doses add a real-time run-through; fight day carries the pre-bout line", () => {
  const long = buildGuideScript({ ...d0, durationSec: 480 }).segments;
  assert.ok(long.some((item) => item.kind === "say" && /run it all again/i.test(item.text)));
  const short = buildGuideScript(d0).segments;
  assert.ok(!short.some((item) => item.kind === "say" && /run it all again/i.test(item.text)));
  assert.ok(short.some((item) => item.kind === "say" && item.text.includes("Twenty seconds")));
  assert.equal(short.at(-2)?.kind, "say");
  assert.equal(short.at(-2)?.kind === "say" && short.at(-2)?.phase, "close");
});

test("elapsed time is the sum of the segments before an index", () => {
  const script = buildGuideScript(d0);
  assert.equal(elapsedBefore(script, 0), 0);
  assert.equal(Math.round(elapsedBefore(script, script.segments.length)), script.totalSec);
});

test("the narrator prefers a natural English voice", () => {
  const voice = (name: string, lang: string, localService = true) =>
    ({ name, lang, localService }) as SpeechSynthesisVoice;
  assert.equal(
    pickVoice([voice("Thomas", "fr-FR"), voice("Fred", "en-US"), voice("Daniel (Enhanced)", "en-GB")])?.name,
    "Daniel (Enhanced)",
  );
  assert.equal(pickVoice([voice("Fred", "en-US")])?.name, "Fred");
  assert.equal(pickVoice([voice("Thomas", "fr-FR")]), null);
});

const spokenText = (script: ReturnType<typeof buildGuideScript>) =>
  script.segments.filter((item) => item.kind === "say").map((item) => item.text);

test("the athlete's first name is said first, and once more on the cue", () => {
  const lines = spokenText(buildGuideScript(d0, { firstName: "Alex" }));
  assert.match(lines[0], /^Alex\. /);
  assert.ok(lines.some((line) => line.startsWith("Alex, your cue.")));
  assert.equal(lines.filter((line) => line.includes("Alex")).length, 2);
  assert.ok(!spokenText(buildGuideScript(d0)).some((line) => line.includes("Alex")));
});

test("only a plain first name is spoken", () => {
  assert.equal(firstNameOf("Alex Jones"), "Alex");
  assert.equal(firstNameOf("  Zoë-Ann  O'Neil "), "Zoë-Ann");
  assert.equal(firstNameOf("alex@example.com"), null);
  assert.equal(firstNameOf("x_fighter99"), null);
  assert.equal(firstNameOf(""), null);
  assert.equal(firstNameOf(null), null);
});

test("the script frames perspective, venue and the body before the steps", () => {
  const pro = spokenText(buildGuideScript({ ...d0, durationSec: 480 }, { level: "professional" }));
  assert.ok(pro.some((line) => /through your own eyes/.test(line)));
  assert.ok(pro.some((line) => /Hear the arena/.test(line)));
  assert.ok(pro.some((line) => /heart rate lift\. That's your body getting ready/.test(line)));
  const amateur = spokenText(buildGuideScript({ ...d0, durationSec: 480 }, { level: "amateur" }));
  assert.ok(amateur.some((line) => /Hear the hall/.test(line)));
  // D-1 / D-0 stay calm.
  const calm = spokenText(buildGuideScript(d0));
  assert.ok(calm.some((line) => /then settle\. You've been here before/.test(line)));
});

test("longer doses rehearse a setback and add real-time run-throughs", () => {
  const camp = spokenText(buildGuideScript({ ...d0, durationSec: 720 }));
  assert.ok(camp.some((line) => /goes wrong in the picture, see yourself reset/.test(line)));
  assert.equal(camp.filter((line) => /start to finish/i.test(line)).length, 2);
  const fightDay = spokenText(buildGuideScript({ ...d0, durationSec: 120 }));
  assert.ok(!fightDay.some((line) => /goes wrong/.test(line)));
});

test("the optional camp session is read from the server's flag", () => {
  const camp = fightVisualisationFromSession({
    ...d7Session,
    objective: "Optional. Rehearse building range.",
    optional: true,
    blocks: [{ ...d7Session.blocks![0], purpose: null }],
  });
  assert.equal(camp?.optional, true);
  assert.equal(camp?.why, "Rehearse building range.");
  assert.equal(fightVisualisationFromSession(d7Session)?.optional, false);
});

test("the visualisation names its block, so finishing the run can log it", () => {
  const withId = fightVisualisationFromSession({
    ...d7Session,
    blocks: [{ ...d7Session.blocks![0], block_id: "locked-d-7-tactical-picture" }],
  });
  assert.equal(withId?.blockId, "locked-d-7-tactical-picture");
  assert.equal(
    fightVisualisationFromSession({ ...d7Session, blocks: [{ ...d7Session.blocks![0], block_id: null }] })?.blockId,
    null,
  );
});

test("the crowd sits under the voice and stays out of the settle", () => {
  assert.equal(crowdLevel({ phase: "settle", speaking: false, calm: false }), 0);
  const rehearse = crowdLevel({ phase: "rehearse", speaking: false, calm: false });
  assert.ok(rehearse > 0);
  assert.ok(crowdLevel({ phase: "rehearse", speaking: true, calm: false }) < rehearse);
  assert.ok(crowdLevel({ phase: "rehearse", speaking: false, calm: true }) < rehearse);
  assert.equal(crowdLevel({ phase: "close", speaking: false, calm: false }), 0);
  assert.equal(fightLevel("professional"), "professional");
  assert.equal(fightLevel("Pro"), "professional");
  assert.equal(fightLevel("amateur"), "amateur");
  assert.equal(fightLevel(undefined), "amateur");
});

test("Neural Visualization uses its supplied instructions in the guided player", () => {
  const result = fightVisualisationFromSession({
    session_id: "neural-visualization", title: "Neural Visualization", blocks: [
      { block_type: "mindset", display_name: "Neural Visualization", notes: "Rehearse your first exchange and exit." },
    ],
  });
  assert.deepEqual(result?.steps, ["Rehearse your first exchange and exit."]);
});
