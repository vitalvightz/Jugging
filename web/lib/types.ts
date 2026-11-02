import type { TrainingHabitPreferences } from "./training-habits";
// Role foundation for the single Unlxck app. `athlete` and `admin` are live in
// private beta; `coach` and `gym_owner` are reserved for public beta and are not
// yet selectable at sign-up or assignable to accounts. We use `gym_owner` (not
// `gym`) because the account represents the person managing the gym organisation.
export type UserRole = "athlete" | "coach" | "gym_owner" | "admin";
export type AppearanceMode = "dark" | "light";
// localStorage key used to remember the applied appearance mode so the document
// can restore it before first paint (see the pre-paint script in the app layout).
export const APPEARANCE_STORAGE_KEY = "unlxck.appearance-mode";
export type SexValue = "male" | "female";
export type DailyActivityLevel = "low" | "mixed" | "active_job";
export type WeighInType = "same_day" | "day_before" | "informal";
export type PhaseOverride = "GPP" | "SPP" | "TAPER";
export type FatigueLevel = "low" | "moderate" | "high";
export type WeightSource = "manual" | "latest_bodyweight_log" | "imported";
export type TrainingRestrictionLevel = "none" | "minor" | "moderate" | "major";
export type SleepQuality = "good" | "mixed" | "poor";
export type AppetiteStatus = "normal" | "low" | "high";
export type FoundationStatus = "incomplete" | "sufficient" | "complete";
export type NutritionWorkspaceSource = "default" | "draft" | "intake";
export type FightWeekOverrideBand = "none" | "final_day_protocol" | "micro_taper_protocol" | "mini_taper_protocol";
// NOTE: "technical" is a legacy internal token kept for saved-draft/API compatibility.
// It represents support_work_days (non-hard training / S&C-compatible slots).
export type SessionDayType = "hard_spar" | "technical" | "strength" | "conditioning" | "recovery" | "off";
export type SparringDayClass = "primary_hard" | "secondary_hard" | "managed_hard" | "technical" | "none";
export type EffectiveLoad = "hard" | "technical" | "reduced" | "none";

export type GenerationJobStatus = "queued" | "running" | "completed" | "review_required" | "failed";

export type AthleteProfileInput = {
  full_name: string;
  sex?: SexValue | null;
  /**
   * Derived compatibility value, not something the athlete edits.
   * `profiles.date_of_birth` is the only persisted source of age truth: the
   * form hydrates this from that date (see `hydratePlanRequest`) and the server
   * overwrites it again before generation, so anything sent here is discarded.
   */
  age?: number | null;
  weight_kg?: number | null;
  target_weight_kg?: number | null;
  height_cm?: number | null;
  technical_style: string[];
  tactical_style: string[];
  stance?: string;
  professional_status?: string;
  record?: string;
  athlete_timezone?: string;
  athlete_locale?: string;
};

export type GuidedInjuryInput = {
  area?: string;
  // Stable body-map zone key (e.g. "l_shoulder") when the area was picked from
  // the map. Lets the map stay lit even after the athlete edits the free-text
  // area, and prevents duplicate cards when the same zone is tapped again.
  zone?: string;
  // The API accepts only these values (checked in lib/api-contract.ts).
  severity?: "" | "low" | "moderate" | "high";
  trend?: string;
  avoid?: string;
  notes?: string;
  injury_type?: string;
  injury_subtypes?: string[];
  surface_type?: string;
  timeframe?: string;
  cleared?: string;
  open_wound?: string;
  bleeding_status?: string;
  infection_signs?: string[];
  impact_related?: string;
  sensitive_area?: string;
};

export type NutritionProfileInput = {
  sex?: SexValue | null;
  age?: number | null;
  height_cm?: number | null;
  daily_activity_level?: DailyActivityLevel | null;
  dietary_restrictions: string[];
  food_preferences: string[];
  meals_per_day_preference?: number | null;
  foods_avoided_pre_session: string[];
  foods_avoided_fight_week: string[];
  supplement_use: string[];
  caffeine_use?: boolean | null;
};

export type NutritionBodyweightLogEntry = {
  date: string;
  weight_kg: number;
  time?: string | null;
  is_fasted?: boolean | null;
  notes?: string | null;
};

export type NutritionReadinessInput = {
  sleep_quality?: SleepQuality | null;
  appetite_status?: AppetiteStatus | null;
};

export type NutritionMonitoringInput = {
  daily_bodyweight_log: NutritionBodyweightLogEntry[];
};

export type NutritionCoachControlsInput = {
  coach_override_enabled: boolean;
  athlete_override_enabled: boolean;
  do_not_reduce_below_calories?: number | null;
  protein_floor_g_per_kg?: number | null;
  fight_week_manual_mode: boolean;
  water_cut_locked_to_manual: boolean;
};

export type GoalWeaknessCollisionDetail = {
  tag: string;
  label: string;
  detail: string;
};

export type NutritionSandCPreferences = {
  equipment_access: string[];
  key_goals: string[];
  primary_goal?: string | null;
  weak_areas: string[];
  primary_weak_area?: string | null;
  goal_weakness_collision_detail?: string;
  goal_weakness_collision_tags?: string[];
  goal_weakness_collision_details?: GoalWeaknessCollisionDetail[];
  training_preference?: string;
  mindset_challenges?: string;
  notes?: string;
  random_seed?: number | null;
};

export type NutritionSharedCampContext = {
  fight_date?: string;
  rounds_format?: string;
  weigh_in_type?: WeighInType | null;
  weigh_in_time?: string | null;
  current_weight_kg?: number | null;
  current_weight_recorded_at?: string | null;
  current_weight_source?: WeightSource | null;
  target_weight_kg?: number | null;
  target_weight_range_kg?: [number, number] | number[] | null;
  phase_override?: PhaseOverride | null;
  fatigue_level?: FatigueLevel | null;
  weekly_training_frequency?: number | null;
  training_availability: string[];
  hard_sparring_days: string[];
  support_work_days: string[];
  session_types_by_day: Record<string, SessionDayType>;
  injuries?: string;
  guided_injury?: GuidedInjuryInput | null;
  training_restriction_level?: TrainingRestrictionLevel | null;
};

export type NutritionBodyweightTrendPoint = {
  date: string;
  scale_weight_kg: number;
  trend_weight_kg: number;
};

export type WeightCutRiskBand = "none" | "moderate" | "high" | "severe";

// Fields added after first release are optional: the web app can deploy ahead
// of the API, and the tracker must read an older response without them.
export type NutritionDerivedState = {
  days_until_fight?: number | null;
  weight_cut_pct: number;
  weight_cut_risk: boolean;
  weight_cut_risk_band?: WeightCutRiskBand;
  weight_cut_supervision_required?: boolean;
  aggressive_weight_cut: boolean;
  high_pressure_weight_cut: boolean;
  short_notice: boolean;
  fight_week: boolean;
  readiness_flags: string[];
  fight_week_override_band: FightWeekOverrideBand;
  current_phase_effective?: string | null;
  rolling_7_day_average_weight?: number | null;
  rolling_7_day_weigh_in_days?: number;
  weekly_pace_kg?: number | null;
  bodyweight_trend?: NutritionBodyweightTrendPoint[];
  foundation_status: FoundationStatus;
  missing_required_fields: string[];
};

