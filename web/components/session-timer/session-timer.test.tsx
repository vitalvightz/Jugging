import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import { SessionTimer } from "./session-timer";
import { blockToTimerItem } from "@/lib/session-timer/plan";
import type { TimerItem } from "@/lib/session-timer/plan";

const ITEMS: TimerItem[] = [
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
    kind: "sets",
    id: "squat",
    title: "Back squat",
    detail: "5 reps · RPE 7",
    blockType: "strength",
    sets: { min: 3, max: 5 },
    holdSec: null,
    restSec: { min: 90, max: 120 },
  },
];

function render(items: TimerItem[], visible = true): string {
  return renderToStaticMarkup(
    <SessionTimer
      items={items}
      storageKey="test-run"
      sessionTitle="Fight-pace day"
      visible={visible}
      onMinimize={() => undefined}
      onExpand={() => undefined}
      onFinish={() => undefined}
      onClose={() => undefined}
    />,
  );
}

test("the ready screen shows the first exercise, its adjustable rounds and what is next", () => {
  const html = render(ITEMS);
  assert.match(html, /role="dialog"/);
  assert.match(html, /Up next/);
  assert.match(html, /Hard sparring/);
  assert.match(html, /Start round 1/);
  // The count sits beside the phase; one summary line; the steppers live behind the Adjust icon.
  assert.match(html, /<span class="st-phase">Up next<\/span><p class="st-board-format">6 rounds<\/p>/);
  assert.match(html, /<span>3:00<\/span> work · <span>1:00<\/span> rest/);
  assert.match(html, /aria-label="Adjust timer"/);
  assert.doesNotMatch(html, /aria-label="Increase rounds"/);
  assert.match(html, /Next<\/span><span class="st-next-title">Back squat<\/span>/);
  assert.match(html, /3–5 sets · 90s–2 min rest/);
  assert.doesNotMatch(html, /Pick a format/);
});

