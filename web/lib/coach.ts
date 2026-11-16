export type Connection = {
  id: string;
  coach_id: string;
  athlete_id: string | null;
  invited_email: string;
  coach_name: string;
  status: "pending" | "accepted";
};
export type CoachSession = {
  id: string;
  title?: string | null;
  session_id?: string;
  plan_id?: string;
  day: string;
  type: string;
  status: string;
  source: string;
  rpe?: number | null;
  effort?: string;
  duration?: number;
  recorded_at: string;
};
export type Recovery = {
  day: string;
  sleep?: string;
  body?: string;
  readiness?: number;
  fatigue?: number;
  sleep_quality?: number;
  source: string;
};
export type Fighter = {
  id: string;
  connection_id: string;
  name: string;
  avatar_url: string | null;
  sport: string | null;
  today: string;
  timezone: string;
  fight_date: string | null;
  plan_id: string | null;
  plan_name: string | null;
  share_recovery: boolean;
  share_injuries: boolean;
  schedule: { day: string; id: string; type: string; title: string }[];
  sparring_reports?: {
    id: string;
    day: string;
    intensity: string;
    rounds: number;
    round_seconds: number | null;
    head_contact: string | null;
    rocked: boolean | null;
  }[];
  sessions: CoachSession[];
  recovery: Recovery[] | null;
  injuries:
    | {
        id: string;
        body_area: string;
        severity: string;
        status: string;
        updated_at: string;
      }[]
    | null;
};

// Use the existing session token explicitly. No persisted coach-data cache and
// no service-role key: the database checks the actor and current consent.
export async function coachRpc<T>(
  token: string,
  name: string,
  body: object = {},
): Promise<T> {
  const response = await fetch(
    `${process.env.NEXT_PUBLIC_SUPABASE_URL}/rest/v1/rpc/${name}`,
    {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        apikey: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
    },
  );
  if (!response.ok) {
    throw new Error(
      response.status === 401 || response.status === 403
        ? "Access unavailable."
        : "Something went wrong. Please retry.",
    );
  }
  return response.json() as Promise<T>;
}
export function dayOffset(day: string, offset: number): string {
  const date = new Date(`${day}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}
export function weekStart(day: string): string {
  const weekday = new Date(`${day}T12:00:00Z`).getUTCDay();
  return dayOffset(day, -((weekday + 6) % 7));
}
export function completed(session: CoachSession): boolean {
  return session.status === "done" || session.status === "modified";
}
export function fighterSummary(fighter: Fighter) {
  const { today } = fighter;
  const recent = dayOffset(today, -6);
  const monday = weekStart(today);
  const latest = fighter.recovery?.[0];
  const stale = latest && latest.day < recent;
  const tired = latest?.body === "flat" || (latest?.fatigue ?? 0) >= 4;
  const poorSleep = latest?.sleep === "poor";
  const recoveryConcern =
    latest &&
    !stale &&
    (poorSleep ||
      tired ||
      (latest.readiness !== undefined && latest.readiness <= 2) ||
      (latest.sleep_quality !== undefined && latest.sleep_quality <= 2));
  const weekSessions = fighter.sessions.filter(
    (s) => completed(s) && s.day >= monday && s.day <= today,
  ).length;
  return {
    weekSessions,
    active:
      weekSessions > 0 ||
      Boolean(
        fighter.sparring_reports?.some((s) => s.rounds > 0 && s.day >= monday),
      ),
    // The reason a coach should look at this fighter, or null.
    attention: fighter.injuries?.length
      ? `${fighter.injuries.length} open injury ${fighter.injuries.length === 1 ? "flag" : "flags"}`
      : fighter.sparring_reports?.some(
            (s) => s.day >= recent && (s.rocked || s.head_contact === "heavy"),
          )
        ? "Contact concern reported"
        : !recoveryConcern
          ? null
          : poorSleep && latest.body === "flat"
            ? "Poor sleep and fatigue reported"
            : poorSleep
              ? "Poor sleep reported"
              : tired
                ? "Fatigue reported"
                : "Low recovery reported",
    recovery: !fighter.share_recovery
      ? "Not shared"
      : !latest
        ? "No check-in"
        : stale
          ? "Older check-in"
          : recoveryConcern
            ? "Recovery concern"
            : "No concern reported",
    upcoming: Boolean(fighter.fight_date && fighter.fight_date >= today),
  };
}
export function humanize(value: string | null | undefined): string {
  return value
    ? value.replaceAll("_", " ").replace(/^./, (s) => s.toUpperCase())
    : "Not recorded";
}
