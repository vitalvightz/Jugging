import type { CustomSession } from "./api-schema.generated";
import type { MeResponse } from "./types";
import { hydratePlanRequest } from "./onboarding";
import { parseRoundsFormat } from "./rounds-format";
import { blankDose, exerciseIncomplete } from "./custom-plan-dose";
import { calendarMonday } from "./custom-plan-calendar";

const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] as const;
export const sessionNames = {
  mixed: "Training", strength_power: "Strength & Power", conditioning: "Conditioning",
  skill: "Light Technical", sparring: "Hard Sparring", recovery: "Recovery", rest: "Rest",
} as const;
export type SessionType = CustomSession["session_type"];
type EditableSession = Omit<CustomSession, "exercises" | "session_type"> & {
  session_type?: SessionType;
  exercises: Array<{ dose: CustomSession["exercises"][number]["dose"]; id: string }>;
};

export function savedRoundsFormat(me: MeResponse | null) {
  // Read stored evidence directly: hydratePlanRequest supplies a generic 3 x 3 fallback.
  for (const value of [me?.profile.onboarding_draft?.rounds_format, me?.latest_intake?.rounds_format]) {
    if (typeof value !== "string") continue;
    const parsed = parseRoundsFormat(value);
    const rounds = Number(parsed.roundCount), minutes = Number(parsed.roundDuration);
    if (rounds >= 1 && rounds <= 100 && minutes > 0 && minutes * 60 <= 36000)
      return { rounds, work_seconds: minutes * 60 };
  }
  return null;
}

function defaultName(name: string, previous: SessionType) {
  return ["training", "workout", "session", sessionNames[previous].toLowerCase(),
    ...(previous === "skill" ? ["light technical / skill"] : [])].includes(name.trim().toLowerCase());
}

/** Repairs only generic names and empty combat slots. Existing occurrences stay untouched. */
export function prepareCustomSession<T extends EditableSession>(session: T, me: MeResponse | null, previousType = session.session_type ?? "mixed"): T {
  const type = session.session_type ?? "mixed";
  const name = defaultName(session.name, previousType) ? sessionNames[type] : session.name;
  if (session.exercises.length || !["sparring", "skill"].includes(type)) return { ...session, name };
  const format = savedRoundsFormat(me);
  return { ...session, name, exercises: [{
    id: crypto.randomUUID(), origin: "manual", name: `${sessionNames[type]} rounds`,
    equipment: null, media_id: null, community_id: null, community_revision_id: null,
    notes: format ? "From your saved round format. Rest is an editable suggestion; adjust with your coach."
      : "Editable suggestion only: no saved round format. Set your rounds, duration and rest with your coach.",
    incomplete: false,
    coaching_cues: null, stop_rules: null,
    dose: { ...blankDose("intervals"), ...(format ?? { rounds: 3, work_seconds: 180 }), rest_seconds: 60 },
  }] };
}

export function customScheduleIssues(sessions: EditableSession[]): Array<{ sessionId?: string; message: string }> {
  const issues: Array<{ sessionId?: string; message: string }> = [];
  const weeklyCounts = new Map<string, number>();
  for (const session of sessions) {
    if (session.scheduled_date) {
      const week = calendarMonday(session.scheduled_date);
      weeklyCounts.set(week, (weeklyCounts.get(week) ?? 0) + 1);
    }
    const type = session.session_type ?? "mixed";
    const label = session.scheduled_date ? `${session.scheduled_date}’s ${sessionNames[type]}` : session.weekday ? `${session.weekday}’s ${sessionNames[type]}` : session.name;
    const add = (message: string) => issues.push({ sessionId: session.id, message });
    if (!session.weekday) add(`Choose a day for ${session.name}.`);
    if (type === "rest") {
      continue;
    } else if (!session.exercises.length && !["recovery", "skill", "sparring"].includes(type)) {
      add(`${label} session has no exercises.`);
    } else if (session.exercises.some(e => exerciseIncomplete(e))) {
      add(`Finish the exercise doses in ${label}.`);
    }
  }
  for (const [week, count] of weeklyCounts)
    if (count > 28) issues.push({ message: `The week of ${week} has ${count} sessions. Keep each week to 28 or fewer.` });
  if (!sessions.some(s => !["rest", "recovery"].includes(s.session_type ?? "mixed")))
    issues.push({ message: "Add at least one training day to your plan." });
  return issues;
}

export function customPlanError(error: unknown, fallback: string) {
  const message = error instanceof Error ? error.message : fallback;
  return message.replace(/\s*\(request[ _-]?id:\s*[^)]*\)/gi, "");
}

type Schedule = { session_types_by_day?: Record<string, string> };

/** Seed a new draft once. Support slots alone never declare skill work. */
export function customSchedulePrefill(me: MeResponse | null): CustomSession[] {
  const intake = hydratePlanRequest(me);
  const draft = me?.profile.onboarding_draft as Schedule | null | undefined;
  const latest = me?.latest_intake as (Schedule & object) | null | undefined;
  const types = draft?.session_types_by_day ?? latest?.session_types_by_day ?? {};
  const sameDay = (a: string, b: string) => a.toLowerCase().slice(0, 3) === b.toLowerCase().slice(0, 3);
  return weekdays.flatMap((weekday) => {
    const hard = intake.hard_sparring_days.some((day) => sameDay(day, weekday))
      || Object.entries(types).some(([day, type]) => sameDay(day, weekday) && type === "hard_spar");
    const technical = Object.entries(types).some(([day, type]) => sameDay(day, weekday) && type === "technical");
    return [
      ...(hard ? [{ name: "Hard Sparring", session_type: "sparring" as const }] : []),
      ...(technical ? [{ name: "Light Technical", session_type: "skill" as const }] : []),
    ].map((session) => prepareCustomSession({ ...session, id: crypto.randomUUID(), scheduled_date: null, weekday, exercises: [] }, me));
  });
}
