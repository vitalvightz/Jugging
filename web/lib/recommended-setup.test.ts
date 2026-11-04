import test from "node:test";
import assert from "node:assert/strict";

import { buildDaysOutContext } from "@/lib/days-out-policy";
import {
  EQUIPMENT_ACCESS_OPTIONS,
  EQUIPMENT_ACCESS_GROUPS,
  KEY_GOAL_OPTIONS,
  TRAINING_AVAILABILITY_OPTIONS,
  WEAK_AREA_OPTIONS,
} from "@/lib/intake-options";
import { QUICK_BUILD_KEY_GOAL_CAP, QUICK_BUILD_WEAK_AREA_CAP } from "@/lib/quick-build";
import { getPerformanceFocusCap } from "@/lib/performance-focus-cap";
import {
  EQUIPMENT_PRESETS,
  FOCUS_PRESETS,
  TRAINING_PRESETS,
  deriveSetupSource,
  getAvailableFocusPresets,
  matchesEquipmentPreset,
  matchesFocusPreset,
  matchesTrainingPreset,
  resolveFocusPresetSelections,
  type FocusPresetSelectionLimits,
} from "@/lib/recommended-setup";

function defaultFocusLimits(overrides: Partial<FocusPresetSelectionLimits> = {}): FocusPresetSelectionLimits {
  return {
    goalLimit: QUICK_BUILD_KEY_GOAL_CAP,
    weakAreaLimit: QUICK_BUILD_WEAK_AREA_CAP,
    sharedLimit: null,
    daysOutCtx: buildDaysOutContext(null),
    ...overrides,
  };
}

