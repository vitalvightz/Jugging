import type { ReactNode } from "react";

import { formatAppDate, formatAppDateTime } from "@/lib/date-format";
import {
  EQUIPMENT_ACCESS_OPTIONS,
  KEY_GOAL_OPTIONS,
  PROFESSIONAL_STATUS_OPTIONS,
  STANCE_OPTIONS,
  TACTICAL_STYLE_OPTIONS,
  TECHNICAL_STYLE_OPTIONS,
  TRAINING_AVAILABILITY_OPTIONS,
  WEAK_AREA_OPTIONS,
} from "@/lib/intake-options";
import type { AdminAthleteRecord, PlanRequest } from "@/lib/types";

const FATIGUE_LEVEL_OPTIONS = [
  { value: "low", label: "Low" },
  { value: "moderate", label: "Moderate" },
  { value: "high", label: "High" },
];

type OverviewItem = {
  label: string;
  value: ReactNode;
  multiline?: boolean;
  emphasize?: boolean;
};

type OverviewSection = {
  kicker: string;
  title: string;
  items: OverviewItem[];
  wide?: boolean;
};

function getOptionLabel(options: { value: string; label: string }[], value: string): string {
  return options.find((option) => option.value === value)?.label ?? value;
}

function getOptionLabels(options: { value: string; label: string }[], values: string[] | undefined): string[] {
  return (values ?? []).map((value) => getOptionLabel(options, value)).filter(Boolean);
}

function formatValue(value: string | number | null | undefined, empty = "Not provided"): string {
  if (value == null) {
    return empty;
  }
  const normalized = String(value).trim();
  return normalized ? normalized : empty;
}

function formatOptionalValue(value: string | number | null | undefined): string | null {
  const normalized = formatValue(value, "").trim();
  return normalized ? normalized : null;
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) {
    return "Not available";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Not available";
  }
  return formatAppDateTime(value);
}

function formatDateOnly(value: string | null | undefined): string {
  if (!value) {
    return "Not available";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Not available";
  }
  return formatAppDate(value);
}

function formatMeasurement(value: number | null | undefined, unit: string): string | null {
  return value == null ? null : `${value} ${unit}`;
}

function formatList(values: string[], empty = "Not provided"): string {
  return values.length ? values.join(", ") : empty;
}

function toneClassName(tone?: "default" | "alt" | "success" | "warning"): string {
  switch (tone) {
    case "alt":
      return " athlete-profile-pill-alt";
    case "success":
      return " athlete-profile-pill-success";
    case "warning":
      return " athlete-profile-pill-warning";
    default:
      return "";
  }
}

function InlinePills({
  items,
  tone = "default",
  empty = "Not provided",
}: {
  items: string[];
  tone?: "default" | "alt" | "success" | "warning";
  empty?: string;
}) {
  if (!items.length) {
    return <span className="athlete-profile-empty-value">{empty}</span>;
  }

  return (
    <div className="athlete-profile-inline-pills">
      {items.map((item) => (
        <span key={item} className={`athlete-profile-pill athlete-profile-pill-compact${toneClassName(tone)}`}>
          {item}
        </span>
      ))}
    </div>
  );
}

