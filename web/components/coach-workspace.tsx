"use client";

import Link from "next/link";
import Image from "next/image";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { RequireAuth } from "@/components/auth-guard";
import { ManualExerciseLogger } from "@/components/manual-exercise-logger";
import { CoachFeedback } from "./coach-feedback";
import { LogTraining } from "@/components/log-training";
import { useAppSession } from "@/components/auth-provider";
import {
  SessionDetails,
  SessionRow,
  type SessionDetailsCache,
} from "@/components/session-details";
import { Skeleton } from "@/components/skeleton";
import { WeightTrendChart } from "@/components/weight-trend-chart";
import {
  getFighterHistory,
  getFighterSessionDetails,
  getFighterWeight,
  type getSessionDetails,
} from "@/lib/api";
import type { CoachFighterWeight } from "@/lib/api-schema.generated";
import { isSafeAvatarImageUrl } from "@/lib/avatar-image-url";
import {
  coachRpc,
  groupCoachCheckIns,
  coachCheckInValues,
  coachRecoveryTone,
  completed,
  connectionSharing,
  formatList,
  dayOffset,
  fighterSummary,
  humanize,
  weekStart,
  type Connection,
  type Fighter,
} from "@/lib/coach";
import { CALENDAR_WEEKDAYS } from "@/lib/plan-calendar";
import { formatAppDate } from "@/lib/date-format";
import {
  formatFastedState,
  formatWeight,
  formatWeightDelta,
  getBodyweightEntriesWithIndex,
  getBodyweightTrend,
  getDefaultBodyweightRange,
  getRangeStart,
  getTargetGap,
  type BodyweightRange,
} from "@/lib/nutrition-bodyweight";
import {
  sortTrainingHistory,
  type TrainingHistorySession,
} from "@/lib/training-history";
import styles from "./coach-workspace.module.css";
import summary from "./training-summary.module.css";
import weightStyles from "./weight-tracker.module.css";

export function CoachWorkspace({
  fighterId,
  connectionsOnly = false,
  view = "overview",
}: {
  fighterId?: string;
  connectionsOnly?: boolean;
  view?: "overview" | "fighters" | "plans" | "log";
}) {
  const { session, me, isMeHydrated } = useAppSession();
  const coach = me?.profile.role === "coach" || me?.profile.role === "admin";
  return (
    <RequireAuth>
      <div className={styles.workspace}>
        {!isMeHydrated || !session || !me ? (
          <Loading />
        ) : connectionsOnly || !coach ? (
          <CoachAccess
            key={`${session.user_id}:${session.access_token}`}
            token={session.access_token}
            coach={coach}
          />
        ) : (
          <Fighters
            key={`${session.user_id}:${session.access_token}:${fighterId}`}
            token={session.access_token}
            fighterId={fighterId}
            view={view}
          />
        )}
      </div>
    </RequireAuth>
  );
}

// Reloads on focus and every 30 seconds, so access an athlete has ended drops
// off the screen. A failed load clears the data instead of leaving it stale.
function useLive<T>(fetcher: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const latest = useRef(0);
  const reload = useCallback(async () => {
    const request = ++latest.current;
    try {
      const next = await fetcher();
      if (request !== latest.current) return;
      setData(next);
      setError("");
    } catch (e) {
      if (request !== latest.current) return;
      setData(null);
      setError(e instanceof Error ? e.message : "Something went wrong.");
    }
  }, [fetcher]);
  useEffect(() => {
    const requests = latest;
    const onFocus = () => void reload();
    const first = window.setTimeout(onFocus, 0);
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void reload();
    }, 30_000);
    window.addEventListener("focus", onFocus);
    return () => {
      ++requests.current;
      window.clearTimeout(first);
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [reload]);
  return { data, error, setError, reload };
}

function LoadState({
  error,
  loading,
  retry,
}: {
  error: string;
  loading: boolean;
  retry: () => void;
}) {
  return error ? (
    <div className={styles.error} role="alert">
      {error}
      <button className="ghost-button" type="button" onClick={retry}>
        Retry
      </button>
    </div>
  ) : loading ? (
    <Loading />
  ) : null;
}

function Loading() {
  return (
    <div className={styles.loading} role="status" aria-label="Loading">
      <Skeleton height={84} />
      <Skeleton height={148} />
      <Skeleton height={148} />
    </div>
  );
}

