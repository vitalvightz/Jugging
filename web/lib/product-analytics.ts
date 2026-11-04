export type Coverage = {
  status: "not_measured" | "partial" | "measured" | "insufficient";
  measurable_athletes: number;
  eligible_athletes: number;
  fully_measured_athletes: number;
};
export type SessionMetrics = {
  planned_started: number;
  planned_completed: number;
  planned_modified: number;
  manual_logged: number;
  distinct_recorded: number;
  matched_completed: number;
};
export type PlanFunnel = {
  generated: number;
  generating_athletes: number;
  usable: number;
  review_required: number;
  pending: number;
  unusable: number;
  immature_usable: number;
  observed_opened: number | null;
  mature_usable: number;
  trained_within_7d: number;
  percent: number | null;
};
export type ActivitySummary = {
  signals?: string[];
  id: string;
  full_name?: string | null;
  email?: string | null;
  registered_at: string;
  last_seen?: string | null;
  latest_action?: { feature: string; action: string } | null;
  visit_days_7?: number;
  visit_days_30?: number;
  first_activity: string | null;
  last_activity: string | null;
  active_days_7: number;
  active_days_30: number;
  inactivity_days: number | null;
};
export type FeatureUsage = {
  coverage?: Coverage | null;
  completion_label?: string;
  completion?: {
    started: number;
    completed: number;
    percent: number | null;
  } | null;
  observed_early_exits?: number | null;
  feature: string;
  athletes: number | null;
  percent: number | null;
  uses: number | null;
  repeat_athletes: number | null;
  discovered: number | null;
  actions: Record<string, number>;
};
export type Retention = {
  eligible: number;
  returned: number;
  percent: number | null;
};
export type ProductReport = {
  tracking_health?: { healthy: boolean; configured?: boolean; last_success_at?: string | null } | null;
  tracking_since?: string | null;
  latest_browser_event?: string | null;
  browser_reporting_athletes?: number;
  seen_recently?: number;
  no_visit_7d?: number;
  visitors_today?: number;
  visitors_7d?: number;
  visitors_30d?: number;
  coverage: Coverage;
  session_metrics: SessionMetrics;
  plan_funnel: PlanFunnel;
  as_of: string;
  days: number;
  registered: number;
  ever_active: number;
  active: number;
  previous_active: number | null;
  dau: number;
  wau: number;
  mau: number;
  stickiness: number | null;
  average_active_days: number | null;
  new_active: number;
  returning_active: number;
  never_active: number;
  sessions_started: number;
  sessions_completed: number;
  observed_session_abandonments: number;
  inactive: Record<string, number>;
  retention: Record<string, Retention>;
  cohorts: {
    week: string;
    signups: number;
    retention: Record<string, Retention>;
  }[];
  trend: { day: string; athletes: number }[];
  features: FeatureUsage[];
  funnel: {
    coverage?: Coverage;
    measurement_note?: string;
    label: string;
    eligible: number;
    converted: number | null;
    percent: number | null;
    median_seconds: number | null;
  }[];
  conversions: {
    coverage?: Coverage;
    measurement_note?: string;
    label: string;
    eligible: number | null;
    converted: number | null;
    percent: number | null;
    median_seconds: number | null;
  }[];
  dropoffs: Record<string, number | null>;
  errors: {
    feature: string;
    category: string;
    count: number;
    affected_athletes: number;
    errors_per_athlete: number;
  }[];
  friction_followup: {
    feature: string;
    category: string;
    eligible_athletes: number;
    no_recorded_return_7d: number;
  }[];
  segments: {
    professional_status: string[];
    sports: string[];
    acquisition: string;
  };
  athletes: ActivitySummary[];
  athlete_total: number;
  offset: number;
};
export type AthleteActivity = {
  session_metrics: SessionMetrics;
  summary: ActivitySummary;
  features: FeatureUsage[];
  sessions_started: number;
  sessions_completed: number;
  timeline: {
    id: string;
    occurred_at: string;
    feature: string;
    action: string;
  }[];
  latest_plan_interactions: {
    id: string;
    occurred_at: string;
    action: string;
  }[];
  offset: number;
};
export const humanize = (value: string) => ({
  app_opened: "Opened or returned to the app",
  page_viewed: "Viewed a page",
  checkin_started: "Started a check-in",
  sparring_log_started: "Started a sparring log",
  operation_failed: "Action failed",
  frontend_error: "App error",
}[value] ?? value.replaceAll("_", " "));
export const percent = (value: number | null) =>
  value === null ? "Insufficient data" : `${value.toFixed(1)}%`;

export const athleteIdentity = (
  athlete: Pick<ActivitySummary, "id" | "full_name" | "email">,
) =>
  athlete.full_name?.trim() || athlete.email?.trim() || athlete.id.slice(0, 8);
export const measurementLabel = (coverage?: Coverage | null) =>
  coverage?.status === "not_measured"
    ? "Not measured"
    : coverage?.status === "partial"
      ? "Partial coverage"
      : coverage?.status === "insufficient"
        ? "Insufficient data"
        : "Measured";
export const coverageExplanation = (coverage: Coverage) =>
  `${measurementLabel(coverage)}: feature interactions have coverage for ${coverage.measurable_athletes} / ${coverage.eligible_athletes} athletes in this period. Earlier gaps cannot be reconstructed; events expire after 180 days.`;