export type NutritionWorkspaceState = {
  athlete_id: string;
  source: NutritionWorkspaceSource;
  intake_id?: string | null;
  nutrition_profile: NutritionProfileInput;
  shared_camp_context: NutritionSharedCampContext;
  s_and_c_preferences: NutritionSandCPreferences;
  nutrition_readiness: NutritionReadinessInput;
  nutrition_monitoring: NutritionMonitoringInput;
  nutrition_coach_controls: NutritionCoachControlsInput;
  derived: NutritionDerivedState;
};

export type NutritionWorkspaceUpdateRequest = Omit<NutritionWorkspaceState, "athlete_id" | "source" | "intake_id" | "derived">;

export type PlanRequest = {
  athlete: AthleteProfileInput;
  fight_date: string;
  /** Optional event/competition name for the scheduled fight. */
  event_name?: string;
  no_scheduled_fight?: boolean;
  open_camp_weeks?: number;
  rounds_format?: string;
  weekly_training_frequency?: number | null;
  fatigue_level?: string;
  equipment_access: string[];
  training_availability: string[];
  hard_sparring_days: string[];
  reduced_contact_requested?: boolean;
  support_work_days: string[];
  injuries?: string;
  guided_injury?: GuidedInjuryInput | null;
  guided_injuries?: GuidedInjuryInput[] | null;
  key_goals: string[];
  primary_goal?: string | null;
  weak_areas: string[];
  primary_weak_area?: string | null;
  goal_weakness_collision_detail?: string;
  goal_weakness_collision_tags?: string[];
  goal_weakness_collision_details?: GoalWeaknessCollisionDetail[];
  training_preference?: string;
  mindset_challenges?: string;
  notes?: string;
  random_seed?: number | null;
  intake_id?: string | null;
  current_step?: number;
  shared_camp_context?: NutritionSharedCampContext;
  s_and_c_preferences?: NutritionSandCPreferences;
  nutrition_readiness?: NutritionReadinessInput;
  nutrition_monitoring?: NutritionMonitoringInput;
  nutrition_coach_controls?: NutritionCoachControlsInput;
};

export type ManualStage2SubmissionRequest = {
  final_plan_text: string;
};

export type ApproveAndResumeGenerationRequest = {
  reason: string;
};

export type ProfileUpdateRequest = {
  starting_path?: "tracking_first" | "plan_first";
  training_habits?: TrainingHabitPreferences;
  training_habits_revision?: number;
  full_name?: string;
  technical_style?: string[];
  tactical_style?: string[];
  stance?: string;
  professional_status?: string;
  record?: string;
  athlete_timezone?: string;
  athlete_locale?: string;
  appearance_mode?: AppearanceMode;
  onboarding_draft?: Record<string, unknown> | null;
  avatar_url?: string | null;
  nutrition_profile?: NutritionProfileInput | null;
  /** Intent only: the server stamps `private_trial_ack_at` itself. */
  private_trial_acknowledged?: boolean;
};

export type ProfileRecord = {
  starting_path?: "tracking_first" | "plan_first" | null;
  training_habits?: TrainingHabitPreferences;
  training_habits_revision?: number;
  athlete_id: string;
  email: string;
  username?: string | null;
  username_change_history?: string[];
  role: UserRole;
  access_status?: "pending" | "approved";
  full_name: string;
  technical_style: string[];
  tactical_style: string[];
  stance: string;
  professional_status: string;
  record: string;
  athlete_timezone: string;
  athlete_locale: string;
  appearance_mode: AppearanceMode;
  onboarding_draft?: Record<string, unknown> | null;
  avatar_url?: string | null;
  nutrition_profile: NutritionProfileInput;
  /** Null until the athlete confirms they read the private trial instructions. */
  private_trial_ack_at?: string | null;
  /**
   * Compliance state. Every field below is decided by the server: `is_minor`,
   * `age_band`, `terms_accepted` and `health_consent_granted` are derived from
   * the stored date of birth and server-owned consent evidence on each read, so
   * the client displays them and never computes them.
   */
  date_of_birth?: string | null;
  age_band?: string;
  is_minor?: boolean;
  meets_minimum_age?: boolean;
  terms_version?: string | null;
  terms_accepted_at?: string | null;
  terms_accepted?: boolean;
  health_consent_version?: string | null;
  health_consent_at?: string | null;
  health_data_consent?: boolean;
  health_consent_withdrawn_at?: string | null;
  health_consent_granted?: boolean;
  created_at: string;
  updated_at: string;
};

/**
 * Intent only — the server stamps every timestamp and version string, so an
 * acceptance cannot be backdated or attributed to a document the athlete never
 * saw. `health_data_consent: false` withdraws consent.
 */
export type ComplianceAcceptanceRequest = {
  date_of_birth?: string;
  accept_terms?: boolean;
  health_data_consent?: boolean;
};

export type UsernameRateLimitInfo = {
  max_changes_per_window: number;
  window_days: number;
  remaining: number;
  next_available_at?: string | null;
};

export type UsernameChangeRequest = {
  username: string;
};

export type PlanActivationState = "eligible" | "fight_date_passed" | "status_ineligible";

export type PlanSummary = {
  custom_plan_id?: string | null;
  plan_id: string;
  plan_name?: string | null;
  athlete_id: string;
  full_name: string;
  fight_date: string;
  technical_style: string[];
  created_at: string;
  status: string;
  activation_state: PlanActivationState;
  pdf_url?: string | null;
  review_reason?: string | null;
};

export type PlanOutputs = {
  plan_text: string;
  pdf_url?: string | null;
  // Schema-first structured plan (see api/structured_plan_models.py). Optional so
  // legacy/raw-text-only plans keep working: when absent or malformed the UI
  // renders `plan_text` as the fallback. Typed permissively because the payload
  // is best-effort and the renderer must be defensive.
  structured_plan?: StructuredPlan | null;
  schema_version?: string | null;
  /** Curated demo video per block, keyed by the block's exact display_name
   * (api/services/exercise_media.py). Absent keys render cues-only. */
  exercise_media?: Record<string, ExerciseMedia> | null;
};

export type ExerciseMedia = {
  provider: "youtube";
  video_id: string;
  /** Loop segment: the rep that shows the movement, not the intro. */
  start_s: number;
  end_s?: number | null;
  /** "coach" = filmed by the UNLXCK coaching team; "curated" = vetted third-party. */
  source: "curated" | "coach";
  /** YouTube channel that published the video, credited under the player. */
  channel_title?: string | null;
  /** Frame shape. Absent or null = not detected yet: render as landscape. */
  orientation?: "portrait" | "landscape" | null;
};

export type MeasuredValue = {
  value?: number | null;
  unit?: string | null;
};

export type LoadPrescription = {
  method?: string | null;
  value?: number | null;
  unit?: string | null;
  ref?: string | null;
  display?: string | null;
};

export type EffortPrescription = {
  method?: string | null;
  value?: number | string | null;
  scale?: string | null;
};

export type MindsetAnchor = {
  intent?: string | null;
  focus_cue?: string | null;
  reset_cue?: string | null;
  confidence_anchor?: string | null;
  context?: string | null;
};

/** One movement inside a cluster block, e.g. each half of a contrast pair. */
export type BlockComponent = {
  name: string;
  reps?: number | string | null;
  load?: LoadPrescription | null;
  effort?: EffortPrescription | null;
};