function CoachAccess({ token, coach }: { token: string; coach: boolean }) {
  const fetcher = useCallback(
    () => coachRpc<Connection[]>(token, "coach_connections_list"),
    [token],
  );
  const { data: connections, error, setError, reload } = useLive(fetcher);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  async function command(action: string, id?: string, connection?: Connection) {
    if (busy) return;
    setBusy(true);
    // Accepting, or choosing to share the rest, shares training, weight,
    // recovery and injury status together. Nothing else grants health data.
    const share = action === "accept" || action === "consent";
    try {
      await coachRpc(token, "coach_connection_command", {
        p_action: action === "logging" ? "consent" : action,
        p_id: id ?? null,
        p_email: action === "invite" ? email : null,
        p_recovery: share || connection?.share_recovery === true,
        p_injuries: share || connection?.share_injuries === true,
        p_weight: share || connection?.share_weight === true,
        p_logging: action === "accept" || action === "logging" || connection?.allow_session_logging === true,
      });
      if (action === "invite") setEmail("");
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      {coach ? (
        <Link className={styles.back} href="/coach">
          ← Coach workspace
        </Link>
      ) : null}
      <h1>Coach access</h1>
      {coach ? (
        <form
          className={styles.invite}
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            void command("invite");
          }}
        >
          <input
            required
            type="email"
            maxLength={254}
            aria-label="Athlete email"
            placeholder="Athlete email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <button className="cta" disabled={busy}>
            Invite
          </button>
        </form>
      ) : null}
      <LoadState error={error} loading={!connections} retry={reload} />
      {connections?.length === 0 ? (
        <p className={styles.empty}>
          {coach
            ? "No invitations yet. Athletes accept in Settings → Coach access."
            : "No coach invitations yet."}
        </p>
      ) : null}
      {connections?.map((c) => {
        const pending = c.status === "pending";
        const name = c.coach_name || "Your coach";
        const sharing = connectionSharing(c);
        return (
          <article className={styles.connection} key={c.id}>
            <div className={styles.rowCopy}>
              <strong>{coach ? c.invited_email : name}</strong>
              <small>
                {coach
                  ? pending
                    ? "Pending"
                    : "Connected"
                  : pending
                    ? "Accepting shares training, weight, recovery and injuries, and lets your coach add sessions. Disconnect anytime."
                    : `Connected · ${sharing.label}`}
              </small>
              {!coach && !pending && sharing.note ? (
                <small>{sharing.note}</small>
              ) : null}
            </div>
            <div className={styles.actions}>
              {!coach && !pending && sharing.missing.length ? (
                <button
                  className="cta"
                  disabled={busy}
                  onClick={() => void command("consent", c.id, c)}
                >
                  Share {formatList(sharing.missing)}
                </button>
              ) : null}
              {!coach && !pending && !c.allow_session_logging ? (
                <button className="cta" disabled={busy} onClick={() => void command("logging", c.id, c)}>
                  Allow session logging
                </button>
              ) : null}
              {!coach && pending ? (
                <button
                  className="cta"
                  disabled={busy}
                  onClick={() => void command("accept", c.id)}
                >
                  Accept
                </button>
              ) : null}
              <button
                className="ghost-button"
                disabled={busy}
                onClick={() =>
                  void command(
                    coach ? "remove" : pending ? "decline" : "revoke",
                    c.id,
                  )
                }
              >
                {coach
                  ? pending
                    ? "Cancel"
                    : "Remove"
                  : pending
                    ? "Decline"
                    : "Disconnect"}
              </button>
            </div>
          </article>
        );
      })}
    </>
  );
}

function Fighters({ token, fighterId, view }: { token: string; fighterId?: string; view: string }) {
  const fetcher = useCallback(
    () =>
      coachRpc<Fighter[]>(token, "coach_fighters", {
        p_athlete_id: fighterId ?? null,
      }),
    [token, fighterId],
  );
  const { data: fighters, error, reload } = useLive(fetcher);
  const loggingFighters = fighters?.filter((fighter) => fighter.allow_session_logging === true) ?? [];
  // Bumped when a session is logged, so the History list reads it at once.
  const [logged, setLogged] = useState(0);
  return (
    <>
      <div className={styles.pageHead}>
        {fighterId ? (
          <Link className={styles.back} href="/coach">
            ← All fighters
          </Link>
        ) : (
          <h1>{view === "fighters" ? "Your fighters" : view === "plans" ? "Plans" : view === "log" ? "Log Session" : "Your corner. In focus."}</h1>
        )}
        {view !== "plans" && loggingFighters.length ? (
          <LogTraining
            accessToken={token}
            autoOpen={view === "log"}
            variant="primary"
            fighters={loggingFighters.map((f) => ({
              id: f.id,
              name: f.name,
              today: f.today,
              sport: f.sport,
            }))}
            onSaved={() => {
              setLogged((count) => count + 1);
              void reload();
            }}
          />
        ) : null}
      </div>
      {view === "plans" ? <p className={styles.empty}>Plan assignment is coming soon.</p> : view === "log" && fighters && !loggingFighters.length ? <p className={styles.empty}>No fighters have authorised session logging.</p> : null}
      {fighterId ? <h1 className={styles.srOnly}>Fighter overview</h1> : null}
      <LoadState error={error} loading={!fighters} retry={reload} />
      {!fighters ? null : !fighterId ? (
        view === "plans" || view === "log" ? null : <CoachDashboard fighters={fighters} rosterOnly={view === "fighters"} />
      ) : fighters[0] ? (
        <FighterOverview
          fighter={fighters[0]}
          token={token}
          historyKey={logged}
        />
      ) : (
        <section className={styles.panel}>
          <h2>Fighter unavailable</h2>
          <p className={styles.empty}>Not connected, or sharing has ended.</p>
        </section>
      )}
    </>
  );
}

