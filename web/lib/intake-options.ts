import equipmentAliases from "../../shared/equipment-aliases.json";

export type IntakeOption = {
  label: string;
  value: string;
  disabled?: boolean;
};

export const GUIDED_INJURY_SEVERITY_VALUES = ["low", "moderate", "high"] as const;
export type GuidedInjurySeverity = (typeof GUIDED_INJURY_SEVERITY_VALUES)[number];

export const GUIDED_INJURY_SEVERITY_OPTIONS: Array<IntakeOption & { value: GuidedInjurySeverity }> = [
  { label: "Low", value: "low" },
  { label: "Moderate", value: "moderate" },
  { label: "High", value: "high" },
];

const GUIDED_INJURY_SEVERITY_ALIASES: Record<string, GuidedInjurySeverity> = {
  low: "low",
  mild: "low",
  moderate: "moderate",
  high: "high",
  severe: "high",
};

export function normalizeGuidedInjurySeverity(value: string | null | undefined): GuidedInjurySeverity | "" {
  const normalized = (value ?? "").trim().toLowerCase();
  return GUIDED_INJURY_SEVERITY_ALIASES[normalized] ?? "";
}

// Advances severity one step for the body-map tap-to-set interaction:
// unset/unknown → low → moderate → high → low. Always lands on a concrete
// severity so a marked zone is never left without a colour against the legend.
export function cycleGuidedInjurySeverity(value: string | null | undefined): GuidedInjurySeverity {
  const current = normalizeGuidedInjurySeverity(value);
  const index = current ? GUIDED_INJURY_SEVERITY_VALUES.indexOf(current) : -1;
  return GUIDED_INJURY_SEVERITY_VALUES[(index + 1) % GUIDED_INJURY_SEVERITY_VALUES.length];
}

export const TECHNICAL_STYLE_OPTIONS: IntakeOption[] = [
  { label: "Boxing", value: "boxing" },
  { label: "Kickboxing", value: "kickboxing" },
  { label: "Muay Thai", value: "muay_thai", disabled: true },
  { label: "MMA", value: "mma" },
  { label: "Wrestling", value: "wrestling", disabled: true },
  { label: "BJJ", value: "bjj", disabled: true },
];

export const TACTICAL_STYLE_OPTIONS: IntakeOption[] = [
  { label: "Pressure Fighter", value: "pressure_fighter" },
  { label: "Counter Striker", value: "counter_striker" },
  { label: "Distance Striker", value: "distance_striker" },
  { label: "Clinch Fighter", value: "clinch_fighter" },
  { label: "Grappler", value: "grappler" },
  { label: "Hybrid", value: "hybrid" },
];

export const PROFESSIONAL_STATUS_OPTIONS: IntakeOption[] = [
  { label: "Amateur", value: "amateur" },
  { label: "Professional", value: "professional" },
];

export const STANCE_OPTIONS: IntakeOption[] = [
  { label: "Orthodox", value: "Orthodox" },
  { label: "Southpaw", value: "Southpaw" },
  { label: "Switch", value: "Switch" },
  { label: "Hybrid", value: "Hybrid" },
];

export const TRAINING_AVAILABILITY_OPTIONS: IntakeOption[] = [
  { label: "Monday", value: "Monday" },
  { label: "Tuesday", value: "Tuesday" },
  { label: "Wednesday", value: "Wednesday" },
  { label: "Thursday", value: "Thursday" },
  { label: "Friday", value: "Friday" },
  { label: "Saturday", value: "Saturday" },
  { label: "Sunday", value: "Sunday" },
];

export type EquipmentAccessGroup = {
  label: string;
  options: IntakeOption[];
};

// This grouped list is the intake source of truth. Keep the persisted values
// stable: plans, drafts and previously completed intakes store these values.
export const EQUIPMENT_ACCESS_GROUPS: EquipmentAccessGroup[] = [
  {
    label: "Weights & strength",
    options: [
      { label: "Dumbbells", value: "dumbbells" },
      { label: "Kettlebells", value: "kettlebells" },
      { label: "Barbell", value: "barbell" },
      { label: "Trap Bar", value: "trap_bar" },
      { label: "Plates", value: "plate" },
      { label: "Bench", value: "bench" },
      { label: "Cable", value: "cable" },
      { label: "Landmine", value: "landmine" },
      { label: "Log", value: "log" },
      { label: "Atlas Stones", value: "atlas_stone" },
    ],
  },
  {
    label: "Conditioning",
    options: [
      { label: "Assault Bike", value: "assault_bike" },
      { label: "Rower", value: "rower" },
      { label: "Treadmill", value: "treadmill" },
      { label: "Step Mill", value: "step_mill" },
      { label: "Pool", value: "pool" },
      { label: "Sled", value: "sled" },
      { label: "Battle Ropes", value: "battle_ropes" },
      { label: "Tire", value: "tire" },
      { label: "Sledgehammer", value: "sledgehammer" },
      { label: "Weight Vest", value: "weight_vest" },
    ],
  },
  {
    label: "Combat",
    options: [
      { label: "Heavy Bag", value: "heavy_bag" },
      { label: "Pads / Mitts", value: "pads" },
      { label: "Partner", value: "partner" },
    ],
  },
  {
    label: "Plyometrics & movement",
    options: [
      { label: "Plyo Box / Box", value: "box" },
      { label: "Agility Ladder", value: "agility_ladder" },
      { label: "Hurdles", value: "hurdles" },
      { label: "Jump Rope", value: "jump_rope" },
    ],
  },
  {
    label: "Functional & accessories",
    options: [
      { label: "Medicine Ball", value: "medicine_ball" },
      { label: "Sandbag", value: "sandbag" },
      { label: "Bulgarian Bag", value: "bulgarian_bag" },
      { label: "Bands", value: "bands" },
      { label: "TRX", value: "trx" },
      { label: "Pull-Up Bar", value: "pullup_bar" },
      { label: "Swiss Ball", value: "swiss_ball" },
      { label: "Bosu Ball", value: "bosu_ball" },
      { label: "Neck Harness", value: "neck_harness" },
      { label: "Weight Belt", value: "weight_belt" },
      { label: "Water Jug", value: "water_jug" },
    ],
  },
  {
    label: "Recovery",
    options: [
      { label: "Foam Roller", value: "foam_roller" },
      { label: "Towel", value: "towel" },
      { label: "Stable support for rehab", value: "stable_support" },
      { label: "Table for supported rehab", value: "table" },
      { label: "Stable chair for seated rehab", value: "chair" },
      { label: "Stable wall for rehab", value: "wall" },
      { label: "Lightweight dowel for rehab", value: "dowel" },
      { label: "Sliding cloth for rehab", value: "cloth" },
      { label: "Soft ball for rehab", value: "soft_ball" },
    ],
  },
];

