import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { buildStructuredPlanFromText } from "./plan-text-adapter.ts";

const screenshot = readFileSync(new URL("../../tests/fixtures/sc_week5.txt", import.meta.url), "utf8");

test("Week 5 screenshot preserves session, codes, loading rows and unknown units", () => {
  const plan = buildStructuredPlanFromText(screenshot);
  const week = plan.weeks![0];
  const day = week.days![0];
  const session = day.sessions![0];
  const blocks = session.blocks!;
  assert.equal(week.week_index, 5);
  assert.equal(session.title, "Day 1 (Total)");
  assert.equal(day.weekday, null);
  assert.equal(day.date, null);
  assert.equal(day.countdown_label, null);
  assert.deepEqual(blocks.map((block) => block.exercise_code), ["A1", "A2", "B1", "B2", "C1", "C2", "D1"]);
  assert.deepEqual(blocks.map((block) => block.exercise_block), ["A", "A", "B", "B", "C", "C", "D"]);
  assert.ok(blocks.every((block) => block.group_type === null));
  assert.equal(blocks[0].display_name, "Clean");
  assert.equal(blocks[0].prescribed_sets!.length, 8);
  assert.deepEqual(blocks[0].prescribed_sets![0], { percentage: 50, reps: 2, source_text: "WT: 120" });
  assert.deepEqual(blocks[0].prescribed_sets![7], { percentage: 95, sets: 3, reps: 1, source_text: "WT: 225" });
  assert.equal(blocks[2].prescribed_sets!.length, 6);
  assert.deepEqual(blocks[2].prescribed_sets![5], { percentage: 88, sets: 7, reps: 1, source_text: "WT: 295" });
  assert.deepEqual(blocks[4].prescribed_sets, [{ reps: "4e" }, { reps: "4e" }, { reps: "4e" }]);
  assert.deepEqual(blocks[1].prescribed_sets, [{ sets: 3, reps: "25 sec" }]);
  assert.deepEqual(blocks[3].prescribed_sets, [{ sets: 2, reps: "2 Ea" }]);
  assert.equal(JSON.stringify(blocks).includes("kg"), false);
});

test("fight week screenshot uses reps-first header and does not imply supersets", () => {
  const rows = [
    ["A1", "Seated rotations E/S", "8 x 3", "BW", "Low"],
    ["A2", "Banded pull aparts", "15 x 3", "Light", "Medium"],
    ["A3", "Lunge matrix E/S", "3 x 3", "BW", "Medium"],
    ["B1", "Pogo jumps", "5 x 3", "BW", "Max"],
    ["B2", "Medicine ball vertical slam", "5 x 3", "5–7 kg", "Max"],
    ["C1", "Overspeed kettlebell swings", "8 x 3", "16–24 kg", "Max"],
    ["C2", "Ice skaters (continuous)", "10 x 3", "BW", "Max"],
    ["D1", "Isometric punch holds E/S", "6 secs x 3", "BW", "Max"],
    ["D2", "Landmine split jerk E/S", "5 x 3", "5–12 kg", "Max"],
    ["D3", "Landmine rotational catches E/S", "5 x 3", "2.5–7.5 kg", "Max"],
  ];
  const text = "Fight week gym plan for boxing\n| Code | Exercise | REPS X SETS | Weight | Intensity |\n" +
    rows.map((row) => `| ${row.join(" | ")} |`).join("\n");
  const day = buildStructuredPlanFromText(text).weeks![0].days![0];
  assert.equal(day.weekday, null);
  const blocks = day.sessions![0].blocks!;
  assert.equal(blocks.length, 10);
  assert.deepEqual(blocks[0].prescribed_sets, [{ sets: 3, reps: 8, source_text: "Weight: BW · Intensity: Low" }]);
  for (let i = 0; i < rows.length; i++) {
    assert.equal(blocks[i].exercise_code, rows[i][0]);
    assert.equal(blocks[i].display_name, rows[i][1]);
    assert.ok(blocks[i].prescribed_sets![0].source_text!.includes(rows[i][3]));
    assert.equal(blocks[i].group_type, null);
  }
  assert.equal(blocks[7].prescribed_sets![0].reps, "6 secs x 3");
});

test("natural coded rows preserve every dose separator and arbitrary code numbers", () => {
  const day = buildStructuredPlanFromText("Day 2\nZ12. Clean — 50% × 2 — 120\n60% × 1 — 140\nZ13 Pull — 3 x 4").weeks![0].days![0];
  const blocks = day.sessions![0].blocks!;
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].exercise_code, "Z12");
  assert.equal(blocks[0].exercise_block, "Z");
  assert.deepEqual(blocks[0].prescribed_sets, [{ source_text: "50% × 2 — 120" }, { source_text: "60% × 1 — 140" }]);
  assert.equal(day.weekday, null);
});

