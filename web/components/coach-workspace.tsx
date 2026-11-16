"use client";

import Link from "next/link";
import Image from "next/image";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { TileIcon } from "@/components/record-icons";
import { RequireAuth } from "@/components/auth-guard";
import { useAppSession } from "@/components/auth-provider";
import { isSafeAvatarImageUrl } from "@/lib/avatar-image-url";
import {
  coachRpc,
  completed,
  dayOffset,
  fighterSummary,
  formatDay,
  humanize,
  sessionLabel,
  weekStart,
  type Connection,
  type Fighter,
} from "@/lib/coach";
import styles from "./coach-workspace.module.css";

export function CoachWorkspace({
  fighterId,
  connectionsOnly = false,
}: {
  fighterId?: string;
  connectionsOnly?: boolean;
}) {
  const { session, me, isMeHydrated } = useAppSession();
  return (
    <RequireAuth>
      {isMeHydrated && session && me ? (
        <Workspace
          key={`${session.user_id}:${session.access_token}:${fighterId ?? "roster"}`}
          token={session.access_token}
          coach={me.profile.role === "coach"}
          fighterId={fighterId}
          connectionsOnly={connectionsOnly}
        />
      ) : (
        <p role="status">Restoring your workspace…</p>
      )}
    </RequireAuth>
  );
}