export const EQUIPMENT_ACCESS_OPTIONS: IntakeOption[] = EQUIPMENT_ACCESS_GROUPS.flatMap(
  (group) => group.options,
);

export const KEY_GOAL_OPTIONS: IntakeOption[] = [
  { label: "Power", value: "power" },
  { label: "Strength", value: "strength" },
  { label: "Conditioning", value: "conditioning" },
  { label: "Speed", value: "speed" },
  { label: "Skill Refinement", value: "skill_refinement" },
  { label: "Mobility", value: "mobility" },
  { label: "Recovery", value: "recovery" },
  { label: "Weight Cut", value: "weight_cut", disabled: true },
];

export const WEAK_AREA_OPTIONS: IntakeOption[] = [
  { label: "Gas Tank", value: "gas_tank" },
  { label: "Strength", value: "strength" },
  { label: "Power", value: "power" },
  { label: "Speed", value: "speed" },
  { label: "Footwork", value: "footwork" },
  { label: "Balance", value: "balance" },
  { label: "Mobility", value: "mobility" },
  { label: "Coordination", value: "coordination" },
  { label: "Core / Trunk Strength", value: "trunk_strength" },
];

export const RECORD_PATTERN = /^\d+-\d+(?:-\d+)?$/;

const LEGACY_OPTION_LABELS: Record<string, string> = {
  air_bike: "Air Bike",
  dumbbell: "Dumbbell",
  kettlebell: "Kettlebell",
  jump_rope: "Jump Rope",
  thai_pads: "Pads / Mitts",
  thai_pad: "Pads / Mitts",
  focus_mitts: "Pads / Mitts",
  partner_mitts: "Pads / Mitts",
};

/**
 * Legacy equipment values that mean the same athlete capability as the
 * canonical `pads` option. Profiles saved before "Thai Pads" became
 * "Pads / Mitts" still carry these, and `retainKnownOptionValues` would
 * otherwise drop them as unknown the first time such a profile is re-saved.
 *
 * The spellings come from shared/equipment-aliases.json, the list
 * `fightcamp/training_context.py` normalizes to `pads`; the backend remains the
 * authority for eligibility, this only keeps the form honest about what the
 * athlete already told us.
 */
export const LEGACY_EQUIPMENT_ALIASES: Record<string, string> = Object.fromEntries(
  equipmentAliases.pads.aliases.map((alias) => [alias, equipmentAliases.pads.canonical]),
);

/** Map legacy equipment values onto their canonical option value, deduped. */
export function canonicalizeEquipmentAccessValues(values: string[] | undefined): string[] {
  const canonical: string[] = [];
  for (const value of values ?? []) {
    const mapped = LEGACY_EQUIPMENT_ALIASES[value] ?? value;
    if (mapped && !canonical.includes(mapped)) {
      canonical.push(mapped);
    }
  }
  return canonical;
}

export function detectDeviceTimeZone(): string {
  if (typeof window === "undefined" || typeof Intl === "undefined") {
    return "";
  }
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
}

export function detectDeviceLocale(): string {
  if (typeof navigator === "undefined") {
    return "";
  }
  return navigator.language || "";
}

export function sanitizeRecordInput(value: string): string {
  return value.replace(/[^\d-]/g, "").replace(/-{2,}/g, "-");
}

export function isValidRecordFormat(value: string): boolean {
  const normalized = value.trim();
  return !normalized || RECORD_PATTERN.test(normalized);
}

export function toggleListValue(values: string[], target: string): string[] {
  return values.includes(target)
    ? values.filter((value) => value !== target)
    : [...values, target];
}

export function retainKnownOptionValues(values: string[] | undefined, options: IntakeOption[]): string[] {
  const knownValues = new Set(options.filter((option) => !option.disabled).map((option) => option.value));
  return (values ?? []).filter((value) => knownValues.has(value));
}

export function getOptionLabel(options: IntakeOption[], value: string): string {
  return options.find((option) => option.value === value)?.label ?? LEGACY_OPTION_LABELS[value] ?? value;
}

export function getOptionLabels(options: IntakeOption[], values: string[] | undefined): string[] {
  return (values ?? []).map((value) => getOptionLabel(options, value));
}