test("rounds with no length in the plan ask the athlete to check them", () => {
  const first = ITEMS[0];
  assert.ok(first.kind === "interval");
  const html = render([{ ...first, needsSetup: true }]);
  assert.match(html, /Round length isn&#x27;t set in your plan\. Pick a format\./);
  // The athlete's own gym work is never framed as missing from their plan.
  const gym = render([{ ...first, needsSetup: true, setupNote: "Match your gym's rounds. Pick a format." }]);
  assert.match(gym, /Match your gym&#x27;s rounds\. Pick a format\./);
  assert.doesNotMatch(gym, /Not in your plan|isn&#x27;t set in your plan/);
});

test("a minimised timer on its ready screen renders the mini bar", () => {
  const html = render(ITEMS, false);
  assert.match(html, /class="st-mini"/);
  assert.match(html, /Hard sparring/);
  assert.doesNotMatch(html, /role="dialog"/);
});

test("the last exercise has no button that ends the session except the end link", () => {
  const last = ITEMS[1];
  // A single-exercise run is always on its last exercise.
  const html = render([last]);
  assert.doesNotMatch(html, />Finish</);
  assert.doesNotMatch(html, /Finish exercise/);
  assert.doesNotMatch(html, /Next exercise/);
  // Unstarted, that link only closes the timer.
  assert.match(html, />Close timer</);
});

test("Next exercise only appears when there is a next exercise", () => {
  assert.match(render(ITEMS), /Next exercise/);
});

test("an unstarted timer offers Close timer instead of ending a session", () => {
  const html = render(ITEMS);
  assert.match(html, />Close timer</);
  assert.doesNotMatch(html, />End session</);
});

test("the plan summary opens the adjust sheet and the first-run hint waits for storage", () => {
  const html = render(ITEMS);
  assert.match(html, /class="st-plan-summary"[^>]*aria-label="Edit rounds and timing"/);
  // Hidden on the server so it never flashes for athletes who have seen it.
  assert.doesNotMatch(html, /st-hint/);
});

const SHUTTLE: TimerItem = {
  kind: "sets",
  id: "shuttle",
  title: "Shuttle sprints",
  detail: "200 m · RPE 8",
  stats: [
    { kind: "target", value: "200 m" },
    { kind: "effort", value: "RPE 8" },
  ],
  blockType: "conditioning",
  sets: { min: 6, max: 6 },
  holdSec: null,
  restSec: { min: 90, max: 90 },
  unit: "round",
};

test("the prescription separates distance and effort labels without losing their units", () => {
  const html = render([SHUTTLE, ITEMS[0]]);
  assert.match(html, /<dl class="st-stats" aria-label="Prescription"><div data-kind="target"><dt>Distance<\/dt><dd>200 m<\/dd><\/div><div data-kind="effort"><dt>RPE<\/dt><dd>8<\/dd><\/div><\/dl>/);
  assert.match(html, /aria-label="Edit rounds and rest"><span>6<\/span>\u00a0rounds · <span>1:30<\/span>\u00a0rest/);
  // What comes after carries its own dose too.
  const before = render([ITEMS[0], SHUTTLE]);
  assert.match(before, /st-next-plan">6 × 200 m · 90s rest</);
});

test("prescription stats preserve rep ranges, percentage loads and RIR", () => {
  const html = render([{ ...SHUTTLE, stats: [
    { kind: "target", value: "5–8 reps" },
    { kind: "load", value: "80% 1RM" },
    { kind: "effort", value: "RIR 3" },
  ] }]);
  assert.match(html, /<dt>Reps<\/dt><dd>5–8<\/dd>/);
  assert.match(html, /<dt>Load<\/dt><dd>80% 1RM<\/dd>/);
  assert.match(html, /<dt>RIR<\/dt><dd>3<\/dd>/);
});

test("a run saved before stats existed still shows its one-line detail", () => {
  const html = render([ITEMS[1]]);
  assert.match(html, /<p class="st-detail">5 reps · RPE 7<\/p>/);
  assert.doesNotMatch(html, /st-stats/);
});

test("a single timed block reads as its length, not 1 × length", () => {
  const html = render([
    { kind: "interval", id: "bike", title: "Bike", detail: null, stats: [], blockType: "conditioning", rounds: 1, workSec: 1200, restSec: 0, sparring: false, needsSetup: false },
  ]);
  assert.match(html, /aria-label="Edit rounds and timing"><span>20:00<\/span> work<\/button>/);
  assert.doesNotMatch(html, /st-board-format/);
});

test("the next-exercise line keeps every per-set target, not just the first", () => {
  const amrap: TimerItem = {
    kind: "sets",
    id: "pushups",
    title: "Push-ups",
    detail: "AMRAP · 60 s",
    stats: [
      { kind: "target", value: "AMRAP" },
      { kind: "target", value: "60 s" },
    ],
    blockType: "strength",
    sets: { min: 3, max: 3 },
    holdSec: null,
    restSec: { min: 90, max: 90 },
  };
  assert.match(render([ITEMS[0], amrap]), /st-next-plan">3 × AMRAP, 60 s · 90s rest</);
  const drill: TimerItem = {
    ...amrap,
    id: "drill",
    title: "Shadow drill",
    stats: [
      { kind: "target", value: "10 reps" },
      { kind: "target", value: "5 min" },
      { kind: "effort", value: "RPE 6" },
    ],
    sets: { min: 2, max: 2 },
    restSec: null,
  };
  assert.match(render([ITEMS[0], drill]), /st-next-plan">2 × 10 reps, 5 min</);
});


test("cluster ready screen shows both movements without combat format setup", () => {
  const item = blockToTimerItem({
    display_name: "Heavy RDL → Broad Jump", block_type: "strength", rounds: 3,
    rest: { value: 4, unit: "min" },
    components: [{ name: "Heavy RDL", reps: 2, load: { display: "85% 1RM" } }, { name: "Broad Jump", reps: 2 }],
  }, 0)!;
  const html = render([item]);
  assert.match(html, /Movements each round/);
  assert.match(html, /2 reps · 85% 1RM/);
  assert.match(html, /<strong>Broad Jump<\/strong>/);
  assert.match(html, /class="st-primary">Start</);
  assert.match(html, /4:00/);
  assert.doesNotMatch(html, /Pick a format|Boxing 3\/1|3:00/);
});

test("a timed set reads as its time, never as a hold, whatever it is called", () => {
  const item = blockToTimerItem({ block_type: "rehab", display_name: "Copenhagen plank hold", sets: 3, reps: 1,
    duration: { value: 20, unit: "seconds" }, rest: { value: 45, unit: "seconds" } }, 0)!;
  const html = render([item]);
  assert.match(html, /Copenhagen plank hold/);
  assert.match(html, /<span>0:20<\/span>\u00a0each/);
  assert.doesNotMatch(html, /\u00a0hold|End hold|>Hold<|>Lift</);
});