export type StructuredBlock = {
  prescription_source?: "athlete" | null;
  prescription_text?: string | null;
  athlete_dose?: Record<string, unknown> | null;
  catalogue_id?: string | null;
  private_media_id?: string | null;
  community_revision_id?: string | null;
  source_session_id?: string | null;
  notes?: string | null;
  block_id?: string | null;
  block_type?: string | null;
  contact_level?: "none" | "controlled" | "full" | "unknown" | null;
  mechanical_load_regions?: string[] | null;
  tags?: string[] | null;
  display_name?: string | null;
  /** Canonical exercise identity (server-stamped); demo videos resolve on it. Absent on legacy plans. */
  exercise_key?: string | null;
  category?: string | null;
  order_index?: number | null;
  duration?: MeasuredValue | null;
  sets?: number | null;
  reps?: number | string | null;
  load?: LoadPrescription | null;
  effort?: EffortPrescription | null;
  rest?: MeasuredValue | null;
  /** Qualitative rest the dose states without a number ("Full rest between holds"). */
  rest_note?: string | null;
  work?: MeasuredValue | null;
  distance?: MeasuredValue | null;
  rounds?: number | null;
  /** Cluster (contrast pair) movements sharing the block's rounds and rest. Server-owned. */
  components?: BlockComponent[] | null;
  intensity?: string | null;
  energy_system?: string | null;
  impact_level?: string | null;
  purpose?: string | null;
  why_today?: string | null;
  coaching_cues?: string[] | null;
  /** Drill setup and sequence; separate from the single short coach cue. */
  execution_instructions?: string | null;
  /** Exact approved rehab instructions from the live/frozen prescription. */
  instructions?: string | null;
  regression_options?: string[] | null;
  substitutions?: string[] | null;
  progression_rule?: string | null;
  /** Open/ongoing plans only: the block's own week-4 deload instruction. */
  deload_rule?: string | null;
  stop_rules?: string[] | null;
  /** Rehab blocks only: server-frozen drill metadata. */
  rehab_drill_id?: string | null;
  injury_id?: string | null;
  is_loading?: boolean | null;
  dose?: { sets?: number | null; reps?: number | null; duration_seconds?: number | null } | null;
  drill_snapshot?: { rehab_stage?: string | null; function?: string | null; contraction_type?: string | null;
    dose?: Record<string, unknown> | null; equipment?: string[] | null;
    prescription?: { frequency?: string | null } | null } | null;
  /** Approved rehab requirements from the live/frozen prescription. */
  range_choice?: string | null;
  resistance?: { mode?: string | null; kg?: number | null } | null;
  mandatory_restrictions?: string[] | null;
  frequency?: string | null;
  /** Server-owned: present only on an athlete's swap or added exercise. */
  athlete_change?: AthleteExerciseChange | null;
};

export type AthleteExerciseChange = {
  override_id?: string | null;
  action?: "swap" | "add" | "remove" | "edit" | null;
  scope?: "occurrence" | "future" | null;
  from_date?: string | null;
  original_name?: string | null;
};

export type AthleteRemovedExercise = {
  override_id?: string | null;
  display_name?: string | null;
  scope?: "occurrence" | "future" | null;
};

export type ExerciseChangeScope = { value: "occurrence" | "future"; label: string };

export type ExerciseAlternative = {
  catalogue_id: string;
  exercise_key: string;
  name: string;
  reason?: string | null;
  equipment?: string[] | null;
  media?: ExerciseMedia | null;
  prescription?: string | null;
  dose_required?: boolean;
  dose_mode?: "reps" | "duration" | "intervals" | "distance" | "text";
  suggested_dose?: Record<string, unknown> | null;
};

export type ExerciseOptionsResponse = {
  alternatives: ExerciseAlternative[];
  more_available?: boolean;
  notice?: string | null;
  scopes: ExerciseChangeScope[];
};

type ExerciseDoseExtras = {
  load_or_intensity?: string | null;
  rpe?: number | null;
  tempo?: string | null;
  per_side?: boolean | null;
};
export type ExerciseChangeDose = ExerciseDoseExtras & (
  | { mode: "reps"; sets: number; reps: number; rest_seconds?: number | null; rpe?: number | null }
  | { mode: "duration"; duration_seconds: number; sets?: number | null; rest_seconds?: number | null }
  | { mode: "intervals"; rounds: number; work_seconds: number; rest_seconds: number }
  | { mode: "distance"; distance: number; distance_unit: "m" | "km" | "mi" | "yd"; sets?: number | null; rest_seconds?: number | null }
  | { mode: "text"; prescription: string; rest_seconds?: number | null });

export type ExerciseChangeRequest = {
  client_request_id: string;
  action: "swap" | "add" | "remove" | "edit";
  scope?: "occurrence" | "future";
  date: string;
  session_id: string;
  block_id?: string | null;
  catalogue_id?: string | null;
  dose?: ExerciseChangeDose | null;
  coaching_cues?: string[] | null;
  stop_rules?: string[] | null;
  regression_options?: string[] | null;
  progression_rule?: string | null;
  confirmed?: boolean;
};

export type ExerciseChangeResult = {
  id: string;
  action: "swap" | "add" | "remove" | "edit";
  scope: "occurrence" | "future";
  date: string;
  name?: string | null;
  original_name?: string | null;
  revoked?: boolean;
  session?: StructuredSession | null;
};

export type StructuredSession = {
  prescription_source?: "athlete" | null;
  /** Server-owned: exercises the athlete removed from this occurrence. */
  athlete_removed?: AthleteRemovedExercise[] | null;
  session_id?: string | null;
  session_type?: string | null;
  title?: string | null;
  objective?: string | null;
  planned_duration?: MeasuredValue | null;
  primary_stressor?: string | null;
  cns_demand?: string | null;
  impact_level?: string | null;
  completion_status?: string | null;
  mindset_anchor?: MindsetAnchor | null;
  blocks?: StructuredBlock[] | null;
  /** 1-based, server-decided position within the day; `sessions` is already in this order. */
  execution_order?: number | null;
  /** Server-owned: offered, never required (the optional camp Fight Visualisation). */
  optional?: boolean | null;
};

export type StructuredTodayCard = {
  headline?: string | null;
  readiness_status?: string | null;
  primary_warning?: string | null;
  nutrition_summary?: string | null;
  weight_cut_warning?: string | null;
  mindset_anchor?: MindsetAnchor | null;
  /**
   * Coach-owned contact (declared / downgraded sparring) that coexists with the
   * day's app sessions. Set deterministically when a contact day also carries app
   * work; the renderer surfaces it as a coach-led context block above the
   * session cards so the sparring day never disappears behind the app session.
   */
  coach_led_contact?: string | null;
  /** Server-owned explanation for a reduced-dose (deloaded) hard-sparring day. */
  contact_reason?: string | null;
};

export type PriorityMicrodose = {
  goal?: string | null;
  name?: string | null;
  prescription?: string | null;
};

export type StructuredDay = {
  date?: string | null;
  weekday?: "Mon" | "Tue" | "Wed" | "Thu" | "Fri" | "Sat" | "Sun" | null;
  day_type?: string | null;
  countdown_label?: string | null;
  phase_label?: string | null;
  planning_week_index?: number | null;
  planning_week_phase?: string | null;
  planning_day_role_keys?: string[] | null;
  priority_microdose?: PriorityMicrodose | null;
  today_card?: StructuredTodayCard | null;
  sessions?: StructuredSession[] | null;
};

export type StructuredWeek = {
  week_id?: string | null;
  week_index?: number | null;
  phase_label?: string | null;
  week_goal?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  countdown_start?: string | null;
  countdown_end?: string | null;
  display_week_kind?: string | null;
  planning_week_indices?: number[] | null;
  phase_coverage?: string[] | null;
  days?: StructuredDay[] | null;
};