function fightDateInDays(days: number, now: Date): string {
  const target = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  target.setUTCDate(target.getUTCDate() + days);
  const y = target.getUTCFullYear();
  const m = String(target.getUTCMonth() + 1).padStart(2, "0");
  const d = String(target.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

test("every equipment preset value exists in EQUIPMENT_ACCESS_OPTIONS", () => {
  const known = new Set(EQUIPMENT_ACCESS_OPTIONS.map((option) => option.value));
  for (const preset of EQUIPMENT_PRESETS) {
    for (const value of preset.equipment_access) {
      assert.ok(known.has(value), `${preset.key} -> "${value}" missing from EQUIPMENT_ACCESS_OPTIONS`);
    }
  }
});

test("equipment groups contain every supported option exactly once", () => {
  const groupedValues = EQUIPMENT_ACCESS_GROUPS.flatMap((group) => group.options.map((option) => option.value));
  const expectedGroups = {
    "Weights & strength": ["dumbbells", "kettlebells", "barbell", "trap_bar", "plate", "bench", "back_extension_bench", "cable", "landmine", "log", "atlas_stone"],
    Conditioning: ["assault_bike", "rower", "treadmill", "step_mill", "pool", "sled", "battle_ropes", "tire", "sledgehammer", "weight_vest"],
    Combat: ["heavy_bag", "pads", "partner"],
    "Plyometrics & movement": ["box", "agility_ladder", "hurdles", "jump_rope"],
    "Functional & accessories": ["medicine_ball", "sandbag", "bulgarian_bag", "bands", "trx", "pullup_bar", "swiss_ball", "bosu_ball", "neck_harness", "weight_belt", "water_jug"],
    Recovery: ["foam_roller", "towel", "stable_support", "table", "chair", "wall", "dowel", "cloth", "soft_ball"],
  };

  assert.deepEqual(
    Object.fromEntries(EQUIPMENT_ACCESS_GROUPS.map((group) => [group.label, group.options.map((option) => option.value)])),
    expectedGroups,
  );
  assert.equal(new Set(groupedValues).size, groupedValues.length, "equipment values must not appear in two groups");
  assert.deepEqual(EQUIPMENT_ACCESS_OPTIONS.map((option) => option.value), groupedValues);
  assert.ok(EQUIPMENT_ACCESS_GROUPS.every((group) => group.label && group.options.length > 0));
});

test("specialist exercise-bank equipment can be declared at intake", () => {
  const known = new Set(EQUIPMENT_ACCESS_OPTIONS.map((option) => option.value));
  const specialistEquipment = [
    "agility_ladder",
    "atlas_stone",
    "bench",
    "box",
    "bosu_ball",
    "bulgarian_bag",
    "foam_roller",
    "log",
    "neck_harness",
    "plate",
    "sledgehammer",
    "step_mill",
    "swiss_ball",
    "tire",
    "towel",
    "treadmill",
    "trx",
    "water_jug",
    "weight_vest",
  ];

  for (const value of specialistEquipment) {
    assert.ok(known.has(value), `"${value}" missing from EQUIPMENT_ACCESS_OPTIONS`);
  }
});

test("every training preset day exists in TRAINING_AVAILABILITY_OPTIONS", () => {
  const known = new Set(TRAINING_AVAILABILITY_OPTIONS.map((option) => option.value));
  for (const preset of TRAINING_PRESETS) {
    for (const value of preset.training_availability) {
      assert.ok(known.has(value), `${preset.key} -> "${value}" missing from TRAINING_AVAILABILITY_OPTIONS`);
    }
  }
});

test("training preset frequency matches its training-day count", () => {
  for (const preset of TRAINING_PRESETS) {
    assert.equal(
      preset.weekly_training_frequency,
      preset.training_availability.length,
      `${preset.key} frequency must equal day count`,
    );
  }
});

test("equipment presets round-trip through matchesEquipmentPreset", () => {
  for (const preset of EQUIPMENT_PRESETS) {
    assert.equal(matchesEquipmentPreset(preset.equipment_access), preset.key);
    const shuffled = [...preset.equipment_access].reverse();
    assert.equal(matchesEquipmentPreset(shuffled), preset.key);
  }
});

test("training presets round-trip through matchesTrainingPreset", () => {
  for (const preset of TRAINING_PRESETS) {
    assert.equal(
      matchesTrainingPreset(preset.training_availability, preset.weekly_training_frequency),
      preset.key,
    );
  }
});

test("matchesEquipmentPreset returns null for non-preset selections", () => {
  assert.equal(matchesEquipmentPreset([]), null);
  assert.equal(matchesEquipmentPreset(["dumbbells"]), null);
});

test("matchesTrainingPreset returns null when frequency differs from preset", () => {
  const threeDay = TRAINING_PRESETS.find((preset) => preset.key === "three_days")!;
  assert.equal(matchesTrainingPreset(threeDay.training_availability, 2), null);
});

test("deriveSetupSource maps preset hits to source label", () => {
  assert.equal(deriveSetupSource(["home", "three_days"]), "preset");
  assert.equal(deriveSetupSource(["home", null]), "mixed");
  assert.equal(deriveSetupSource([null, null]), "manual");
  assert.equal(deriveSetupSource([]), "manual");
});

test("every focus preset only uses existing goal and weak-area options", () => {
  const knownGoals = new Set(KEY_GOAL_OPTIONS.map((option) => option.value));
  const knownWeakAreas = new Set(WEAK_AREA_OPTIONS.map((option) => option.value));
  for (const preset of FOCUS_PRESETS) {
    for (const value of preset.goals) {
      assert.ok(knownGoals.has(value), `${preset.key} -> "${value}" missing from KEY_GOAL_OPTIONS`);
    }
    for (const value of preset.weak_areas) {
      assert.ok(knownWeakAreas.has(value), `${preset.key} -> "${value}" missing from WEAK_AREA_OPTIONS`);
    }
  }
});

test("focus presets respect current individual and shared caps", () => {
  const now = new Date(Date.UTC(2026, 4, 17));
  for (const days of [0, 7, 14, 21, 42, 56, 90]) {
    const cap = getPerformanceFocusCap(fightDateInDays(days, now), { now, timeZone: "UTC" })!;
    const limits = defaultFocusLimits({
      sharedLimit: cap.maxSelections,
      daysOutCtx: buildDaysOutContext(cap.daysUntilFight),
    });
    for (const preset of FOCUS_PRESETS) {
      const selections = resolveFocusPresetSelections(preset, limits);
      assert.ok(selections.key_goals.length <= QUICK_BUILD_KEY_GOAL_CAP, preset.key);
      assert.ok(selections.weak_areas.length <= QUICK_BUILD_WEAK_AREA_CAP, preset.key);
      assert.ok(selections.key_goals.length + selections.weak_areas.length <= cap.maxSelections, preset.key);
    }
  }
});

test("focus presets apply selections in priority order", () => {
  const explosive = FOCUS_PRESETS.find((preset) => preset.key === "explosive_power")!;
  const selections = resolveFocusPresetSelections(explosive, defaultFocusLimits());
  assert.deepEqual(selections.key_goals, ["power", "speed", "strength"]);
  assert.deepEqual(selections.weak_areas, ["power", "trunk_strength"]);
});

test("limited shared cap selects highest-priority goal and weak area first", () => {
  const weightCut = FOCUS_PRESETS.find((preset) => preset.key === "weight_cut_support")!;
  const selections = resolveFocusPresetSelections(weightCut, defaultFocusLimits({ sharedLimit: 2 }));
  assert.deepEqual(selections.key_goals, ["recovery"]);
  assert.deepEqual(selections.weak_areas, ["gas_tank"]);
});

test("individual cap space limits each focus lane independently", () => {
  const strengthBase = FOCUS_PRESETS.find((preset) => preset.key === "strength_base")!;
  const selections = resolveFocusPresetSelections(strengthBase, defaultFocusLimits({
    goalLimit: 1,
    weakAreaLimit: 1,
    sharedLimit: null,
  }));
  assert.deepEqual(selections.key_goals, ["strength"]);
  assert.deepEqual(selections.weak_areas, ["strength"]);
});

test("focus preset matching uses resolved cap-aware selections", () => {
  const limits = defaultFocusLimits({ sharedLimit: 2 });
  assert.equal(matchesFocusPreset(["power"], ["power"], limits), "explosive_power");
  assert.equal(matchesFocusPreset(["power"], ["gas_tank"], limits), null);
});

test("focus presets do not mutate existing manual selections", () => {
  const manualGoals = ["mobility"];
  const manualWeakAreas = ["balance"];
  const preset = FOCUS_PRESETS.find((entry) => entry.key === "gas_tank")!;
  resolveFocusPresetSelections(preset, defaultFocusLimits());
  assert.deepEqual(manualGoals, ["mobility"]);
  assert.deepEqual(manualWeakAreas, ["balance"]);
});

test("getAvailableFocusPresets disables profiles that cannot produce a valid goal", () => {
  const available = getAvailableFocusPresets({
    fightDate: "",
    noScheduledFight: true,
    limits: defaultFocusLimits({ daysOutCtx: buildDaysOutContext(0), sharedLimit: 2 }),
  });
  const technicalSharpness = available.find((entry) => entry.preset.key === "technical_sharpness")!;
  const mobilityControl = available.find((entry) => entry.preset.key === "mobility_control")!;
  assert.equal(technicalSharpness.disabledReason, "No goal available for this window.");
  assert.equal(mobilityControl.disabledReason, null);
});

test("deriveSetupSource handles three-way match with focus presets", () => {
  assert.equal(deriveSetupSource(["home", "three_days", "gas_tank"]), "preset");
  assert.equal(deriveSetupSource(["home", null, "gas_tank"]), "mixed");
  assert.equal(deriveSetupSource([null, null, null]), "manual");
});


test("disabled weight-cut goal stays unavailable in all focus presets", () => {
  for (const preset of FOCUS_PRESETS) {
    assert.equal(resolveFocusPresetSelections(preset, defaultFocusLimits()).key_goals.includes("weight_cut"), false);
  }
});