function Workspace({
  token,
  coach,
  fighterId,
  connectionsOnly,
}: {
  token: string;
  coach: boolean;
  fighterId?: string;
  connectionsOnly: boolean;
}) {
  const [fighters, setFighters] = useState<Fighter[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [query, setQuery] = useState("");
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const generation = useRef(0);
  const load = useCallback(async () => {
    const request = ++generation.current;
    try {
      const [nextConnections, nextFighters] = await Promise.all([
        coachRpc<Connection[]>(token, "coach_connections_list"),
        coach && !connectionsOnly
          ? coachRpc<Fighter[]>(token, "coach_fighters", {
              p_athlete_id: fighterId ?? null,
            })
          : Promise.resolve([]),
      ]);
      if (request !== generation.current) return;
      setConnections(nextConnections);
      setFighters(nextFighters);
      setError("");
    } catch (e) {
      if (request !== generation.current) return;
      setFighters([]);
      setConnections([]);
      setError(e instanceof Error ? e.message : "Unable to load workspace.");
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [token, coach, fighterId, connectionsOnly]);
  useEffect(() => {
    const lifecycle = generation;
    const initialLoad = window.setTimeout(() => {
      void load();
    }, 0);
    const onFocus = () => {
      void load();
    };
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, 30_000);
    window.addEventListener("focus", onFocus);
    return () => {
      ++lifecycle.current;
      window.clearTimeout(initialLoad);
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [load]);
  async function command(
    action: string,
    id?: string,
    recovery = false,
    injuries = false,
  ) {
    if (busy) return;
    ++generation.current;
    setBusy(true);
    setError("");
    setNotice("");
    // Clear displayed performance while permissions may be changing.
    setFighters([]);
    try {
      await coachRpc(token, "coach_connection_command", {
        p_action: action,
        p_id: id ?? null,
        p_email: action === "invite" ? email : null,
        p_recovery: recovery,
        p_injuries: injuries,
      });
      if (action === "invite") setEmail("");
      setNotice(
        action === "invite"
          ? "Invitation saved. Awaiting acceptance."
          : "Sharing updated.",
      );
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to update sharing.");
    } finally {
      setBusy(false);
    }
  }
  const fighter = fighters[0];
  const showRoster = coach && !connectionsOnly && !fighterId;
  return (
    <div className={styles.workspace}>
      <header className={fighterId ? styles.profileHeader : styles.header}>
        <div>
          <p className="kicker">
            {fighterId
              ? "Fighter overview"
              : coach
                ? "Coach workspace"
                : "Permissions"}
          </p>
          <h1 className={fighterId ? styles.srOnly : undefined}>
            {fighterId
              ? "Fighter overview"
              : connectionsOnly || !coach
                ? "Coach access"
                : "Your corner. In focus."}
          </h1>
          <p className={fighterId ? styles.srOnly : styles.muted}>
            {coach
              ? "Your team’s preparation at a glance."
              : "Your training. Your choice."}
          </p>
        </div>
        <div className={styles.actions}>
          {coach && !fighterId && !connectionsOnly ? (
            <Link className="cta" href="/settings/coach-access">
              Invite fighter
            </Link>
          ) : null}
          {fighterId ? (
            <Link className="ghost-button" href="/coach">
              ← All fighters
            </Link>
          ) : null}
          {coach && connectionsOnly ? (
            <Link className="ghost-button" href="/coach">
              Coach workspace
            </Link>
          ) : null}
          <button
            className="ghost-button"
            type="button"
            disabled={busy || loading}
            onClick={() => {
              setLoading(true);
              void load();
            }}
          >
            Refresh
          </button>
        </div>
      </header>
      {error ? (
        <div className="status-card" role="alert">
          {error}
        </div>
      ) : null}
      {notice ? <p role="status">{notice}</p> : null}
      {loading ? <p role="status">Loading authorised records…</p> : null}
      {!loading && !error && showRoster ? (
        <CoachDashboard fighters={fighters} query={query} setQuery={setQuery} />
      ) : null}
      {!loading && !error && fighterId ? (
        fighter ? (
          <FighterOverview fighter={fighter} />
        ) : (
          <section className={styles.panel}>
            <h2>Fighter unavailable</h2>
            <p>This fighter has not accepted access, or sharing has ended.</p>
          </section>
        )
      ) : null}
      {!fighterId ? (
        <details className={styles.panel} open={!coach || connectionsOnly}>
          <summary className={styles.manageSummary}>
            {coach ? "Manage connections" : "Your coaches"}
          </summary>

          {coach ? (
            <form
              className={styles.invite}
              onSubmit={(event: FormEvent) => {
                event.preventDefault();
                void command("invite");
              }}
            >
              <label>
                Athlete email
                <input
                  required
                  type="email"
                  maxLength={254}
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="fighter@example.com"
                />
              </label>
              <button className="cta" disabled={busy || loading}>
                Invite athlete
              </button>
            </form>
          ) : null}
          <p className="muted">
            {coach
              ? "Athletes accept in Settings → Coach access."
              : "Accepting shares your name, avatar, sport, fight date, programme schedule and recorded training. Recovery and injury status are separate, optional choices. Private notes and medical narratives are never shared."}
          </p>
          {!loading && connections.length === 0 ? (
            <p>
              No {coach ? "invitations or connections" : "coach requests"} yet.
            </p>
          ) : null}
          <div className={styles.connectionList}>
            {connections.map((c) => (
              <ConnectionRow
                key={`${c.id}:${c.share_recovery}:${c.share_injuries}`}
                connection={c}
                coach={coach}
                busy={busy}
                command={command}
              />
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}

function CoachDashboard({
  fighters,
  query,
  setQuery,
}: {
  fighters: Fighter[];
  query: string;
  setQuery: (value: string) => void;
}) {
  const rows = fighters.map((fighter) => ({
    fighter,
    summary: fighterSummary(fighter),
  }));
  const attention = rows.filter((row) => row.summary.attention);
  const upcoming = rows
    .filter((row) => row.summary.upcoming)
    .sort((a, b) => a.fighter.fight_date!.localeCompare(b.fighter.fight_date!));
  const visible = rows.filter(({ fighter }) =>
    `${fighter.name} ${fighter.sport ?? ""}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  return (
    <>
      <section className={styles.metrics} aria-label="Roster summary">
        {[
          ["Total fighters", rows.length, "Connected athletes"],
          [
            "Active fighters",
            rows.filter((r) => r.summary.active).length,
            "Logged training · 7 days",
          ],
          ["Needs attention", attention.length, "Reported concerns"],
          ["Upcoming fights", upcoming.length, "Scheduled fights"],
        ].map(([label, value, help]) => (
          <article className={styles.metric} key={label}>
            <span>{label}</span>
            <strong
              className={
                label === "Needs attention" && Number(value) > 0
                  ? styles.concern
                  : undefined
              }
            >
              {value}
            </strong>
            <small>{help}</small>
          </article>
        ))}
      </section>
      <section className={styles.panel}>
        <div className={styles.sectionHeading}>
          <div>
            <p className={styles.eyebrow}>Priority</p>
            <h2>Needs attention</h2>
          </div>
          <span className={styles.flag}>
            {attention.length} {attention.length === 1 ? "fighter" : "fighters"}
          </span>
        </div>
        {attention.length ? (
          <ul className={styles.roster}>
            {attention.map(({ fighter: f }) => (
              <li key={f.id}>
                <Link
                  href={`/coach/fighters/${f.id}`}
                  className={styles.rowLink}
                >
                  <span className={styles.signal}>
                    <TileIcon name="pain" />
                  </span>
                  <span className={styles.rowCopy}>
                    <strong>{f.name || "Fighter"}</strong>
                    <small>{attentionReason(f)}</small>
                  </span>
                  <span className={styles.rowEnd}>
                    <TileIcon name="source" />
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className={styles.empty}>No shared concerns reported.</p>
        )}
      </section>
      <section className={styles.panel}>
        <div className={styles.sectionHeading}>
          <div>
            <p className={styles.eyebrow}>Schedule</p>
            <h2>Upcoming fights</h2>
          </div>
          <span className={styles.quietIcon}>
            <TileIcon name="time" />
          </span>
        </div>
        {upcoming.length ? (
          <ul className={styles.roster}>
            {upcoming.map(({ fighter: f }) => (
              <li key={f.id}>
                <Link
                  href={`/coach/fighters/${f.id}`}
                  className={styles.rowLink}
                >
                  <span className={styles.countdown}>
                    <strong>{daysUntil(f.today, f.fight_date!)}</strong>
                    <small>days</small>
                  </span>
                  <span className={styles.rowCopy}>
                    <strong>{f.name || "Fighter"}</strong>
                    <small>
                      {humanize(f.sport)} · {formatDay(f.fight_date)}
                    </small>
                    {f.plan_name ? (
                      <small className={styles.concern}>{f.plan_name}</small>
                    ) : null}
                  </span>
                  <span className={styles.pill}>View</span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className={styles.empty}>No upcoming fights.</p>
        )}
      </section>
      <section className={styles.panel}>
        <div className={styles.sectionHeading}>
          <h2>Your fighters</h2>
          <small className={styles.muted}>{rows.length} connected</small>
        </div>
        <label className={styles.search}>
          <span className={styles.srOnly}>Search fighters</span>
          <input
            type="search"
            placeholder="Search name or sport"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        {!rows.length ? (
          <div className={styles.empty}>
            <strong>Your corner starts here.</strong>
            <p>Invite a fighter to get started.</p>
          </div>
        ) : !visible.length ? (
          <p className={styles.empty}>No fighters match this search.</p>
        ) : (
          <ul className={styles.roster}>
            {visible.map(({ fighter: f, summary: s }) => (
              <li key={f.id}>
                <Link
                  className={styles.rowLink}
                  href={`/coach/fighters/${f.id}`}
                >
                  <Avatar fighter={f} />
                  <span className={styles.rowCopy}>
                    <strong>{f.name || "Fighter"}</strong>
                    <small>
                      {humanize(f.sport)} · {s.weekSessions} sessions this week
                    </small>
                    <small>
                      Last activity:{" "}
                      {s.lastActivity
                        ? formatDay(s.lastActivity)
                        : "Not recorded"}
                      {s.upcoming ? ` · Fight: ${formatDay(f.fight_date)}` : ""}
                    </small>
                  </span>
                  <span className={styles.rosterState}>
                    <span
                      className={s.attention ? styles.concern : styles.muted}
                    >
                      {s.attention ? "Needs attention" : s.recovery}
                    </span>
                    <small>
                      {s.recoveryDay ? formatDay(s.recoveryDay) : ""}
                    </small>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
        <details className={styles.method}>
          <summary>How counts work</summary>
          <p>
            Weeks start Monday in the athlete’s timezone. Completed and modified
            sessions count; day credits do not. Missing logs are not missed
            training.
          </p>
        </details>
      </section>
    </>
  );
}
function daysUntil(today: string, date: string) {
  return Math.max(
    0,
    Math.round((Date.parse(date) - Date.parse(today)) / 86400000),
  );
}
function attentionReason(f: Fighter): string {
  if (f.injuries?.length)
    return `${f.injuries.length} open injury ${f.injuries.length === 1 ? "flag" : "flags"}`;
  if (
    f.sparring_reports?.some(
      (s) =>
        s.day >= dayOffset(f.today, -6) &&
        (s.rocked || s.head_contact === "heavy"),
    )
  )
    return "Contact concern reported";
  const r = f.recovery?.[0];
  if (r?.sleep === "poor" && r.body === "flat")
    return "Poor sleep and fatigue reported";
  if (r?.sleep === "poor") return "Poor sleep reported";
  if (r?.body === "flat" || (r?.fatigue ?? 0) >= 4) return "Fatigue reported";
  return "Low recovery reported";
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
function ConnectionRow({
  connection: c,
  coach,
  busy,
  command,
}: {
  connection: Connection;
  coach: boolean;
  busy: boolean;
  command: (
    action: string,
    id?: string,
    recovery?: boolean,
    injuries?: boolean,
  ) => Promise<void>;
}) {
  const [recovery, setRecovery] = useState(c.share_recovery);
  const [injuries, setInjuries] = useState(c.share_injuries);
  return (
    <article className={styles.connection}>
      <div>
        <strong>{coach ? c.invited_email : c.coach_name || "Coach"}</strong>
        <p>
          {humanize(c.status)}
          {coach && c.status === "accepted"
            ? ` · Recovery ${c.share_recovery ? "shared" : "private"} · Injury status ${c.share_injuries ? "shared" : "private"}`
            : ""}
        </p>
      </div>
      {coach ? (
        <button
          className="ghost-button"
          disabled={busy}
          onClick={() => void command("remove", c.id)}
        >
          {c.status === "pending" ? "Cancel invitation" : "Remove fighter"}
        </button>
      ) : (
        <>
          <fieldset disabled={busy} className={styles.consent}>
            <legend>
              Optional sharing with {c.coach_name || "this coach"}
            </legend>
            <label>
              <input
                type="checkbox"
                checked={recovery}
                onChange={(e) => setRecovery(e.target.checked)}
              />{" "}
              Recovery & readiness (sleep, fatigue and readiness check-ins)
            </label>
            <label>
              <input
                type="checkbox"
                checked={injuries}
                onChange={(e) => setInjuries(e.target.checked)}
              />{" "}
              Injury & contact safety (body area, severity, open flags and
              sparring contact reports; no medical notes)
            </label>
          </fieldset>
          <div className={styles.actions}>
            {c.status === "pending" ? (
              <>
                <button
                  className="cta"
                  disabled={busy}
                  onClick={() =>
                    void command("accept", c.id, recovery, injuries)
                  }
                >
                  Accept & share training
                </button>
                <button
                  className="ghost-button"
                  disabled={busy}
                  onClick={() => void command("decline", c.id)}
                >
                  Decline
                </button>
              </>
            ) : (
              <>
                <button
                  className="ghost-button"
                  disabled={
                    busy ||
                    (recovery === c.share_recovery &&
                      injuries === c.share_injuries)
                  }
                  onClick={() =>
                    void command("consent", c.id, recovery, injuries)
                  }
                >
                  Save sharing
                </button>
                <button
                  className="ghost-button"
                  disabled={busy}
                  onClick={() => void command("revoke", c.id)}
                >
                  Revoke all access
                </button>
              </>
            )}
          </div>
        </>
      )}
    </article>
  );
}

function FighterOverview({ fighter: f }: { fighter: Fighter }) {
  const summary = fighterSummary(f);
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
  const sparring = weekly.filter(
    (s) =>
      s.type === "sparring" ||
      f.schedule.some(
        (p) =>
          p.type === "sparring" &&
          p.id === s.session_id &&
          p.day === s.day &&
          f.plan_id === s.plan_id,
      ),
  );
  const rpe = done.flatMap((s) =>
    s.rpe !== null && s.rpe !== undefined ? [s.rpe] : [],
  );
  const averageRpe = rpe.length
    ? (rpe.reduce((a, b) => a + b, 0) / rpe.length).toFixed(1)
    : "—";
  const minutes = done.reduce((total, s) => total + (s.duration ?? 0), 0);
  const campStart = f.schedule.map((s) => s.day).sort()[0];
  const campProgress =
    campStart && f.fight_date && f.fight_date > campStart
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
  const latest = f.recovery?.[0];
  const reports =
    f.sparring_reports?.filter((s) => s.day >= week && s.day <= end) ?? [];
  return (
    <>
      <section className={styles.panel}>
        <div className={styles.identity}>
          <Avatar fighter={f} />
          <div className={styles.rowCopy}>
            <h2>{f.name || "Fighter"}</h2>
            <small>
              {humanize(f.sport)}
              {f.plan_name ? ` · ${f.plan_name}` : ""}
            </small>
          </div>
          {summary.attention ? (
            <span className={styles.concern}>
              <TileIcon name="pain" />
            </span>
          ) : null}
        </div>
        <div className={styles.campGrid}>
          <div>
            <p className={styles.eyebrow}>Next fight</p>
            <p className={styles.bigNumber}>
              {summary.upcoming ? daysUntil(f.today, f.fight_date!) : "—"}
              <small>{summary.upcoming ? "days" : "No date"}</small>
            </p>
            <small className={styles.muted}>
              {summary.upcoming ? formatDay(f.fight_date) : "Not scheduled"}
            </small>
          </div>
          <div>
            <p className={styles.eyebrow}>Camp timeline</p>
            <strong>
              {campProgress !== null
                ? `${campProgress}% elapsed`
                : "Not available"}
            </strong>
            {campProgress !== null ? (
              <progress
                aria-label="Camp timeline elapsed"
                value={campProgress}
                max={100}
              />
            ) : null}
            <small className={styles.muted}>
              {f.plan_name || "No active programme"}
            </small>
          </div>
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
              onClick={() => setWeek(dayOffset(week, -7))}
            >
              Previous
            </button>
            <span>
              {week === weekStart(f.today) ? "This week" : formatDay(week)}
            </span>
            <button
              type="button"
              className={styles.pill}
              aria-label="Next week"
              disabled={week >= weekStart(f.today)}
              onClick={() => setWeek(dayOffset(week, 7))}
            >
              Next
            </button>
          </div>
        </div>
        <div className={styles.performance}>
          {[
            [done.length, "Sessions completed"],
            [averageRpe, "Average RPE"],
            [
              strength.length ? `${strengthDone}/${strength.length}` : "—",
              "S&C completed",
            ],
            [
              minutes || "—",
              minutes ? "Recorded minutes" : "Minutes not recorded",
            ],
          ].map(([value, label]) => (
            <article className={styles.metric} key={label}>
              <strong>{value}</strong>
              <small>{label}</small>
            </article>
          ))}
        </div>
      </section>
      <section className={styles.panel}>
        <div className={styles.sectionHeading}>
          <h2>Training history</h2>
          <span className={styles.quietIcon}>
            <TileIcon name="effort" />
          </span>
        </div>
        <SessionList
          fighter={f}
          sessions={weekly.slice(0, 4)}
          empty="No sessions recorded this week."
        />
        <details className={styles.sessionDetails}>
          <summary>View session details</summary>
          <p className={styles.footnote}>
            {formatDay(week)} – {formatDay(end)}
          </p>
          <SessionList
            fighter={f}
            sessions={weekly}
            empty="No recorded sessions."
          />
          <h3>Recent activity</h3>
          <SessionList
            fighter={f}
            sessions={f.sessions.slice(0, 8)}
            empty="No recent activity."
          />
        </details>
      </section>
      <section className={styles.panel}>
        <div className={styles.sectionHeading}>
          <h2>Recovery monitoring</h2>
          <span className={styles.concern}>
            <TileIcon name="source" />
          </span>
        </div>
        <p className={styles.footnote}>
          Latest check-in ·{" "}
          {latest
            ? formatDay(latest.day)
            : f.share_recovery
              ? "Not recorded"
              : "Not shared"}
        </p>
        <p className={summary.attention ? styles.concern : styles.muted}>
          {summary.attention ? "Recovery needs attention" : summary.recovery}
        </p>
        <div className={styles.recoveryTiles}>
          {[
            [
              "Sleep",
              latest?.sleep
                ? humanize(latest.sleep)
                : latest?.sleep_quality
                  ? `${latest.sleep_quality}/5`
                  : f.share_recovery
                    ? "Not recorded"
                    : "Not shared",
            ],
            [
              "Readiness",
              latest?.body
                ? humanize(latest.body)
                : latest?.readiness
                  ? `${latest.readiness}/5`
                  : f.share_recovery
                    ? "Not recorded"
                    : "Not shared",
            ],
            [
              "Injuries",
              !f.share_injuries
                ? "Not shared"
                : f.injuries?.length
                  ? `${f.injuries.length} open flags`
                  : "None reported",
            ],
          ].map(([label, value]) => (
            <div key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
            </div>
          ))}
        </div>
        <details className={styles.method}>
          <summary>Check-ins & sharing</summary>
          <p>Only authorised health data is visible.</p>
          {!f.share_recovery ? (
            <p>Recovery information is not shared.</p>
          ) : (
            <ul className={styles.activity}>
              {f.recovery?.slice(0, 14).map((r, i) => (
                <li key={`${r.day}:${i}`}>
                  <strong>{formatDay(r.day)}</strong>
                  <span>
                    {r.sleep
                      ? `Sleep: ${r.sleep} · Body: ${r.body}`
                      : `Readiness: ${r.readiness}/5 · Fatigue: ${r.fatigue}/5`}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {!f.share_injuries ? (
            <p>Injury information is not shared.</p>
          ) : f.injuries?.length ? (
            <ul className={styles.activity}>
              {f.injuries.map((i) => (
                <li key={i.id}>
                  <strong>{humanize(i.body_area)}</strong>
                  <span>
                    {humanize(i.severity)} · {humanize(i.status)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p>No open injury flags.</p>
          )}
        </details>
      </section>
      <section className={styles.panel}>
        <h2>Sparring & S&C</h2>
        <div className={styles.subsection}>
          <div className={styles.sectionHeading}>
            <h3>Sparring</h3>
            <span className={styles.quietIcon}>
              <TileIcon name="source" />
            </span>
          </div>
          <p className={styles.footnote}>
            {sparring.filter(completed).length} recorded{" "}
            {sparring.length === 1 ? "session" : "sessions"}
            {sparring.find((s) => s.rpe)
              ? ` · Latest RPE ${sparring.find((s) => s.rpe)!.rpe}`
              : ""}
          </p>
          {reports.length ? (
            <details className={styles.method}>
              <summary>Round reports</summary>
              <ul className={styles.activity}>
                {reports.map((s) => (
                  <li key={s.id}>
                    <strong>{formatDay(s.day)}</strong>
                    <span>
                      {s.rounds} rounds · {s.intensity}
                      {s.head_contact !== null
                        ? ` · Head contact: ${s.head_contact}`
                        : ""}
                      {s.rocked ? " · Rocked reported" : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </div>
        <div className={styles.subsection}>
          <div className={styles.sectionHeading}>
            <h3>S&C programme</h3>
            {strength.length ? (
              <span className={styles.positive}>
                {Math.round((strengthDone / strength.length) * 100)}%
              </span>
            ) : null}
          </div>
          <p className={styles.footnote}>
            {strength.length
              ? `${strengthDone} of ${strength.length} scheduled sessions completed`
              : "No S&C schedule this week"}
          </p>
          {strength.length ? (
            <progress
              className={styles.strengthProgress}
              aria-label="S&C programme progress"
              value={strengthDone}
              max={strength.length}
            />
          ) : null}
        </div>
      </section>
      <p className={styles.footnote}>Read-only · Last 56 training days.</p>
    </>
  );
}

function SessionList({
  fighter,
  sessions,
  empty,
}: {
  fighter: Fighter;
  sessions: Fighter["sessions"];
  empty: string;
}) {
  return sessions.length ? (
    <ul className={styles.activity}>
      {sessions.map((s) => (
        <li key={s.id}>
          <div>
            <strong>{sessionLabel(fighter, s)}</strong>
            <small>
              {formatDay(s.day)} · {humanize(s.status)}
            </small>
          </div>
          <span>
            {s.duration ? `${s.duration} min · ` : ""}
            {s.effort
              ? `Intensity: ${s.effort}`
              : s.rpe
                ? `RPE ${s.rpe}`
                : "RPE not recorded"}
          </span>
        </li>
      ))}
    </ul>
  ) : (
    <p className="muted">{empty}</p>
  );
}