export type StructuredRedFlagRule = {
  rule_id?: string | null;
  severity?: string | null;
  when?: string | null;
  display_text?: string | null;
  action?: string | null;
};

export type StructuredWeightCutWarning = {
  risk_level?: string | null;
  display_text?: string | null;
  requires_professional_support?: boolean | null;
};

export type StructuredNutrition = {
  summary?: string | null;
  daily_focus?: string | null;
  training_day_guidance?: string | null;
  fight_week_guidance?: string | null;
  weight_cut_warning?: StructuredWeightCutWarning | null;
};

// Athlete-safe projection of Stage 1's deterministic computed_support. Coach/
// medical-gated dosing is stripped server-side, so these shapes never carry it.
export type DeterministicMacroRange = {
  min?: number | null;
  max?: number | null;
  per_kg?: (number | null)[] | null;
  per_kg_l?: (number | null)[] | null;
  note?: string | null;
};

export type DeterministicWeightCut = {
  active?: boolean | null;
  risk_band?: string | null;
  supervision_required?: boolean | null;
};

export type DeterministicNutritionPhase = {
  phase?: string | null;
  meal_structure?: string | null;
  calorie_adjustment?: string | null;
  protein_g_per_day?: DeterministicMacroRange | null;
  carbs_g_per_day?: DeterministicMacroRange | null;
  fats_g_per_day?: DeterministicMacroRange | null;
  hydration_ml_per_day?: DeterministicMacroRange | null;
  fuel_timing?: { pre?: string | null; intra?: string | null; post?: string | null } | null;
  fatigue_adjustment?: string | null;
  weight_cut?: DeterministicWeightCut | null;
};

export type DeterministicRecoveryPhase = {
  phase?: string | null;
  core_strategies?: string[] | null;
  sleep_hours_target?: (number | null)[] | null;
  age_adjustments?: string[] | null;
  fatigue_flags?: string[] | null;
  fatigue_notes?: string[] | null;
  phase_focus?: string[] | null;
  weight_cut?: DeterministicWeightCut | null;
};

export type DeterministicSupport = {
  schema_version?: string | null;
  source?: string | null;
  nutrition?: { by_phase?: Record<string, DeterministicNutritionPhase> | null } | null;
  recovery?: { by_phase?: Record<string, DeterministicRecoveryPhase> | null } | null;
};

export type StructuredPlanMetadata = {
  prescription_source?: "athlete" | null;
  title?: string | null;
  sport?: string | null;
  plan_type?: string | null;
  status?: string | null;
  units?: string | null;
};

export type StructuredAthleteContext = {
  sport_profile?: string | null;
  style_profile?: string | null;
  experience_level?: string | null;
  weight_class?: string | null;
};

export type StructuredEventContext = {
  fight_date?: string | null;
  match_date?: string | null;
  weigh_in_date?: string | null;
  event_type?: string | null;
  ruleset?: string | null;
};

// A short, plan-level "active note" / non-negotiable reminder (weight cut,
// injury, nutrition, general). Surfaced as a standalone Active Notes card so
// header/footer plan context is not lost in the structured view.
export type StructuredPlanNote = {
  category?: string | null;
  label?: string | null;
  text?: string | null;
};

// An optional, plan-level readiness summary. `focus` / `injury_watch` /
// `weekly_load` feed the plan page's lighter camp-readiness strip (see
// getReadinessStrip); when absent the strip derives them from the current day /
// week. `today_call` is reserved for the Today surface — the exact "train /
// modify / pull back" decision belongs there and is intentionally NOT rendered
// on the plan page. Every field is nullable. Purely additive — generation does
// not emit it yet.
export type StructuredReadinessSnapshot = {
  today_call?: string | null;
  focus?: string | null;
  injury_watch?: string | null;
  weekly_load?: string | null;
};

export type StructuredPlan = {
  schema_version?: string | null;
  plan_metadata?: StructuredPlanMetadata | null;
  athlete_context?: StructuredAthleteContext | null;
  event_context?: StructuredEventContext | null;
  readiness_snapshot?: StructuredReadinessSnapshot | null;
  red_flag_rules?: StructuredRedFlagRule[] | null;
  plan_notes?: StructuredPlanNote[] | null;
  weeks?: StructuredWeek[] | null;
  nutrition?: StructuredNutrition | null;
  deterministic_support?: DeterministicSupport | null;
  progression_notes?: string | null;
  raw_markdown_fallback?: string | null;
};

export type PlanAdvisory = {
  kind: "sparring_adjustment";
  action: "deload" | "convert";
  risk_band?: "green" | "amber" | "red" | "black" | null;
  phase: string;
  week_label: string;
  days: string[];
  title: string;
  reason: string;
  suggestion: string;
  replacement?: string | null;
  disclaimer: string;
};

export type WeeklyDayEntry = {
  weekday: "Mon" | "Tue" | "Wed" | "Thu" | "Fri" | "Sat" | "Sun";
  sparring_day_class: SparringDayClass;
  effective_load: EffectiveLoad;
  status: string;
  reason: string;
  coach_note: string;
  reason_codes: string[];
  d_day?: number | null;
  day_label?: string;
  weekday_with_label?: string;
  calendar_date?: string | null;
  is_fight_day?: boolean;
  is_after_fight_day?: boolean;
};

export type WeeklySchedule = {
  plan_id: string;
  week_index: number;
  week_count: number;
  phase: string;
  projected_days_until_fight_start?: number | null;
  projected_days_until_fight_end?: number | null;
  day_label?: string;
  countdown_range?: number[];
  week_countdown_label?: string;
  week_label_with_countdown?: string;
  days: WeeklyDayEntry[];
};

export type AdminPlanOutputs = {
  coach_notes: string;
  why_log: Record<string, unknown>;
  planning_brief?: Record<string, unknown> | null;
  stage2_payload?: Record<string, unknown> | null;
  parsing_metadata: Record<string, unknown>;
  stage2_handoff_text: string;
  draft_plan_text: string;
  final_plan_text: string;
  stage2_retry_text: string;
  stage2_validator_report: Record<string, unknown>;
  stage2_status: string;
  stage2_attempt_count: number;
  /** The athlete's intake injuries, sent for plans injury triage held. */
  intake_injuries?: {
    injuries: string;
    guided_injuries: Array<Record<string, unknown>>;
  } | null;
};

export type StructuredCardLifecycleState =
  | "live"
  | "building"
  | "failed"
  | "not_attempted"
  | "none";

export type StructuredCardState = {
  state: StructuredCardLifecycleState;
  reasons: string[];
  schema_version?: string | null;
  attempt_started_at?: string | null;
};

export type PlanScheduleContext = {
  schedule_mode: "event_countdown" | "open_recurring" | "static_undated";
  projection_status: "not_required" | "projected" | "unavailable";
  anchor_date?: string | null;
  current_training_day?: string | null;
  /** Athlete-local day the plan was created (YYYY-MM-DD), resolved server-side. */
  plan_start_day?: string | null;
  block_number?: number | null;
  current_week_number?: number | null;
};

export type PlanDetail = PlanSummary & {
  outputs: PlanOutputs;
  advisories: PlanAdvisory[];
  admin_outputs?: AdminPlanOutputs | null;
  structured_card_state: StructuredCardState;
  plan_source?: string | null;
  schedule_context?: PlanScheduleContext | null;
  // True when the stored profile could not be refreshed during generation, so
  // this plan was built from the submitted intake and the saved profile may be
  // stale. Surfaced as a non-blocking notice in the plan viewer.
  profile_refresh_failed?: boolean;
  // Per-region Rehab/Prehab labelling for this plan's rehab blocks, derived
  // server-side from the athlete's live injury flags (see resolve_rehab_label_policy
  // in api/rehab_labels.py). Applied per block by resolveBlockRehabLabel in
  // web/lib/rehab-label.ts. Absent on legacy payloads → everything reads "Rehab".
  rehab_label_policy?: RehabLabelPolicy;
};