test("uncoded repeated table rows consolidate only within the session", () => {
  const text = "Week 2\nDay 1\nExercise | Sets x Reps\nDB Incline Bench | 4\nDB Incline Bench | 4\nDB Incline Bench | 4\nDay 2\nExercise | Sets x Reps\nDB Incline Bench | 6";
  const days = buildStructuredPlanFromText(text).weeks![0].days!;
  assert.equal(days.length, 2);
  assert.equal(days[0].sessions![0].blocks!.length, 1);
  assert.deepEqual(days[0].sessions![0].blocks![0].prescribed_sets, [{ reps: 4 }, { reps: 4 }, { reps: 4 }]);
  assert.equal(days[0].sessions![0].blocks![0].exercise_block, null);
  assert.equal(days[1].sessions![0].blocks![0].prescribed_sets!.length, 1);
});

test("explicit group instructions and ordinary uncoded exercises keep working", () => {
  const blocks = buildStructuredPlanFromText("Day 1\nSuperset A\nA1 Squat — 3 x 5\nA2 Pull — 3 x 6\nStraight sets\nB1 Hold — 3 x 25 sec").weeks![0].days![0].sessions![0].blocks!;
  assert.deepEqual(blocks.map((block) => block.group_type), ["superset", "superset", null]);
  for (const heading of ["Circuit", "Alternating sets"]) {
    const block = buildStructuredPlanFromText(`Day 1\n${heading}\nA1 Squat — 3 x 5`).weeks![0].days![0].sessions![0].blocks![0];
    assert.equal(block.group_type, heading.toLowerCase().replace(/ /g, "_"));
  }
  const uncoded = buildStructuredPlanFromText("Week 1\nDay 1\n- Squat — 3 x 5\n- Hold — 25 sec").weeks![0].days![0].sessions![0].blocks!;
  assert.deepEqual(uncoded.map((block) => block.display_name), ["Squat", "Hold"]);
  assert.ok(uncoded.every((block) => !block.exercise_code && !block.exercise_block));
  const uncodedSuperset = buildStructuredPlanFromText("Day 1\nSuperset\n- Squat — 3 x 5\n- Pull — 3 x 6").weeks![0].days![0].sessions![0].blocks!;
  assert.ok(uncodedSuperset.every((block) => block.group_type === "superset"));
  const scopedGroup = buildStructuredPlanFromText("Day 1\nSuperset A\nA1 Squat — 3 x 5\nB1 Pull — 3 x 6").weeks![0].days![0].sessions![0].blocks!;
  assert.deepEqual(scopedGroup.map((block) => block.group_type), ["superset", null]);
});

test("table names may repeat while the code appears only on the first row", () => {
  const text = "Day 1\nCode | Exercise | Sets x Reps\nC1 | DB Incline Bench | 4e\n | DB Incline Bench | 4e\n | DB Incline Bench | 4e";
  const blocks = buildStructuredPlanFromText(text).weeks![0].days![0].sessions![0].blocks!;
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].prescribed_sets!.length, 3);
  assert.equal(blocks[0].exercise_code, "C1");
});

test("markdown coded exercise headings and combined week/day headings stay independent", () => {
  const week = buildStructuredPlanFromText("Day 1 (Total) Week 5\n### A1 — Clean — 50% × 2\n60% × 1").weeks![0];
  assert.equal(week.week_index, 5);
  assert.equal(week.days![0].weekday, null);
  const blocks = week.days![0].sessions![0].blocks!;
  assert.equal(blocks[0].display_name, "Clean");
  assert.equal(blocks[0].prescribed_sets!.length, 2);
});

test("explicit Tues/Thu and Monday/Wed schedules survive alongside coded exercises", () => {
  for (const [a, b, expected] of [["Tues", "Thu", ["Tue", "Thu"]], ["Monday", "Wed", ["Mon", "Wed"]]] as const) {
    const days = buildStructuredPlanFromText(`Week 5\n${a} — Strength\nA1 Clean — 3 x 1\n${b} — Strength\nB1 Squat — 3 x 5`).weeks![0].days!;
    assert.deepEqual(days.map((day) => day.weekday), expected);
  }
});

test("untagged blocks are typed as plain exercises: their names never pick a type", () => {
  const plan = buildStructuredPlanFromText(screenshot);
  const blocks = plan.weeks![0].days![0].sessions![0].blocks!;
  // "Clean", jumps and throws used to be read as strength_power or conditioning.
  for (const block of blocks) assert.equal(block.block_type, "exercise", block.display_name ?? "");
  assert.equal(blocks[0].display_name, "Clean");
});