function CoachDashboard({ fighters, rosterOnly = false }: { fighters: Fighter[]; rosterOnly?: boolean }) {
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const rows = fighters.map((fighter) => ({
    fighter,
    summary: fighterSummary(fighter),
  }));
  const attention = rows.filter((row) => row.summary.attention);
  const upcoming = rows
    .filter(
      (row) =>
        row.summary.upcoming &&
        row.fighter.fight_date! <= dayOffset(row.fighter.today, 30),
    )
    .sort((a, b) => a.fighter.fight_date!.localeCompare(b.fighter.fight_date!));
  const visible = rows.filter(({ fighter }) =>
    `${fighter.name} ${fighter.sport ?? ""}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  return (
    <>
      {!rosterOnly ? <>
      <section className={styles.metrics} aria-label="Roster summary">
        {(
          [
            ["Fighters", rows.length],
            ["Active this week", rows.filter((r) => r.summary.active).length],
            ["Needs attention", attention.length],
            ["Fights in 30 days", upcoming.length],
          ] as const
        ).map(([label, value]) => (
          <article className={styles.metric} key={label}>
            <span>{label}</span>
            <strong
              className={
                label === "Needs attention" && value
                  ? styles.concern
                  : undefined
              }
            >
              {value}
            </strong>
          </article>
        ))}
      </section>
      {attention.length ? (
        <section className={styles.panel}>
          <h2>Needs attention</h2>
          <ul className={styles.roster}>
            {attention.map(({ fighter: f, summary: s }) => (
              <FighterRow
                key={f.id}
                fighter={f}
                lead={<Avatar fighter={f} />}
                detail={<span className={styles.concern}>{s.attention}</span>}
              />
            ))}
          </ul>
        </section>
      ) : null}
      {upcoming.length ? (
        <section className={styles.panel}>
          <h2>Upcoming fights</h2>
          <ul className={styles.roster}>
            {upcoming.map(({ fighter: f }) => (
              <FighterRow
                key={f.id}
                fighter={f}
                lead={
                  <span className={styles.countdown}>
                    <strong>{daysUntil(f.today, f.fight_date!)}</strong>
                    <small>days</small>
                  </span>
                }
                detail={`${humanize(f.sport)} · ${formatAppDate(f.fight_date!)}`}
              />
            ))}
          </ul>
        </section>
      ) : null}
      </> : null}
      <section className={styles.panel}>
        <div className={styles.sectionHeading}>
          <h2>Your fighters</h2>
          <div className={styles.actions}>
            {rows.length ? (
              <button
                type="button"
                className={styles.pill}
                aria-expanded={searchOpen}
                onClick={() => setSearchOpen(!searchOpen)}
              >
                Search
              </button>
            ) : null}
            <Link className={styles.pill} href="/coach/invitations">
              Invite
            </Link>
          </div>
        </div>
        {searchOpen ? (
          <input
            className={styles.search}
            type="search"
            aria-label="Search fighters"
            placeholder="Search name or sport"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        ) : null}
        {!visible.length ? (
          <p className={styles.empty}>
            {rows.length
              ? "No fighters match this search."
              : "Your corner starts here. Invite a fighter to get started."}
          </p>
        ) : (
          <ul className={styles.roster}>
            {visible.map(({ fighter: f, summary: s }) => (
              <FighterRow
                key={f.id}
                fighter={f}
                lead={<Avatar fighter={f} />}
                detail={[f.sport ? humanize(f.sport) : null, f.age != null ? `${f.age} years` : null, f.record, `${s.weekSessions} sessions`].filter(Boolean).join(" · ")}
                extra={f.last_training ? `Last trained ${formatAppDate(f.last_training.day)}` : undefined}
                end={
                  <span
                    className={
                      s.attention
                        ? styles.concern
                        : s.recovery === "No concern reported"
                          ? styles.positive
                          : styles.muted
                    }
                  >
                    {s.attention
                      ? "Needs attention"
                      : s.recovery === "No concern reported"
                        ? "On track"
                        : s.recovery}
                  </span>
                }
              />
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

function FighterRow({
  fighter: f,
  lead,
  detail,
  extra,
  end,
}: {
  fighter: Fighter;
  lead: ReactNode;
  detail: ReactNode;
  extra?: ReactNode;
  end?: ReactNode;
}) {
  return (
    <li>
      <Link href={`/coach/fighters/${f.id}`} className={styles.rowLink}>
        {lead}
        <span className={styles.rowCopy}>
          <strong>{f.name || "Fighter"}</strong>
          {detail ? <small>{detail}</small> : null}
          {extra ? <small>{extra}</small> : null}
        </span>
        {end}
      </Link>
    </li>
  );
}

function daysUntil(today: string, date: string) {
  return Math.max(
    0,
    Math.round((Date.parse(date) - Date.parse(today)) / 86400000),
  );
}

function Avatar({ fighter }: { fighter: Fighter }) {
  return isSafeAvatarImageUrl(fighter.avatar_url) ? (
    <Image
      unoptimized
      className={styles.avatar}
      src={fighter.avatar_url!}
      alt=""
      width={44}
      height={44}
    />
  ) : (
    <span className={styles.avatar} aria-hidden="true">
      {fighter.name
        .trim()
        .split(/\s+/)
        .map((p) => p[0])
        .slice(0, 2)
        .join("") || "F"}
    </span>
  );
}

function FighterOverview({
  fighter: f,
  token,
  historyKey,
}: {
  fighter: Fighter;
  token: string;
  historyKey: number;
}) {
  const summary = fighterSummary(f);
  const sharingKey = `${f.share_weight}:${f.share_recovery}:${f.share_injuries}:${f.allow_session_logging}`;
  const [allSparring, setAllSparring] = useState(false);
  const [allCheckIns, setAllCheckIns] = useState(false);
  const [week, setWeek] = useState(weekStart(f.today));
  const end = dayOffset(week, 6);
  const weekly = f.sessions.filter((s) => s.day >= week && s.day <= end);
  const done = weekly.filter(completed);
  const strength = f.schedule.filter(
    (s) =>
      s.day >= week &&
      s.day <= end &&
      ["strength_power", "conditioning"].includes(s.type),
  );
  const strengthDone = strength.filter((s) =>
    f.sessions.some(
      (c) =>
        c.session_id === s.id &&
        c.plan_id === f.plan_id &&
        c.day === s.day &&
        completed(c),
    ),
  ).length;
  const reports =
    f.sparring_reports?.filter((s) => s.day >= week && s.day <= end) ?? [];
  const rpe = done.flatMap((s) =>
    s.rpe !== null && s.rpe !== undefined ? [s.rpe] : [],
  );
  const minutes = done.reduce((total, s) => total + (s.duration ?? 0), 0);
  const campStart = f.schedule.map((s) => s.day).sort()[0];
  const campProgress =
    campStart && campStart <= f.today && f.fight_date && f.fight_date >= f.today && f.fight_date > campStart
      ? Math.max(
          0,
          Math.min(
            100,
            Math.round(
              ((Date.parse(f.today) - Date.parse(campStart)) /
                (Date.parse(f.fight_date) - Date.parse(campStart))) *
                100,
            ),
          ),
        )
      : null;
  const checkIns = groupCoachCheckIns(f.share_recovery ? f.recovery ?? [] : []);
  const latest = checkIns[0];
  const latestSleep = latest?.readings.find(row => row.sleep != null || row.sleep_quality != null);
  const latestBody = latest?.readings.find(row => row.body != null || row.readiness != null);
  const sleepTone = coachRecoveryTone(latestSleep?.sleep ? "sleep" : "sleep_quality", latestSleep?.sleep ?? latestSleep?.sleep_quality);
  const bodyTone = coachRecoveryTone("readiness", latestBody?.body ?? latestBody?.readiness);
  const warnings = coachCheckInValues(latest?.readings ?? []).filter(value => value.tone === "caution" || value.tone === "concern");
  const injuries = f.share_injuries ? f.injuries ?? [] : [];
  const injuryTone = injuries.some(injury => injury.severity === "severe") ? "concern" : injuries.some(injury => ["mild", "moderate"].includes(injury.severity)) ? "caution" : "unknown";
  const unknown = f.share_recovery ? "Not recorded" : "Not shared";
  return (
    <>
      <section className={styles.panel}>
        <div className={styles.identity}>
          <Avatar fighter={f} />
          <div className={styles.rowCopy}>
            <h2>{f.name || "Fighter"}</h2>
            {[f.sport ? humanize(f.sport) : null, f.age != null ? `${f.age} years` : null, f.record].filter(Boolean).length ?
              <small>{[f.sport ? humanize(f.sport) : null, f.age != null ? `${f.age} years` : null, f.record].filter(Boolean).join(" · ")}</small> : null}
            {[...new Set([...(f.technical_style ?? []), ...(f.tactical_style ?? []), f.stance].filter(value => value && value.trim().toLowerCase() !== f.sport?.trim().toLowerCase()))].length ?
              <small>{[...new Set([...(f.technical_style ?? []), ...(f.tactical_style ?? []), f.stance].filter(value => value && value.trim().toLowerCase() !== f.sport?.trim().toLowerCase()))].map(value => humanize(value)).join(" · ")}</small> : null}
            {f.last_training ? <small>Last training · {humanize(f.last_training.activity)} · {formatAppDate(f.last_training.day)}</small> : null}
            {summary.attention ? (
              <small className={styles.concern} data-recovery-tone={injuries.length ? injuryTone : undefined}>{summary.attention}</small>
            ) : null}
          </div>
        </div>
        <div className={styles.campGrid}>
          <div>
            <p className={styles.eyebrow}>Next fight</p>
            <p className={styles.bigNumber}>
              {summary.upcoming ? (
                <>
                  {daysUntil(f.today, f.fight_date!)}
                  <small>days</small>
                </>
              ) : (
                "—"
              )}
            </p>
            <small className={styles.muted}>
              {summary.upcoming
                ? formatAppDate(f.fight_date!)
                : "Not scheduled"}
            </small>
          </div>
          {campProgress !== null ? <div>
            <p className={styles.eyebrow}>Camp timeline</p>
            <strong>
              {campProgress !== null ? `${campProgress}% elapsed` : "—"}
            </strong>
            {campProgress !== null ? (
              <progress
                aria-label="Camp timeline elapsed"
                value={campProgress}
                max={100}
              />
            ) : null}
          </div> : null}
        </div>
      </section>
      <section aria-label="Weekly performance">
        <div className={styles.sectionHeading}>
          <h2>Weekly performance</h2>
          <div className={styles.weekControls}>
            <button
              type="button"
              className={styles.pill}
              aria-label="Previous week"
              disabled={week <= weekStart(dayOffset(f.today, -49))}
              onClick={() => { setWeek(dayOffset(week, -7)); setAllSparring(false); }}
            >
              Previous
            </button>
            <span>
              {week === weekStart(f.today) ? "This week" : formatAppDate(week)}
            </span>
            <button
              type="button"
              className={styles.pill}
              aria-label="Next week"
              disabled={week >= weekStart(f.today)}
              onClick={() => { setWeek(dayOffset(week, 7)); setAllSparring(false); }}
            >
              Next
            </button>
          </div>
        </div>
        <div className={styles.metrics}>
          {[
            ["Sessions", done.length],
            [
              "Average RPE",
              rpe.length
                ? (rpe.reduce((a, b) => a + b, 0) / rpe.length).toFixed(1)
                : "—",
            ],
            [
              "S&C completed",
              strength.length ? `${strengthDone}/${strength.length}` : "—",
            ],
            ["Minutes", minutes || "—"],
          ].map(([label, value]) => (
            <article className={styles.metric} key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
            </article>
          ))}
        </div>
      </section>
      {reports.length ? (
        <section className={styles.panel}>
          <div className={styles.sectionHeading}>
            <h2>Sparring</h2>
            <span className={styles.muted}>
              {reports.length} {reports.length === 1 ? "report" : "reports"}
            </span>
          </div>
          <p className={styles.muted}>Recent sparring activity</p>
          <div className={styles.sparringStats}>
            <div><strong>{reports.reduce((sum, report) => sum + report.rounds, 0)}</strong><small>Rounds</small></div>
            <div><strong>{reports.length}</strong><small>Reports</small></div>
            <div><strong>{reports.filter(report => report.intensity === "hard").length}</strong><small>Hard</small></div>
          </div>
          <ul className={styles.sparringReports}>
            {[...reports].sort((a, b) => b.day.localeCompare(a.day)).slice(0, allSparring ? undefined : 3).map((s) => (
              <li key={s.id}>
                <div className={styles.sectionHeading}><strong>{formatAppDate(s.day)}</strong>
                  <span className={styles.intensity} data-intensity={s.intensity}>{s.intensity === "medium" ? "Moderate" : humanize(s.intensity)}</span></div>
                <p>{s.rounds} rounds{f.share_injuries && s.head_contact != null ? ` · ${s.head_contact === "none" ? "No head contact" : `${humanize(s.head_contact)} head contact`}` : ""}</p>
                {f.share_injuries && s.rocked ? <p className={styles.rocked} role="note">⚠ Athlete reported being rocked</p> : null}
                <CoachFeedback key={`${sharingKey}:${s.id}`} token={token} athleteId={f.id} source="sparring" targetId={s.id} editable={f.can_leave_feedback === true} />
              </li>
            ))}
          </ul>
          {reports.length > 3 ? <button type="button" className="ghost-button" aria-expanded={allSparring} onClick={() => setAllSparring(!allSparring)}>{allSparring ? "Show recent reports" : "View all reports"}</button> : null}
        </section>
      ) : (
        <section className={styles.panel}>
          <h2>Sparring</h2>
          <p className={styles.empty}>No sparring reports this week</p>
        </section>
      )}
      {/* A new key when sharing changes drops everything these loaded
          (weigh-ins, session rows, cached session records, an open record
          sheet) and reloads it under the new permissions. */}
      <FightWeight key={`weight:${sharingKey}`} token={token} fighter={f} />
      <TrainingHistory
        key={`history:${sharingKey}:${historyKey}`}
        token={token}
        athleteId={f.id}
        today={f.today}
      />
      <section className={styles.panel}>
        <div className={styles.sectionHeading}><h2>Recovery</h2>{latest ? <small className={styles.muted}>Latest check-in · {formatAppDate(latest.day)}</small> : null}</div>
        {injuries.length ? <div className={styles.recoveryPriority}>
          <h3>Open injuries · {injuries.length}</h3>
          <ul className={styles.activity}>{injuries.map(injury => <li key={injury.id}>
            <strong>{humanize(injury.body_area)}</strong>
            <span data-recovery-tone={coachRecoveryTone("injury", injury.severity)}>{humanize(injury.severity)} · {humanize(injury.status)}</span>
          </li>)}</ul>
        </div> : !f.share_injuries ? <p className={styles.muted}>Injuries: Not shared</p> : null}
        {f.share_recovery && warnings.length ? <div className={styles.recoveryPriority}>
          <strong>Recovery concerns</strong>
          {warnings.map(value => <p key={value.label} data-recovery-tone={value.tone}>{value.label}</p>)}
        </div> : f.share_recovery && sleepTone === "good" && bodyTone === "good" ? <p data-recovery-tone="good">Recovery check-in: No new concerns</p> : null}
        <div className={styles.tiles}>
          <div><span>Sleep</span><strong data-recovery-tone={sleepTone}>{latestSleep?.sleep ? humanize(latestSleep.sleep) : latestSleep?.sleep_quality != null ? `${latestSleep.sleep_quality}/5` : unknown}</strong></div>
          <div><span>Readiness</span><strong data-recovery-tone={bodyTone}>{latestBody?.body ? humanize(latestBody.body) : latestBody?.readiness != null ? `${latestBody.readiness}/5` : unknown}</strong></div>
        </div>
        {f.share_recovery ? <>
          <h3>Recent check-ins</h3>
          {checkIns.length ? <CheckIns rows={allCheckIns ? checkIns : checkIns.slice(0, 3)} /> : <p className={styles.empty}>No check-ins recorded.</p>}
          {checkIns.length > 3 ? <button type="button" className="ghost-button" aria-expanded={allCheckIns} onClick={() => setAllCheckIns(!allCheckIns)}>{allCheckIns ? "Show recent check-ins" : "View all check-ins"}</button> : null}
        </> : <p className={styles.muted}>Recovery check-ins: Not shared</p>}
      </section>
    </>
  );
}

function CheckIns({ rows }: { rows: ReturnType<typeof groupCoachCheckIns> }) {
  return <ul className={styles.activity}>{rows.map(row => <li key={row.day}>
    <strong>{formatAppDate(row.day)}</strong>
    {coachCheckInValues(row.readings).map(value => <span key={value.label} data-recovery-tone={value.tone}>{value.label}</span>)}
  </li>)}</ul>;
}

function mergeHistory(before: TrainingHistorySession[], after: TrainingHistorySession[]) {
  return sortTrainingHistory([...new Map([...before, ...after].map(row => [`${row.source}:${row.id}`, row])).values()]);
}
function trainingTone(row: TrainingHistorySession) {
  if (row.status === "modified") return "modified";
  const category = row.training_category ?? row.activity ?? row.session_type;
  if (category === "sparring") return "sparring";
  if (["rehab", "recovery"].includes(category ?? "")) return "rehab";
  if (["strength", "strength_power", "conditioning"].includes(category ?? "")) return "strength";
  return "training";
}
const HISTORY_STEP = 10;

// The athlete's own History, read-only: the same rows and the same session
// record, read through the coach endpoints that check the connection.
function TrainingHistory({
  token,
  athleteId,
  today,
}: {
  today: string;
  token: string;
  athleteId: string;
}) {
  const [view, setView] = useState<"calendar" | "list">("calendar");
  const [month, setMonth] = useState(today.slice(0, 7));
  const [day, setDay] = useState(today);
  const [rows, setRows] = useState<TrainingHistorySession[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [visible, setVisible] = useState(HISTORY_STEP);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<TrainingHistorySession | null>(null);
  const [cache] = useState<SessionDetailsCache>(() => new Map());
  const [editing, setEditing] = useState<TrainingHistorySession | null>(null);
  const load = useCallback<typeof getSessionDetails>(
    (accessToken, source, id) =>
      getFighterSessionDetails(accessToken, athleteId, source, id),
    [athleteId],
  );
  // A failed read can mean access ended: drop the rows, the open record and
  // cached records rather than leaving them on screen.
  const fail = useCallback(() => {
    setRows(null);
    setNext(null);
    setSelected(null);
    setEditing(null);
    cache.clear();
    setError(true);
  }, [cache]);
  useEffect(() => {
    let active = true;
    getFighterHistory(token, athleteId)
      .then((page) => {
        if (!active) return;
        setRows(mergeHistory([], page.sessions));
        setNext(page.next_before_day ?? null);
      })
      .catch(() => {
        if (active) fail();
      });
    return () => {
      active = false;
    };
  }, [token, athleteId, attempt, fail]);
  async function more() {
    if (rows && next && visible + HISTORY_STEP > completedRows.length) {
      setBusy(true);
      try {
        const page = await getFighterHistory(token, athleteId, next);
        setRows(mergeHistory(rows, page.sessions));
        setNext(page.next_before_day ?? null);
      } catch {
        fail();
        return;
      } finally {
        setBusy(false);
      }
    }
    setVisible(visible + HISTORY_STEP);
  }
  // A history page ends on a whole training day. Fetch until the selected
  // month is covered, following the existing exclusive before-day cursor.
  useEffect(() => {
    setBusy(false);
    if (view !== "calendar" || !rows || !next || next <= `${month}-01`) return;
    let active = true;
    setBusy(true);
    getFighterHistory(token, athleteId, next).then(page => {
      if (!active) return;
      if (page.next_before_day && page.next_before_day >= next) throw Error("History cursor did not advance");
      setRows(current => mergeHistory(current ?? [], page.sessions));
      setNext(page.next_before_day ?? null);
    }).catch(() => { if (active) fail(); }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [view, month, rows, next, token, athleteId, fail]);
  const completedRows = (rows ?? []).filter(row => (row.status === "done" || row.status === "modified") && row.completion_origin !== "day_credit" && row.training_day <= today && !(row as TrainingHistorySession & { deleted_at?: unknown }).deleted_at);
  const shown = view === "list" ? completedRows.slice(0, visible) : completedRows.filter(row => row.training_day === day);
  const first = new Date(`${month}-01T12:00:00Z`);
  const offset = (first.getUTCDay() + 6) % 7;
  const count = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate();
  function changeMonth(delta: number) {
    const nextMonth = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + delta, 1)).toISOString().slice(0, 7);
    setMonth(nextMonth); setDay(`${nextMonth}-01`);
  }
  return (
    <section className={styles.panel}>
      <div className={styles.sectionHeading}><h2>Training history</h2>
        <div className={styles.actions} aria-label="History view">{(["calendar", "list"] as const).map(mode => <button key={mode} type="button" className={styles.pill} aria-pressed={view === mode} onClick={() => setView(mode)}>{mode === "calendar" ? "Calendar" : "List"}</button>)}</div>
      </div>
      {view === "calendar" && rows && !error ? <>
        <div className={styles.sectionHeading}>
          <strong>{first.toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" })}</strong>
          <div className={styles.actions}><button type="button" className={styles.pill} aria-label="Previous month" onClick={() => changeMonth(-1)}>‹</button>
            <button type="button" className={styles.pill} onClick={() => { setMonth(today.slice(0, 7)); setDay(today); }}>Today</button>
            <button type="button" className={styles.pill} aria-label="Next month" disabled={month >= today.slice(0, 7)} onClick={() => changeMonth(1)}>›</button></div>
        </div>
        <div className={styles.trainingCalendar} role="group" aria-label="Training calendar">
          {CALENDAR_WEEKDAYS.map(label => <small key={label}>{label.slice(0, 1)}</small>)}
          {Array.from({ length: offset }, (_, index) => <span key={`gap-${index}`} />)}
          {Array.from({ length: count }, (_, index) => {
            const iso = `${month}-${String(index + 1).padStart(2, "0")}`;
            const sessions = completedRows.filter(row => row.training_day === iso);
            return <button type="button" key={iso} aria-label={`${formatAppDate(iso)}: ${sessions.length} completed sessions`} aria-pressed={day === iso} aria-current={iso === today ? "date" : undefined} onClick={() => setDay(iso)}>
              {index + 1}<span className={styles.calendarDots}>{sessions.map(row => <i key={`${row.source}:${row.id}`} data-training-tone={trainingTone(row)} aria-label={trainingTone(row)} />)}</span>
            </button>;
          })}
        </div>
        <div className={styles.calendarLegend}>{["strength", "sparring", "rehab", "modified", "training"].map(tone => <span key={tone}><i data-training-tone={tone} />{humanize(tone)}</span>)}</div>
        {busy ? <p role="status" className={styles.muted}>Loading earlier sessions…</p> : null}
        <h3>{formatAppDate(day)}</h3>
        {!busy && !shown.length ? <p className={styles.empty}>No completed sessions recorded for this day.</p> : null}
      </> : null}
      {error ? (
        <div className={styles.error} role="alert">
          Unable to load training history.
          <button
            className="ghost-button"
            type="button"
            onClick={() => {
              setError(false);
              setVisible(HISTORY_STEP);
              setAttempt(attempt + 1);
            }}
          >
            Retry
          </button>
        </div>
      ) : !rows ? (
        <Loading />
      ) : view === "list" && !completedRows.length ? (
        <p className={styles.empty}>No completed sessions recorded yet.</p>
      ) : null}
      <div className={styles.history}>
        {[...new Set(shown.map((row) => row.training_day))].map((day) => (
          <section
            className="history-day-group"
            key={day}
            aria-label={formatAppDate(day)}
          >
            {view === "list" ? <h3>{formatAppDate(day)}</h3> : null}
            <ul className="history-list">
              {shown
                .filter((row) => row.training_day === day)
                .map((row) => (
                  <SessionRow
                    key={`${row.source}-${row.id}`}
                    row={row}
                    onOpen={() => setSelected(row)}
                  />
                ))}
            </ul>
          </section>
        ))}
      </div>
      {view === "list" && rows && !error && (visible < completedRows.length || next) ? (
        <button
          className={`ghost-button ${styles.showMore}`}
          type="button"
          disabled={busy}
          onClick={() => void more()}
        >
          {busy ? "Loading…" : "Show earlier sessions"}
        </button>
      ) : null}
      {editing ? createPortal(
        <ManualExerciseLogger key={`${athleteId}:${editing.id}`} token={token} session={editing} fighterId={athleteId}
          onClose={() => { setEditing(null); cache.clear(); setAttempt(value => value + 1); }} />,
        document.body,
      ) : null}
      {/* Outside the workspace so its styles never reach the record sheet. */}
      {selected
        ? createPortal(
            <SessionDetails
              key={`${selected.source}:${selected.id}`}
              row={selected}
              token={token}
              cache={cache}
              viewer="coach"
              onEditExercises={selected.coach_exercises_editable ? () => {
                setEditing(selected);
                setSelected(null);
              } : undefined}
              load={load}
              onDismiss={() => setSelected(null)}
            />,
            document.body,
          )
        : null}
    </section>
  );
}

const RANGES: { value: BodyweightRange; label: string }[] = [
  { value: "7D", label: "7 days" },
  { value: "30D", label: "30 days" },
  { value: "All", label: "All" },
];
const WEIGH_INS = 7;

// The athlete's own weigh-ins and target, shown as recorded. Progress only:
// nothing here suggests how to make weight.
function FightWeight({
  token,
  fighter: f,
}: {
  token: string;
  fighter: Fighter;
}) {
  const [weight, setWeight] = useState<CoachFighterWeight | null>(null);
  const [failed, setFailed] = useState(false);
  const [range, setRange] = useState<BodyweightRange | null>(null);
  const [picked, setPicked] = useState<number | null>(null);
  const [showAll, setShowAll] = useState(false);
  useEffect(() => {
    let active = true;
    getFighterWeight(token, f.id)
      .then((next) => {
        if (active) setWeight(next);
      })
      .catch(() => {
        if (!active) return;
        setWeight(null);
        setFailed(true);
      });
    return () => {
      active = false;
    };
  }, [token, f.id]);
  const entries = weight?.entries ?? [];
  const logged = getBodyweightEntriesWithIndex(entries).filter(
    (entry) => entry.date <= f.today,
  );
  const current = weight?.current_weight_kg ?? logged[0]?.weight_kg ?? null;
  const target = weight?.target_weight_kg ?? null;
  const gap = getTargetGap(current, target);
  const activeRange = range ?? getDefaultBodyweightRange(entries, f.today);
  const trend = getBodyweightTrend(entries, activeRange, f.today);
  const selectedIndex =
    picked !== null && picked < trend.length ? picked : trend.length - 1;
  const selected = trend[selectedIndex];
  const fightDays =
    f.fight_date && f.fight_date >= f.today
      ? daysUntil(f.today, f.fight_date)
      : null;
  return (
    <section className={styles.panel}>
      <div className={styles.sectionHeading}>
        <h2>Fight weight</h2>
        {logged[0] ? (
          <span className={styles.muted}>
            Last logged {formatAppDate(logged[0].date)}
          </span>
        ) : null}
      </div>
      {failed ? (
        <p className={styles.empty}>Unable to load weight.</p>
      ) : !weight ? (
        <Loading />
      ) : !weight.shared ? (
        <p className={styles.empty}>Not shared.</p>
      ) : current === null && target === null ? (
        <p className={styles.empty}>No weight logged yet.</p>
      ) : (
        <>
          <div className={styles.tiles}>
            {[
              ["Current", formatWeight(current)],
              ["Target", target === null ? "Not set" : formatWeight(target)],
              ...(gap === null
                ? []
                : [
                    [
                      gap === 0
                        ? "On target"
                        : gap > 0
                          ? "Above target"
                          : "Under target",
                      formatWeight(Math.abs(gap)),
                    ],
                  ]),
              ...(weight.rolling_7_day_average_weight != null
                ? [
                    [
                      "7-day average",
                      formatWeight(weight.rolling_7_day_average_weight),
                    ],
                  ]
                : []),
              ...(weight.weekly_pace_kg != null
                ? [["Per week", formatWeightDelta(weight.weekly_pace_kg)]]
                : []),
              ...(fightDays === null
                ? []
                : [["Days to fight", String(fightDays)]]),
            ].map(([label, value]) => (
              <div key={label}>
                <span>{label}</span>
                <strong>{value}</strong>
              </div>
            ))}
          </div>
          {logged.length ? (
            <div className={weightStyles.trendCard}>
              <div className={styles.sectionHeading}>
                <span className={styles.muted}>
                  {selected
                    ? `${formatWeight(selected.weight_kg)} · ${formatAppDate(selected.date)}`
                    : `No weights in the last ${activeRange === "7D" ? 7 : 30} days`}
                </span>
                <div
                  className={`${summary.toggle} ${weightStyles.rangeToggle}`}
                  role="group"
                  aria-label="Weight range"
                >
                  {RANGES.map((item) => (
                    <button
                      key={item.value}
                      type="button"
                      aria-pressed={activeRange === item.value}
                      onClick={() => {
                        setRange(item.value);
                        setPicked(null);
                      }}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              </div>
              {trend.length ? (
                <WeightTrendChart
                  trend={trend}
                  rangeStart={getRangeStart(activeRange, f.today)}
                  today={f.today}
                  targetKg={target}
                  selectedIndex={selectedIndex}
                  onSelect={setPicked}
                />
              ) : null}
            </div>
          ) : null}
          <ul className={styles.activity}>
            {(showAll ? logged : logged.slice(0, WEIGH_INS)).map(
              (entry, index) => {
                const previous = logged[index + 1];
                return (
                  <li key={entry.sourceIndex}>
                    <div>
                      <strong>{formatAppDate(entry.date)}</strong>
                      <small>
                        {[
                          entry.time,
                          entry.is_fasted == null
                            ? null
                            : formatFastedState(entry.is_fasted),
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </small>
                    </div>
                    <span>
                      <strong>{formatWeight(entry.weight_kg)}</strong>
                      {previous
                        ? ` ${formatWeightDelta(Math.round((entry.weight_kg - previous.weight_kg) * 10) / 10)}`
                        : ""}
                    </span>
                  </li>
                );
              },
            )}
          </ul>
          {logged.length > WEIGH_INS ? (
            <button
              className={`ghost-button ${styles.showMore}`}
              type="button"
              onClick={() => setShowAll(!showAll)}
            >
              {showAll ? "Show fewer" : `Show all ${logged.length} weigh-ins`}
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}