/** A body region the athlete is currently injured in, plus its match terms.
 * `terms` are normalized (lowercase, punctuation collapsed) location synonyms
 * and rehab-bank drill names. */
export type ActiveInjuryRegion = {
  region: string;
  terms: string[];
};

export type RehabLabelPolicy = {
  // What a rehab block reads as when it matches no active region: "prehab" once
  // every live injury has been localized, "rehab" while one cannot be.
  default_mode: "rehab" | "prehab";
  active_regions: ActiveInjuryRegion[];
};

export type ProgressMilestone = {
  code: string;
  label: string;
  detail: string;
  at: string;
  meta?: Record<string, unknown>;
};

export type GenerationJobResponse = {
  job_id: string;
  athlete_id: string;
  client_request_id: string;
  status: GenerationJobStatus;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  heartbeat_at?: string | null;
  completed_at?: string | null;
  error?: string | null;
  plan_id?: string | null;
  latest_plan_id?: string | null;
  source?: string | null;
  progress_milestones?: ProgressMilestone[];
  warnings?: string[];
  can_retry?: boolean;
  status_url?: string | null;
  message?: string | null;
  stage2_status?: string | null;
  requires_admin_resume?: boolean;
  ready_to_open?: boolean;
};

export type MeResponse = {
  profile: ProfileRecord;
  effective_admin?: boolean;
  latest_intake?: PlanRequest | null;
  latest_plan?: PlanSummary | null;
  plan_count: number;
  username_rate_limit: UsernameRateLimitInfo;
};

export type AdminAthleteRecord = {
  last_meaningful_activity_at?: string | null;
  athlete_id: string;
  email: string;
  role: UserRole;
  access_status?: "pending" | "approved";
  full_name: string;
  technical_style: string[];
  tactical_style: string[];
  stance: string;
  professional_status: string;
  record: string;
  athlete_timezone: string;
  athlete_locale: string;
  appearance_mode: AppearanceMode;
  onboarding_draft?: PlanRequest | null;
  latest_intake?: PlanRequest | null;
  nutrition_profile: NutritionProfileInput;
  created_at: string;
  updated_at: string;
  plan_count: number;
  latest_plan_created_at?: string | null;
  first_usable_plan_at?: string | null;
  time_to_first_usable_plan_seconds?: number | null;
};

export type AdminTimeToFirstPlanSummary = {
  window_days: number;
  signups: number;
  with_usable_plan: number;
  median_seconds?: number | null;
  p75_seconds?: number | null;
};

export type AdminLatestIntakeUpdateRequest = {
  fight_date?: string | null;
  no_scheduled_fight?: boolean;
  rounds_format?: string | null;
  weekly_training_frequency?: number | null;
  training_availability?: string[];
  equipment_access?: string[];
  key_goals?: string[];
  weak_areas?: string[];
  injuries?: string | null;
};

export type AdminPlanSummary = PlanSummary & {
  athlete_email: string;
  profile_unavailable?: boolean;
};

export type GenerationRequestPayloadSummary = {
  athlete_name: string;
  fight_date: string;
  phase: string;
  fight_format: string;
  fatigue_level: string;
  goals: string[];
  weaknesses: string[];
  injuries: string[];
  training_availability: string;
};

export type AdminGenerationJobDiagnostic = {
  job_id: string;
  athlete_id?: string;
  athlete_email?: string;
  athlete_full_name?: string;
  intake_id?: string | null;
  status: GenerationJobStatus;
  source: string;
  created_at: string;
  started_at?: string | null;
  heartbeat_at?: string | null;
  completed_at?: string | null;
  client_request_id: string;
  retry_of?: string | null;
  error?: string | null;
  stale_reason?: string | null;
  plan_id?: string | null;
  can_retry: boolean;
  stage2_status?: string | null;
  requires_admin_resume?: boolean;
  is_stale: boolean;
  profile_unavailable?: boolean;
  warnings?: string[];
  request_payload_summary: GenerationRequestPayloadSummary;
};

// ---------------------------------------------------------------------------
// Injury flags and the admin review queue. The dashboard / check-in /
// session-log types that lived here went with the legacy daily-flow endpoints;
// the Today command view below is the live equivalent.
// ---------------------------------------------------------------------------

export type InjuryFlagSeverity = "mild" | "moderate" | "severe";
export type InjuryFlagStatus = "open" | "monitoring" | "resolved";
export type InjuryReportedStatus = "ongoing" | "improving" | "worse" | "resolved";
export type AdminReviewStatus = "pending" | "acknowledged" | "resolved";

export type InjuryFlagCreateRequest = {
  body_area?: string;
  description: string;
  severity?: InjuryFlagSeverity;
};

/** Canonical surface (skin) injury routing class, computed by the backend. The
 * UI never re-derives it — it only decides which follow-up to ask. */
export type SurfaceInjuryClass =
  | "non_surface"
  | "stable_surface"
  | "surface_local_restriction"
  | "surface_no_contact"
  | "surface_medical_review";

export type SkinIntegrity = "intact" | "open" | "unknown";
export type BleedingStatus = "none" | "controlled" | "uncontrolled";
export type Drainage = "none" | "present" | "unknown";
export type Coverable = "yes" | "no" | "unknown";
export type FrictionOrContactProblem = "yes" | "no" | "unknown";

/** One safety check the readiness engine ran, and what it concluded. */
export type TodaySafetyCheck = {
  code: string;
  label: string;
  result:
    | "no_session_change"
    | "local_protection_only"
    | "no_contact"
    | "direct_impact_removed"
    | "multiple_restrictions"
    | "medical_review";
  result_label: string;
};

/** Backend-owned current safety instruction. It is intentionally separate from
 * the session decision: active wound care can lead the UI without claiming a
 * future session has been blocked or modified. */
export type TodayPrimarySafetyNotice = {
  code:
    | "skin_care"
    | "skin_local_protection"
    | "skin_no_contact"
    | "skin_medical_review"
    | "head_impact_no_contact";
  injury_id: string;
  chip: "SKIN CARE" | "CHECK";
  title: string;
  detail: string;
  action: string;
  tone: "amber" | "red";
};

export type InjuryLoadRegion = "lower_limb" | "upper_limb" | "trunk_spine" | "head_neck" | "unknown";