function AthleteProfileSection({ kicker, title, items, wide = false }: OverviewSection) {
  return (
    <section className={`athlete-profile-overview-section${wide ? " athlete-profile-overview-section-wide" : ""}`.trim()}>
      <div className="athlete-profile-section-heading">
        <p className="kicker">{kicker}</p>
        <h3 className="review-card-title">{title}</h3>
      </div>
      <div className="review-detail-list athlete-profile-review-list">
        {items.map((item) => (
          <div
            key={item.label}
            className={`review-detail-row athlete-profile-review-row${item.multiline ? " athlete-profile-review-row-multiline" : ""}`.trim()}
          >
            <p className="review-detail-label">{item.label}</p>
            <div className={`review-detail-value${item.emphasize ? " athlete-profile-review-value-emphasis" : ""}`.trim()}>
              {item.value}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function buildOverviewSections(athlete: AdminAthleteRecord): OverviewSection[] {
  const intake = athlete.latest_intake ?? null;

  const accountItems: OverviewItem[] = [
    { label: "Full name", value: formatValue(athlete.full_name) },
    { label: "Email", value: formatValue(athlete.email) },
    { label: "Role", value: athlete.role === "admin" ? "Admin" : "Athlete" },
    { label: "Timezone", value: formatValue(athlete.athlete_timezone) },
    { label: "Locale", value: formatValue(athlete.athlete_locale) },
    { label: "Member since", value: formatTimestamp(athlete.created_at) },
    { label: "Last updated", value: formatTimestamp(athlete.updated_at) },
  ];

  const profileItems: OverviewItem[] = [
    ...(formatOptionalValue(intake?.athlete.age) ? [{ label: "Age", value: formatValue(intake?.athlete.age) }] : []),
    ...(formatMeasurement(intake?.athlete.height_cm, "cm")
      ? [{ label: "Height", value: formatMeasurement(intake?.athlete.height_cm, "cm") as string }]
      : []),
    ...(formatMeasurement(intake?.athlete.weight_kg, "kg")
      ? [{ label: "Current weight", value: formatMeasurement(intake?.athlete.weight_kg, "kg") as string }]
      : []),
    ...(formatMeasurement(intake?.athlete.target_weight_kg, "kg")
      ? [{ label: "Target weight", value: formatMeasurement(intake?.athlete.target_weight_kg, "kg") as string }]
      : []),
    { label: "Stance", value: formatValue(getOptionLabel(STANCE_OPTIONS, athlete.stance || "")) },
    {
      label: "Combat sport",
      value: <InlinePills items={getOptionLabels(TECHNICAL_STYLE_OPTIONS, athlete.technical_style)} />,
    },
    {
      label: "Tactical style",
      value: <InlinePills items={getOptionLabels(TACTICAL_STYLE_OPTIONS, athlete.tactical_style)} tone="alt" />,
    },
    {
      label: "Professional status",
      value: formatValue(getOptionLabel(PROFESSIONAL_STATUS_OPTIONS, athlete.professional_status || "")),
    },
    { label: "Record", value: formatValue(athlete.record) },
  ];

  const sections: OverviewSection[] = [
    { kicker: "Account", title: "Athlete account", items: accountItems },
    { kicker: "Profile", title: "Competition profile", items: profileItems },
  ];

  if (!intake) {
    sections.push({
      kicker: "Latest intake",
      title: "Plan setup not available yet",
      wide: true,
      items: [
        {
          label: "Status",
          value:
            athlete.plan_count > 0
              ? `This athlete has ${athlete.plan_count} saved ${athlete.plan_count === 1 ? "plan" : "plans"}, but the latest intake payload was not returned for this record.`
              : "No completed intake saved yet.",
          multiline: true,
        },
        {
          label: "Latest plan activity",
          value: athlete.latest_plan_created_at ? formatTimestamp(athlete.latest_plan_created_at) : "Not available",
        },
      ],
    });

    return sections;
  }

  sections.push({
    kicker: "Camp",
    title: "Latest camp setup",
    wide: true,
    items: [
      { label: "Fight date", value: formatDateOnly(intake.fight_date), emphasize: true },
      { label: "Rounds format", value: formatValue(intake.rounds_format), emphasize: true },
      {
        label: (intake.weekly_training_frequency ?? 0) > 7 ? "Sessions / week (legacy)" : "Weekly frequency (recorded)",
        value: formatValue(intake.weekly_training_frequency),
        emphasize: true,
      },
      {
        label: "Fatigue",
        value: formatValue(getOptionLabel(FATIGUE_LEVEL_OPTIONS, intake.fatigue_level || "")),
        emphasize: true,
      },
    ],
  });

  sections.push({
    kicker: "Training",
    title: "Availability and equipment",
    wide: true,
    items: [
      {
        label: "Training availability",
        value: <InlinePills items={getOptionLabels(TRAINING_AVAILABILITY_OPTIONS, intake.training_availability)} />,
      },
      {
        label: "Hard sparring days",
        value: <InlinePills items={getOptionLabels(TRAINING_AVAILABILITY_OPTIONS, intake.hard_sparring_days)} tone="warning" />,
      },
      {
        label: "Light Combat days",
        value: <InlinePills items={getOptionLabels(TRAINING_AVAILABILITY_OPTIONS, intake.support_work_days)} tone="alt" />,
      },
      {
        label: "Equipment access",
        value: <InlinePills items={getOptionLabels(EQUIPMENT_ACCESS_OPTIONS, intake.equipment_access)} tone="success" />,
      },
    ],
  });

  sections.push({
    kicker: "Goals and notes",
    title: "Planner context",
    wide: true,
    items: [
      {
        label: "Key goals",
        value: <InlinePills items={getOptionLabels(KEY_GOAL_OPTIONS, intake.key_goals)} tone="success" />,
      },
      {
        label: "Weak areas",
        value: <InlinePills items={getOptionLabels(WEAK_AREA_OPTIONS, intake.weak_areas)} tone="warning" />,
      },
      { label: "Injuries / restrictions", value: formatValue(intake.injuries), multiline: true },
      { label: "Training preference", value: formatValue(intake.training_preference), multiline: true },
      { label: "Mindset challenges", value: formatValue(intake.mindset_challenges), multiline: true },
      { label: "Extra notes", value: formatValue(intake.notes), multiline: true },
    ],
  });

  return sections;
}

export function AthleteProfileHero({ athlete }: { athlete: AdminAthleteRecord }) {
  return (
    <div className="section-heading athlete-profile-hero">
      <div className="athlete-profile-hero-copy">
        <div className="athlete-profile-title-row">
          <div>
            <p className="kicker">Athlete Profile</p>
            <h1>{athlete.full_name || athlete.email}</h1>
          </div>
          <span className="athlete-profile-role-badge">{athlete.role === "admin" ? "Admin view" : "Athlete"}</span>
        </div>
        <p className="muted">{athlete.email}</p>
      </div>

      <div className="athlete-profile-metric-strip">
        <article className="athlete-profile-metric">
          <p className="plan-meta-label">Saved plans</p>
          <p className="athlete-profile-metric-value">{athlete.plan_count}</p>
        </article>
        <article className="athlete-profile-metric athlete-profile-metric-accent">
          <p className="plan-meta-label">Latest activity</p>
          <p className="athlete-profile-metric-value athlete-profile-metric-value-small">
            {athlete.last_meaningful_activity_at ? formatTimestamp(athlete.last_meaningful_activity_at) : "No recorded activity"}
          </p>
        </article>
        <article className="athlete-profile-metric">
          <p className="plan-meta-label">Combat sports</p>
          <p className="athlete-profile-metric-copy">
            {formatList(getOptionLabels(TECHNICAL_STYLE_OPTIONS, athlete.technical_style), "No style saved")}
          </p>
        </article>
      </div>
    </div>
  );
}

export function AthleteProfileOverviewCard({ athlete }: { athlete: AdminAthleteRecord }) {
  const sections = buildOverviewSections(athlete);

  return (
    <section className="plan-summary-card athlete-profile-overview-card">
      <div className="plan-summary-header athlete-profile-overview-header">
        <div>
          <p className="kicker">Overview</p>
          <h2 className="plan-summary-title">Captured athlete profile</h2>
        </div>
        <p className="muted">
          Account, latest intake and planner context in one view.
        </p>
      </div>

      <div className="athlete-profile-overview-grid">
        {sections.map((section) => (
          <AthleteProfileSection key={section.title} {...section} />
        ))}
      </div>
    </section>
  );
}