export type InjuryFlagRecord = {
  id: string;
  athlete_id: string;
  episode_id?: string | null;
  side?: import("./api-schema.generated").InjuryFlagRecord["side"];
  rehab_decision?: { outcome: string; summary: string; reason_codes: string[];
    policy_id?: string;
    stage?: "calm" | "restore" | "load" | "dynamic" | "return";
    progression?: { reason_codes?: string[]; entry_basis?: "reported_clinician_permission"; authority_event_id?: string;
      next_transition?: { to_stage: string; status: string; target_stage_live: boolean;
      reason_codes: string[]; requirements?: Array<{ requirement_id: string; kind: string; status: string; reason_code: string }> } };
    achilles_load_review?: import("./api-schema.generated").AchillesRestoreLoadReview | null;
    prescription?: { sources?: string[] } | null;
    /** Recent rehab and injury answers for this episode, newest first. */
    recent_log?: Array<{ date: string; kind: "rehab" | "checkin"; label: string; felt?: string; next_day?: string;
      counts?: boolean | null }>;
    schedule?: { state: "due" | "recovery_day" | "already_completed" | "held" | "deferred" | "unsupported";
      reason: string; next_due_day?: string | null } | null } | null;
  clinician_clearance?: { episode_id: string; scopes: string[]; source: "athlete_reported"; externally_verified: false; rehabilitation_permission?: { schema_version: 1; level: "gentle_recovery" | "loading" | "sport_specific" | "not_cleared" } | null } | null;
  plan_id?: string | null;
  source: string;
  body_area: string;
  description: string;
  // Clean, athlete-facing label derived server-side from the injury synonym
  // logic (e.g. "Left wrist tightness"). Present on Today's open_injuries.
  label?: string;
  /** Backend-owned normalized anatomy. The client must not parse injury text. */
  canonical_location?: string | null;
  region_group?:
    | "lower_leg_foot"
    | "knee"
    | "hip_groin"
    | "lower_limb"
    | "upper_limb"
    | "spine_pelvis"
    | "head_face"
    | "unknown"
    | null;
  /** The stored location ("ankle", "lower back"), which rehab exposures match
   * on. The broad loading group is `load_region`. */
  body_region?: string | null;
  /** Today only: the broad group the safe-session rules compare against. */
  load_region?: InjuryLoadRegion | null;
  consequence?: "neuro" | "structural" | "load_sensitive" | null;
  severity: InjuryFlagSeverity;
  /** Who owns `severity`. `surface_system` means it is a floor the backend
   * derived from the wound answers, with the athlete's own value kept in
   * `manual_severity` so a later clean recheck can release it. Absent reads as
   * the athlete's own choice, which is never lowered automatically. */
  severity_source?: "manual" | "surface_system" | null;
  manual_severity?: InjuryFlagSeverity | null;
  status: InjuryFlagStatus;
  latest_reported_status?: InjuryReportedStatus;
  // Structured surface (skin) safety answers, captured by the conditional
  // follow-up when a skin injury is marked worse.
  skin_integrity?: SkinIntegrity | null;
  bleeding_status?: BleedingStatus | null;
  drainage?: Drainage | null;
  infection_signs?: string[];
  coverable?: Coverable | null;
  friction_or_contact_problem?: FrictionOrContactProblem | null;
  /** Backend-computed routing class; drives whether the "Worse" follow-up asks
   * the skin questions at all. */
  surface_class?: SurfaceInjuryClass | null;
  resolved_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type AdminReviewRecord = {
  id: string;
  athlete_id: string;
  athlete_email: string;
  athlete_name: string;
  adaptation_note_id?: string | null;
  injury_flag_id?: string | null;
  reason: string;
  status: AdminReviewStatus;
  resolution_notes: string;
  resolved_by: string;
  resolved_at?: string | null;
  created_at: string;
};

export type AdminReviewResolveRequest = {
  status: "acknowledged" | "resolved";
  resolution_notes?: string;
};

// ---------------------------------------------------------------------------
// Block 4 Today command view. This mirrors the normalized backend read model
// from /api/today; the UI must not parse raw structured_plan for this screen.
// ---------------------------------------------------------------------------

export type TodayRecommendationState =
  | "not_checked_in"
  | "train_as_planned"
  | "modify"
  | "pull_back";

export type TodayCompletionStatus = "not_started" | "started" | "done" | "modified" | "skipped";

export type TodayCheckinSleep = "poor" | "okay" | "good";
export type TodayCheckinBody = "flat" | "normal" | "sharp";
export type TodayCheckinPain = "none" | "manageable" | "high";
export type TodayCheckinPhase = "GPP" | "SPP" | "TAPER" | "REINTEGRATION";
export type TodayActiveInjury = "none" | "stable" | "worse";
export type TodayPreviousSession = "none" | "normal" | "very_hard";

export type TodayActivePlan = {
  id?: string;
  name?: string;
  status?: string;
  phase?: string;
  fight_date?: string;
  camp_type?: string;
  /** Event/competition name from the plan's intake, when the athlete gave one. */
  event_name?: string;
  /** Target fight weight (kg) from the plan's intake. Not current bodyweight. */
  target_weight_kg?: number;
};

export type TodaySession = {
  session_id?: string;
  session_relation?: "today" | "next" | string;
  session_type?: string | null;
  category?: string | null;
  support_insert_category?: string | null;
  stress_class?: string | null;
  title?: string;
  label?: string;
  weekday?: string;
  weekday_with_label?: string;
  calendar_date?: string | null;
  d_day?: number | null;
  day_label?: string;
  status?: string;
  reason?: string;
  coach_note?: string;
  coach_led_contact?: string;
  effective_load?: string;
  primary_focus?: string;
  emphasis?: string;
  estimated_duration?: string | number | null;
  duration_minutes?: number | null;
  planned_duration?: { value?: number | null; unit?: string | null; display?: string | null } | null;
  blocks?: StructuredBlock[] | null;
};

export type TodayCommandView = {
  exercise_media?: Record<string, ExerciseMedia>;
  effective_clinician_clearance?: {
    level: "rehab_only" | "train_no_contact" | "train_contact";
    scopes: string[];
    requires_update: boolean;
    limited_by: Array<{ injury_id: string; injury_episode_id: string; label: string }>;
  } | null;
  /** The backend's single answer to "is today's session blocked?". */
  session_hold?: { held: boolean; reason: string | null; source: "readiness" | "injury" | "check_in" | null } | null;
  live_prescription?: {
    revision: string;
    session: StructuredSession;
    safety_hold: boolean;
    training_hold_reason?: string;
    safety_hold_reason?: string;
    rehab_only?: boolean;
    frozen: boolean;
    changes: Array<{ action: string; reason?: string; injury_id?: string; restriction_reasons?: string[] }>;
  } | null;
  delayed_rehab_prompts?: Array<{
    exposure_id: string; injury_id: string; injury_episode_id: string; region: string;
    question: string; options: Array<"better" | "same" | "worse" | "not_sure">;
  }>;
  active_plan: TodayActivePlan;
  today: {
    training_day: string;
    recommendation_state: TodayRecommendationState;
    recommendation_reason?: string | null;
    // Authoritative decision tier computed by the backend. The banner and the
    // risk-watch footer both render from this so they cannot contradict.
    decision_tier?: "stop" | "pull_back" | "modify" | "green" | "not_checked_in";
    // True when today's scheduled session is a low-cost support / filler that an
    // injury hold does not apply to (mental cue, breathing/mobility reset).
    injury_hold_exempt?: boolean;
    /** What changed about the ATHLETE and set today's decision. Backend-derived
     * from the engine's own trigger codes. Empty until the athlete checks in. */
    recommendation_trigger_labels?: string[];
    /** The camp around the decision (fight week, taper, today's exposure). Only
     * ever changes how cautious the call is; never a claim anything is wrong. */
    recommendation_context_labels?: string[];
    /** Safety questions the engine ASSESSED, with their outcome. A stable skin
     * injury lands here with result "no_session_change" — it was checked and
     * changed nothing — so it never has to appear as a cause. Structured, so the
     * UI never reads prose to tell a check from a trigger. */
    recommendation_safety_checks?: TodaySafetyCheck[];
    /** The inputs the decision was made from, for the card's "Based on" line. */
    recommendation_sources?: string[];
    /** How much data the decision rests on. Data completeness, NOT predictive
     * accuracy — the product has no outcome data to calibrate against yet. */
    recommendation_confidence?: "high" | "moderate" | "low" | null;
    /** Names the missing input when confidence is below high. Empty at high. */
    recommendation_confidence_note?: string;
    /** Current safety guidance that outranks planning-only preview copy. */
    primary_safety_notice?: TodayPrimarySafetyNotice | null;
    warnings?: string[];
    next_session: TodaySession;
    session_scope: "today" | "next" | "none";
    session_label: string;
    completion_status: TodayCompletionStatus;
  };
  risk_watch: Array<{
    category: string;
    priority: number;
    icon: string;
    label: string;
    text: string;
    tone: string;
    timeframe?: "today" | "last_session" | "recent_sessions" | "active" | null;
  }>;
  open_injuries: InjuryFlagRecord[];
  week_summary: Record<string, unknown>;
  quick_actions: Array<{
    id: string;
    label: string;
    route: string;
  }>;
};

/** Plan-free when ``plan_id`` and ``phase`` are both omitted: the server
 * confirms there is no active plan and invents neither. */
export type TodayCheckinRequest = {
  plan_id?: string;
  sleep: TodayCheckinSleep;
  body: TodayCheckinBody;
  pain: TodayCheckinPain;
  phase?: TodayCheckinPhase;
  active_injury?: TodayActiveInjury;
  previous_session?: TodayPreviousSession;
  sharp_pain?: boolean;
  instability?: boolean;
  swelling?: boolean;
  neurological_symptoms?: boolean;
  illness_symptoms?: boolean;
  cannot_warm_into_movement?: boolean;
  worse_next_day_pain?: boolean;
};

export type TodayCheckinResponse = {
  training_day: string;
  recommendation_state: TodayRecommendationState;
  recommendation_reason: string;
  triggers: string[];
  warnings?: string[];
};

export type SparringIntensity = "light" | "medium" | "hard";
export type SparringPlannedIntensity = "hard" | "light" | "technical" | "contact";
export type SparringHeadContact = "none" | "light" | "heavy";
export type SparringLogSource = "contact" | "session" | "free";

/** A post-sparring entry. The server stamps the athlete-local training day,
 * or the linked manual session's own day. */
export type SparringLogRequest = {
  plan_id: string | null;
  session_id: string | null;
  source: SparringLogSource;
  /** The manually logged sparring session this report details. */
  external_session_id?: string | null;
  /** One per set of answers, so a retried save returns the original. */
  client_request_id?: string | null;
  /** Rises with each new set of answers, so a late older one never wins. */
  detail_revision?: number;
  planned_intensity: SparringPlannedIntensity | null;
  intensity: SparringIntensity;
  rounds_completed: number;
  round_seconds: number | null;
  head_contact: SparringHeadContact;
  rocked: boolean;
  notes: string;
};

export type SparringLogRecord = Omit<SparringLogRequest, "client_request_id" | "detail_revision"> & {
  id: string;
  /** The latest applied correction; a reopened report continues from it. */
  detail_revision?: number;
  athlete_id: string;
  training_day: string;
  created_at: string;
};

export type SparringLogResponse = {
  log: SparringLogRecord;
  review_created: boolean;
  safety_notice: string | null;
};

/** Sparring totals over the athlete-local days ending today (inclusive). */
export type SparringWindowSummary = {
  days: number;
  sessions: number;
  rounds: number;
  hard_rounds: number;
  hard_days: number;
  heavy_head_contact_sessions: number;
  rocked_count: number;
};

/** The athlete's own sparring log (newest first) with recent exposure. */
export type SparringLogHistoryResponse = {
  logs: SparringLogRecord[];
  current_training_day: string;
  last_7_days: SparringWindowSummary;
  last_28_days: SparringWindowSummary;
  last_hard_day: string | null;
  last_rocked_day: string | null;
};

export type ExerciseLogStatus = "as_prescribed" | "modified" | "skipped";
export type ExerciseLogReason = "equipment" | "fatigue" | "pain" | "felt_strong";
export type ExerciseLogMeasure = { value: number; unit: string };
export type ExerciseSetResult = { set_number?: number | null; reps?: number | null; load?: ExerciseLogMeasure | null; rpe?: number | null };

/** What the athlete did, in the block's own prescription shape. Only the
 * fields that apply to the exercise are sent. */
export type ExerciseLogActual = {
  set_entries?: ExerciseSetResult[] | null;
  sets?: number | null;
  reps?: number | null;
  rounds?: number | null;
  load?: ExerciseLogMeasure | null;
  duration?: ExerciseLogMeasure | null;
  work?: ExerciseLogMeasure | null;
  rest?: ExerciseLogMeasure | null;
  distance?: ExerciseLogMeasure | null;
  effort?: ExerciseLogEffort | null;
};
export type ExerciseLogEffort = { method?: "RPE"; value: number };

/** Logs one prescribed block of today's started session. The server resolves
 * the prescription and the training day from `block_id`; the plan is unchanged. */
export type ExerciseLogRequest = {
  plan_id: string;
  block_id: string;
  status: ExerciseLogStatus;
  actual?: ExerciseLogActual;
  reason?: ExerciseLogReason | null;
  notes?: string;
};

export type ExerciseLogRecord = {
  id: string;
  athlete_id: string;
  plan_id: string | null;
  session_id: string | null;
  block_id: string;
  exercise_key: string | null;
  training_day: string;
  status: ExerciseLogStatus;
  reason: ExerciseLogReason | null;
  /** The block's prescription fields as they were when it was logged. */
  prescribed: Record<string, unknown>;
  actual: ExerciseLogActual;
  notes: string;
  created_at: string;
  updated_at: string;
};

export type ExerciseLogResponse = { log: ExerciseLogRecord };

/** Several blocks logged at once (e.g. what the session timer recorded). All
 * entries are saved, or none are. */
export type ExerciseLogBatchRequest = {
  plan_id: string;
  entries: Omit<ExerciseLogRequest, "plan_id">[];
  /** Leave blocks already logged today untouched (the timer never overwrites a hand log). */
  keep_existing?: boolean;
};

export type ExerciseLogBatchResponse = { logs: ExerciseLogRecord[] };

/** The weight last logged for an exercise on an earlier day. */
export type ExerciseRecentLoad = {
  exercise_key: string | null;
  display_name: string;
  load: ExerciseLogMeasure;
  training_day: string;
};

export type ExerciseHistoryResponse = { logs: ExerciseLogRecord[]; next_offset: number | null };

export type ExerciseLogListResponse = {
  training_day: string;
  logs: ExerciseLogRecord[];
  /** The latest weight per exercise from earlier days, to carry forward. */
  recent_loads: ExerciseRecentLoad[];
  recent_performances: ExerciseLogRecord[];
};

export type TodaySessionCompletionRequest = {
  plan_id: string | null;
  session_id: string;
  status: TodayCompletionStatus;
  prescription_revision?: string;
  rehab_tracking?: "injury_checkin" | null;
  rehab_performance?: "done_as_shown" | "changed" | "stopped" | "skipped";
  /** Omitted for the Today flow (server resolves the athlete-local day). A
   * retro-log passes the explicit past day; the server enforces the 7-day
   * back-fill window. */
  training_day?: string | null;
  session_rpe?: number | null;
  pain_after?: number | null;
  modification_reason?: string;
  notes?: string;
};

/** How the injury felt during the rehab work. */
export type RehabDuringResponse = "better" | "same" | "worse" | "not_sure";

/** Whether the athlete had to back off because of it. */
export type RehabLimitResponse = "no" | "reduced" | "stopped";

/**
 * One injury's post-rehab question, exactly as the server framed it.
 *
 * The questions and their allowed answers both come from the server, so the
 * client never invents either. Present only when the completed session
 * contained rehab attributable to a known injury — a normal training session
 * returns an empty list and the athlete is asked nothing.
 */
export type RehabResponsePrompt = {
  injury_id: string;
  /** Server-issued immutable episode context; return verbatim with the answer. */
  injury_episode_id: string;
  /** "LEFT ANKLE" — the injury named, so multiple open injuries stay distinct. */
  injury_label: string;
  body_region: string;
  side: string;
  drill_ids: string[];
  during_question: string;
  during_options: RehabDuringResponse[];
  limit_question: string;
  limit_options: RehabLimitResponse[];
  /** Server-decided from the completed drills: calm-stage guidance was followed, not exercises. */
  guidance_only?: boolean;
};

export type TodaySessionCompletionResponse = {
  completion: TodaySessionCompletionRecord;
  completion_status: TodayCompletionStatus;
  landing_session_state: "none" | "resume" | "completed";
  rehab_response_prompts?: RehabResponsePrompt[];
};

export type PendingRehabResponseSet = {
  completion_id: string;
  plan_id: string;
  session_id: string;
  training_day: string;
  rehab_response_prompts: RehabResponsePrompt[];
};

export type PendingRehabResponsesResponse = {
  response_sets: PendingRehabResponseSet[];
  history_truncated: boolean;
};

/** The athlete's answers about one injury plus its server-issued episode context.
 *  Drill, side and demand are still re-resolved server-side on submit. */
export type RehabResponseAnswer = {
  injury_id: string;
  injury_episode_id: string;
  during_response: RehabDuringResponse;
  limit_response: RehabLimitResponse;
};

export type RehabResponseRequest = {
  plan_id: string;
  session_id: string;
  training_day?: string | null;
  answers: RehabResponseAnswer[];
};

export type RehabResponseResult = {
  recorded_exposure_ids: string[];
  recorded_injury_ids: string[];
};

/** One session-history row (the API adds a title from the owned plan when available). */
export type TodaySessionCompletionRecord = {
  id: string;
  athlete_id: string;
  plan_id: string | null;
  session_id: string;
  session_title?: string | null;
  training_day: string;
  status: TodayCompletionStatus;
  session_rpe?: number | null;
  pain_after?: number | null;
  modification_reason?: string;
  notes?: string;
  started_at?: string | null;
  completed_at?: string | null;
  created_at?: string;
  updated_at?: string;
};

/** One stored Today check-in row (mirrors TodayCheckinRecord on the API). */
export type TodayCheckinHistoryRecord = {
  id: string;
  athlete_id: string;
  /** Null for a plan-free check-in, which also has no phase. */
  plan_id: string | null;
  training_day: string;
  athlete_timezone?: string;
  sleep: TodayCheckinSleep;
  body: TodayCheckinBody;
  pain: TodayCheckinPain;
  phase: TodayCheckinPhase | null;
  active_injury?: TodayActiveInjury;
  previous_session?: TodayPreviousSession;
  sharp_pain?: boolean;
  instability?: boolean;
  swelling?: boolean;
  neurological_symptoms?: boolean;
  illness_symptoms?: boolean;
  cannot_warm_into_movement?: boolean;
  worse_next_day_pain?: boolean;
  recommendation_state: Exclude<TodayRecommendationState, "not_checked_in">;
  recommendation_reason?: string;
  recommendation_triggers?: string[];
  created_at?: string;
  updated_at?: string;
};

/** Live completion rows for one plan plus the server-authoritative current
 * training day (used to gate the retro-log window in the plan viewer). */
export type PlanCompletionsResponse = {
  completions: TodaySessionCompletionRecord[];
  current_training_day: string;
};

export type TodayInjuryCheckinStatus = "ongoing" | "improving" | "worse" | "resolved";

export type TodayInjuryDeclaration = {
  flag_id?: string | null;
  body_area?: string;
  description?: string;
  severity?: InjuryFlagSeverity;
  status?: TodayInjuryCheckinStatus;
  // Surface follow-up. Sent only when a known skin injury is marked worse; the
  // backend treats every field as optional.
  skin_integrity?: SkinIntegrity;
  bleeding_status?: BleedingStatus;
  drainage?: Drainage;
  infection_signs?: string[];
  coverable?: Coverable;
  friction_or_contact_problem?: FrictionOrContactProblem;
};

export type TodayInjuryCheckinRequest = {
  injuries: TodayInjuryDeclaration[];
};

export type TodayInjuryCheckinResponse = {
  open_injuries: InjuryFlagRecord[];
};

export type FeedbackSurface = "plan" | "daily_recommendation" | "session" | "global";
export type FeedbackCategory =
  | "plan_usefulness"
  | "recommendation_fit"
  | "recommendation_safety"
  | "session_review"
  | "bug_report"
  | "feature_request"
  | "safety_issue"
  | "general_feedback";
export type FeedbackResponseValue = "yes" | "no" | "unsafe";

// The three quick questions asked after a completed session. Each is optional
// on its own so the prompt stays short enough that testers keep completing
// sessions; the submission just has to carry something.
export type SessionFeedbackDifficulty = "too_easy" | "appropriate" | "too_hard";
export type SessionFeedbackInstructions = "clear" | "unclear";
export type SessionFeedbackPlanAccuracy = "felt_right" | "something_wrong";

export type SessionFeedbackAnswers = {
  difficulty?: SessionFeedbackDifficulty;
  instructions?: SessionFeedbackInstructions;
  plan_accuracy?: SessionFeedbackPlanAccuracy;
};

export type FeedbackRecord = {
  id: string;
  surface: FeedbackSurface;
  category: FeedbackCategory;
  response: FeedbackResponseValue | null;
  reason: string | null;
  comment: string;
  structured_response: SessionFeedbackAnswers;
  priority: "normal" | "safety";
  has_screenshot: boolean;
  created_at: string;
  updated_at: string;
};

export type AdminFeedbackRecord = FeedbackRecord & {
  submitted_by_profile_id: string;
  submitter_email: string;
  submitter_name: string;
  contact_allowed: boolean;
  plan_id: string | null;
  today_checkin_id: string | null;
  session_id: string | null;
  camp_phase: string | null;
  app_version: string;
  page_path: string;
  device_context: string;
  language: string;
  readiness_context: string[];
  injury_context: string[];
  readiness_snapshot: Record<string, unknown>;
  injury_snapshot: Record<string, unknown>;
  technical_context: Record<string, unknown>;
  screenshot_expires_at: string | null;
};

export type AdminFeedbackScreenshotAccess = {
  url: string;
  expires_in: number;
};

export type ContextualFeedbackRequest = {
  response: FeedbackResponseValue;
  reason?: string | null;
  comment?: string;
};

export type GlobalFeedbackRequest = {
  category: Extract<FeedbackCategory, "bug_report" | "feature_request" | "safety_issue" | "general_feedback">;
  description?: string;
  contact_allowed?: boolean;
  screenshot?: File | null;
};

export type SessionFeedbackRequest = SessionFeedbackAnswers & {
  plan_id: string;
  session_id: string;
  /** Omitted for the session just finished; sent when reviewing a retro-log. */
  training_day?: string;
  comment?: string;
  screenshot?: File | null;
};
