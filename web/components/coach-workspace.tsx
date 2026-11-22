"use client";
import { DiscoveryHint } from "@/components/once-hint";

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
import { CoachPlans } from "./coach-plans";
import { CoachFeedback } from "./coach-feedback";
import { FighterMediaPanel } from "./fighter-media";
import { LogTraining } from "@/components/log-training";
import { useAppSession } from "@/components/auth-provider";
import {
  SessionDetails,
  TrainingBlock,
  SessionRow,
  type SessionDetailsCache,
} from "@/components/session-details";
import { Skeleton } from "@/components/skeleton";
import { WeightTrendChart } from "@/components/weight-trend-chart";
import {
  getFighterHistory,
  getFighterPlan,
  getFighterSessionDetails,
  getFighterWeight,
  type getSessionDetails,
} from "@/lib/api";
import type { CoachFighterWeight, CoachPlannedSession } from "@/lib/api-schema.generated";
import { isSafeAvatarImageUrl } from "@/lib/avatar-image-url";
import {
  coachRpc,
  coachConcerns,
  compareCoachConcerns,
  coachCutStatus,
  groupCoachCheckIns,
  coachCheckInValues,
  coachRecoveryTone,
  completed,
  coachInviteShare,
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
      <div className={`${styles.workspace} ${view === "plans" ? styles.plansWorkspace : ""}`}>
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
      return next;
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
        p_action: ["logging", "media_on", "media_off"].includes(action) ? "consent" : action,
        p_id: id ?? null,
        p_email: action === "invite" ? email : null,
        p_recovery: share || connection?.share_recovery === true,
        p_injuries: share || connection?.share_injuries === true,
        p_weight: share || connection?.share_weight === true,
        p_logging: action === "accept" || action === "logging" || connection?.allow_session_logging === true,
        // Photos and voice notes are never shared by accepting; only by asking for it here.
        p_media: action === "media_on" || (action !== "media_off" && action !== "accept" && connection?.share_media === true),
      });
      if (action === "invite") setEmail("");
      await reload();
    } catch (e) {
      // A lost response may follow a committed acceptance. Read the saved state before asking for a retry.
      const current = action === "accept" ? await reload() : undefined;
      if (current?.some(row => row.id === id && row.status === "accepted")) return;
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
            ? "No invitations yet. After you invite, share the invite on WhatsApp so your athlete can accept."
            : "No coach invitations yet."}
        </p>
      ) : null}
      {connections?.map((c) => {
        const pending = c.status === "pending";
        const name = c.coach_name || "Your coach";
        const sharing = connectionSharing(c);
        return (
          coach && pending ? <PendingInvite key={c.id} connection={c} busy={busy} onCancel={() => void command("remove", c.id)} /> :
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
              {!coach && !pending && c.health_consent !== false ? (
                <small className={styles.mediaSharing}>
                  <span>{c.share_media ? "Sharing photos and voice notes" : "Not sharing photos and voice notes"}</span>
                  {c.share_media ? <button type="button" className={styles.inlineAction} disabled={busy}
                    onClick={() => void command("media_off", c.id, c)}>Stop</button> : null}
                </small>
              ) : null}
            </div>
            <div className={`${styles.actions} ${pending ? styles.invitationActions : ""}`}>
              {!coach && !pending && sharing.missing.length ? (
                <button
                  className="cta"
                  disabled={busy}
                  onClick={() => void command("consent", c.id, c)}
                >
                  Share {formatList(sharing.missing)}
                </button>
              ) : null}
              {!coach && !pending && !c.share_media && c.health_consent !== false ? (
                <button className="cta" disabled={busy} onClick={() => void command("media_on", c.id, c)}>
                  Share photos &amp; voice notes
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

// A pending invite has one job: reach the athlete. Sharing leads, full width
// and thumb-height; copying is the fallback; cancelling sits apart in the
// header so it is never tapped by mistake while sharing.
function PendingInvite({ connection, busy, onCancel }: { connection: Connection; busy: boolean; onCancel: () => void }) {
  const [copied, setCopied] = useState<"" | "copied" | "failed">("");
  const share = coachInviteShare(connection, window.location.origin);
  return (
    <article className={`${styles.connection} ${styles.pendingInvite}`}>
      <div className={styles.inviteHeader}>
        <div className={styles.rowCopy}>
          <div className={styles.inviteTitle}><strong>{connection.invited_email}</strong><span className={styles.pendingBadge}>Pending</span></div>
          <small className={styles.inviteHint}>They sign in with this email to accept.</small>
        </div>
        <button type="button" className={styles.cancelInvite} disabled={busy} onClick={onCancel}
          aria-label={`Cancel invite to ${connection.invited_email}`}>
          Cancel
        </button>
      </div>
      <div className={styles.inviteActions}>
        <a className={styles.whatsappButton} href={share.whatsapp} target="_blank" rel="noopener noreferrer">
          <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M12 2a10 10 0 0 0-8.6 15.1L2 22l5-1.3A10 10 0 1 0 12 2Zm0 18.2a8.2 8.2 0 0 1-4.2-1.2l-.3-.2-3 .8.8-2.9-.2-.3A8.2 8.2 0 1 1 12 20.2Zm4.5-6.1c-.2-.1-1.5-.7-1.7-.8-.2-.1-.4-.1-.6.1l-.8 1c-.1.2-.3.2-.5.1a6.7 6.7 0 0 1-3.3-2.9c-.2-.4.2-.4.7-1.3.1-.2 0-.3 0-.4l-.8-1.8c-.2-.5-.4-.4-.6-.4h-.5a1 1 0 0 0-.7.3 3 3 0 0 0-.9 2.2c0 1.3.9 2.5 1.1 2.7.1.2 1.8 2.8 4.4 3.9 1.6.7 2.3.8 3.1.6.5-.1 1.5-.6 1.7-1.2.2-.6.2-1.1.2-1.2-.1-.1-.2-.2-.4-.3Z"/></svg>
          Share on WhatsApp
        </a>
        <button
          type="button"
          className={`ghost-button ${styles.copyInvite}`}
          onClick={() => {
            navigator.clipboard.writeText(share.text).then(() => setCopied("copied"), () => setCopied("failed"));
          }}
        >
          {copied === "copied" ? "Copied ✓" : "Copy invite"}
        </button>
      </div>
      {copied === "failed" ? <small role="alert" className={styles.inviteError}>Copy failed. Share on WhatsApp instead.</small> : null}
    </article>
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
      {view === "plans" ? null : <div className={styles.pageHead}>
        {fighterId ? (
          <Link className={styles.back} href="/coach">
            ← All fighters
          </Link>
        ) : (
          <h1>{view === "fighters" ? "Your fighters" : view === "plans" ? "Fighter Plans" : view === "log" ? "Log Session" : "Your corner. In focus."}</h1>
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
      </div>}
      {view === "plans" && fighters ? <CoachPlans key={token} token={token} fighters={fighters} /> : view === "log" && fighters && !loggingFighters.length ? <p className={styles.empty}>No fighters have authorised session logging.</p> : view === "log" && fighters ? <DiscoveryHint id="coach-log-consent" text="You can only log for fighters who said yes." /> : null}
      {fighterId ? <h1 className={styles.srOnly}>Fighter overview</h1> : null}
      <LoadState error={error} loading={!fighters} retry={reload} />
      {!fighters ? null : !fighterId ? (
        view === "plans" || view === "log" ? null : <CoachDashboard token={token} fighters={fighters} rosterOnly={view === "fighters"} />
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

function CoachDashboard({ token, fighters, rosterOnly = false }: { token: string; fighters: Fighter[]; rosterOnly?: boolean }) {
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [weights, setWeights] = useState<Record<string, { token: string; connectionId: string; weight: CoachFighterWeight | null; failed: boolean }>>({});
  useEffect(() => {
    let active = true;
    const eligible = fighters.filter(f => f.share_weight && (f.age == null || f.age >= 18));
    // Keep authorised results during the regular background refresh, but drop
    // anything belonging to an ended connection, changed account or consent.
    setWeights(previous => Object.fromEntries(eligible.flatMap(f => {
      const result = previous[f.id];
      return result?.token === token && result.connectionId === f.connection_id ? [[f.id, result]] : [];
    })));
    for (const f of eligible) {
      void getFighterWeight(token, f.id).then(weight => {
        if (active) setWeights(previous => ({ ...previous, [f.id]: { token, connectionId: f.connection_id, weight, failed: false } }));
      }).catch(() => {
        if (active) setWeights(previous => ({ ...previous, [f.id]: { token, connectionId: f.connection_id, weight: null, failed: true } }));
      });
    }
    return () => { active = false; };
  }, [token, fighters]);
  const rows = fighters.map(fighter => {
    const eligible = fighter.share_weight && (fighter.age == null || fighter.age >= 18);
    const result = eligible && weights[fighter.id]?.token === token && weights[fighter.id]?.connectionId === fighter.connection_id ? weights[fighter.id] : undefined;
    return {
      fighter,
      summary: fighterSummary(fighter),
      concerns: coachConcerns(fighter, result?.weight ?? null, result?.failed ?? false),
      cut: result?.weight ? coachCutStatus(result.weight, fighter.today) : null,
      weightPending: eligible && !result,
    };
  });
  const [calendarFor, setCalendarFor] = useState<string | null>(null);
  const pending = rows.some(row => row.weightPending);
  const attention = rows.filter(row => row.concerns.length).sort((a, b) => compareCoachConcerns(a.concerns[0], b.concerns[0]));
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
      {attention.length ? <DiscoveryHint id="coach-attention" text="Fighters who need help show at the top. Tap one to see why." /> : null}
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
              {label === "Needs attention" && pending ? "—" : value}
            </strong>
          </article>
        ))}
      </section>
      {attention.length ? (
        <section className={styles.panel} aria-label="Needs attention">
          <h2>Needs attention</h2>
          <ul className={styles.roster}>
            {attention.map(({ fighter: f, concerns }) => (
              <FighterRow
                key={f.id}
                fighter={f}
                lead={<Avatar fighter={f} />}
                detail={<span data-recovery-tone={concerns[0].tone}>{concerns[0].reason}</span>}
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
                below={<div className={styles.fightCalendar}>
                  <button type="button" className={styles.pill} aria-expanded={calendarFor === f.id}
                    aria-label={`${calendarFor === f.id ? "Hide" : "Show"} ${f.name || "fighter"}'s calendar`}
                    onClick={() => setCalendarFor(calendarFor === f.id ? null : f.id)}>
                    {calendarFor === f.id ? "Hide calendar" : "Calendar"}
                  </button>
                  {/* The fighter's own calendar: done, planned and fight day, under the same sharing rules. */}
                  {calendarFor === f.id ? <TrainingHistory
                    key={`fight-calendar:${f.id}:${f.connection_id}:${f.plan_id}:${f.today}`}
                    token={token} athleteId={f.id} today={f.today} fightDate={f.fight_date} sport={f.sport}
                  /> : null}
                </div>}
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
            {visible.map(({ fighter: f, summary: s, concerns, cut, weightPending }) => (
              <FighterRow
                key={f.id}
                fighter={f}
                lead={<Avatar fighter={f} />}
                detail={[f.sport ? humanize(f.sport) : null, f.age != null ? `${f.age} years` : null, f.record, `${s.weekSessions} sessions`].filter(Boolean).join(" · ")}
                extra={[
                  f.last_training ? `Last trained ${formatAppDate(f.last_training.day)}` : null,
                  concerns[0]?.category === "weight" ? (s.recovery === "No concern reported" ? "Recovery normal" : `Recovery: ${s.recovery.toLowerCase()}`) : null,
                ].filter(Boolean).join(" · ") || undefined}
                end={
                  <span data-recovery-tone={concerns[0]?.tone} className={!concerns.length && !weightPending && s.recovery === "No concern reported" ? styles.positive : styles.muted}>
                    {concerns.length
                      ? concerns[0].category === "weight" ? cut?.label === "Unknown" ? "Weight unknown" : cut?.label ?? "Weight unavailable" : "Needs attention"
                      : weightPending ? "Checking weight…" : s.recovery === "No concern reported" ? "Recovery normal" : s.recovery}
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
  below,
}: {
  fighter: Fighter;
  lead: ReactNode;
  detail: ReactNode;
  extra?: ReactNode;
  end?: ReactNode;
  /** Shown under the row, outside its link (e.g. an expandable calendar). */
  below?: ReactNode;
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
      {below}
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

function FoldableSection({ title, children, id }: { title: string; children: ReactNode; id?: string }) {
  return <section id={id} className={`${styles.panel} ${styles.fold}`}><details open>
    <summary className={styles.foldTitle}><h2>{title}</h2><span aria-hidden="true">⌄</span></summary>
    <div className={styles.foldContent}>{children}</div>
  </details></section>;
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
  const [weightResult, setWeightResult] = useState<{ key: string; weight: CoachFighterWeight | null; failed: boolean } | null>(null);
  const weightKey = `${f.id}:${f.share_weight}:${token}`;
  const weight = weightResult?.key === weightKey && f.share_weight ? weightResult.weight : null;
  const weightFailed = weightResult?.key === weightKey && weightResult.failed;
  useEffect(() => {
    if (!f.share_weight) { setWeightResult(null); return; }
    let active = true;
    getFighterWeight(token, f.id).then(weight => { if (active) setWeightResult({ key: weightKey, weight, failed: false }); })
      .catch(() => { if (active) setWeightResult({ key: weightKey, weight: null, failed: true }); });
    return () => { active = false; };
  }, [token, f, weightKey]);
  const [allConcerns, setAllConcerns] = useState(false);
  const concerns = coachConcerns(f, weight, Boolean(weightFailed));
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
  const checkIns = groupCoachCheckIns(f.share_recovery ? (f.recovery ?? []).filter(row => row.day <= f.today) : []);
  const latest = checkIns[0];
  const latestSleep = latest?.readings.find(row => row.sleep != null || row.sleep_quality != null);
  const latestBody = latest?.readings.find(row => row.body != null || row.readiness != null);
  const recoveryRecent = Boolean(latest && latest.day >= dayOffset(f.today, -6));
  const sleepTone = !recoveryRecent ? "unknown" : coachRecoveryTone(latestSleep?.sleep ? "sleep" : "sleep_quality", latestSleep?.sleep ?? latestSleep?.sleep_quality);
  const bodyTone = !recoveryRecent ? "unknown" : coachRecoveryTone("readiness", latestBody?.body ?? latestBody?.readiness);
  const warnings = coachCheckInValues(recoveryRecent ? latest?.readings ?? [] : []).filter(value => value.tone === "caution" || value.tone === "concern");
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
      <DiscoveryHint id="coach-fighter-sections" text="Tap a section to open it." />
      {concerns.length ? <section className={`${styles.panel} ${styles.attentionPanel}`} aria-label="Needs attention">
        <h2>Needs attention</h2>
        <ul>{(allConcerns ? concerns : concerns.slice(0, 3)).map(concern => <li key={concern.id} data-recovery-tone={concern.tone}>
          <a href={`#${concern.section}`} onClick={() => {
            if (concern.section === "sparring") {
              const report = f.sparring_reports?.find(report => `contact:${report.id}` === concern.id);
              if (report) { setWeek(weekStart(report.day)); setAllSparring(true); }
            }
            if (concern.section === "recovery") setAllCheckIns(true);
            const target = document.getElementById(concern.section);
            const details = target?.querySelector("details");
            if (details) details.open = true;
          }}><span>{concern.reason}</span><span aria-hidden="true">›</span></a>
        </li>)}</ul>
        {concerns.length > 3 ? <button type="button" className="ghost-button" aria-expanded={allConcerns} onClick={() => setAllConcerns(!allConcerns)}>{allConcerns ? "Show fewer" : "View all"}</button> : null}
      </section> : null}
      <FoldableSection title="Weekly performance"><section aria-label="Weekly performance">
        <DiscoveryHint id="coach-week-arrows" text="Use the arrows to see other weeks." />
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
      </FoldableSection>
      <FoldableSection id="sparring" title="Sparring">{reports.length ? (
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
      </FoldableSection>
      {/* A new key when sharing changes drops everything these loaded
          (weigh-ins, session rows, cached session records, an open record
          sheet) and reloads it under the new permissions. */}
      <FoldableSection id="fight-weight" title="Fight weight"><FightWeight key={`weight:${sharingKey}`} fighter={f} weight={weight} failed={Boolean(weightFailed)} /></FoldableSection>
      <FoldableSection id="media" title="Photos and voice notes"><FighterMediaPanel token={token} fighter={f} /></FoldableSection>
      <FoldableSection id="training-history" title="Training history"><TrainingHistory
        key={`history:${sharingKey}:${historyKey}:${f.plan_id}:${f.today}`}
        token={token}
        athleteId={f.id}
        today={f.today}
        fightDate={f.fight_date}
        sport={f.sport}
      /></FoldableSection>
      <FoldableSection id="recovery" title="Recovery"><section className={styles.panel}>
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
        <div className={`${styles.tiles} ${styles.recoveryTiles}`}>
          <div><span>Sleep</span><strong data-recovery-tone={sleepTone}>{latest && !recoveryRecent ? "Unknown · Older check-in" : latestSleep?.sleep ? humanize(latestSleep.sleep) : latestSleep?.sleep_quality != null ? `${latestSleep.sleep_quality}/5` : unknown}</strong></div>
          <div><span>Readiness</span><strong data-recovery-tone={bodyTone}>{latest && !recoveryRecent ? "Unknown · Older check-in" : latestBody?.body ? humanize(latestBody.body) : latestBody?.readiness != null ? `${latestBody.readiness}/5` : unknown}</strong></div>
        </div>
        {f.share_recovery ? <>
          <h3>Recent check-ins</h3>
          {checkIns.length ? <CheckIns rows={allCheckIns ? checkIns : checkIns.slice(0, 3)} /> : <p className={styles.empty}>No check-ins recorded.</p>}
          {checkIns.length > 3 ? <button type="button" className="ghost-button" aria-expanded={allCheckIns} onClick={() => setAllCheckIns(!allCheckIns)}>{allCheckIns ? "Show recent check-ins" : "View all check-ins"}</button> : null}
        </> : <p className={styles.muted}>Recovery check-ins: Not shared</p>}
      </section></FoldableSection>
    </>
  );
}

function CheckIns({ rows }: { rows: ReturnType<typeof groupCoachCheckIns> }) {
  return <ul className={`${styles.activity} ${styles.checkInList}`}>{rows.map(row => <li key={row.day}>
    <strong>{formatAppDate(row.day)}</strong>
    <span className={styles.checkInReadings}>{coachCheckInValues(row.readings).map(value => <span key={value.label} data-recovery-tone={value.tone}>{value.label}</span>)}</span>
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
  fightDate,
  sport,
}: {
  fightDate: string | null;
  sport: string | null;
  today: string;
  token: string;
  athleteId: string;
}) {
  const [planned, setPlanned] = useState<CoachPlannedSession[]>([]);
  const [planError, setPlanError] = useState(false);
  const [openPlan, setOpenPlan] = useState<string | null>(null);
  const [openBlock, setOpenBlock] = useState<number | null>(null);
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
  useEffect(() => {
    let active = true;
    setPlanned([]); setPlanError(false);
    getFighterPlan(token, athleteId).then(plan => { if (active) setPlanned(plan); })
      .catch(() => { if (active) { setPlanned([]); setPlanError(true); } });
    return () => { active = false; };
  }, [token, athleteId, attempt]);
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
  const futureRows = [...new Map(planned.filter(session => session.day >= today && !(rows ?? []).some(row =>
    row.plan_id === session.plan_id && row.session_id === session.id && row.training_day === session.day
      && ["done", "modified", "skipped"].includes(row.status))).map(session => [`${session.plan_id}:${session.day}:${session.id}`, session])).values()];
  const dayPlans = futureRows.filter(session => session.day === day);
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
            <button type="button" className={styles.pill} aria-label="Next month" onClick={() => changeMonth(1)}>›</button></div>
        </div>
        <DiscoveryHint id="coach-calendar-day" text="Tap a day to see what they did." />
        <div className={styles.trainingCalendar} role="group" aria-label="Training calendar">
          {CALENDAR_WEEKDAYS.map(label => <small key={label}>{label.slice(0, 1)}</small>)}
          {Array.from({ length: offset }, (_, index) => <span key={`gap-${index}`} />)}
          {Array.from({ length: count }, (_, index) => {
            const iso = `${month}-${String(index + 1).padStart(2, "0")}`;
            const sessions = completedRows.filter(row => row.training_day === iso);
            const plans = futureRows.filter(row => row.day === iso);
            const fight = iso === fightDate;
            return <button type="button" key={iso} aria-label={`${formatAppDate(iso)}: ${sessions.length} completed sessions${plans.length ? `, ${plans.length} planned` : ""}${fight ? ", Fight day" : ""}`} aria-pressed={day === iso} aria-current={iso === today ? "date" : undefined} onClick={() => setDay(iso)}>
              {index + 1}<span className={styles.calendarDots}>{sessions.map(row => <i key={`${row.source}:${row.id}`} data-training-tone={trainingTone(row)} aria-label={trainingTone(row)} />)}{plans.map(row => <i key={`plan:${row.id}`} data-training-tone="planned" aria-label="Planned" />)}{fight ? <b className={styles.fightMarker} aria-label="Fight day">◆</b> : null}</span>
            </button>;
          })}
        </div>
        <div className={styles.calendarLegend}>{["strength", "sparring", "rehab", "modified", "training", "planned"].map(tone => <span key={tone}><i data-training-tone={tone} />{humanize(tone)}</span>)}</div>
        <small className={styles.muted}>◆ Fight day</small>
        {planError ? <p role="status" className={styles.muted}>Upcoming plan unavailable.</p> : null}
        {busy ? <p role="status" className={styles.muted}>Loading earlier sessions…</p> : null}
        <h3>{formatAppDate(day)}</h3>
        {day === fightDate ? <div className={styles.plannedSession}><strong>◆ Fight day</strong><p>{[sport ? humanize(sport) : null, formatAppDate(fightDate)].filter(Boolean).join(" · ")}</p></div> : null}
        {dayPlans.map(session => <article key={`${session.plan_id}:${session.id}`} className={styles.plannedSession}>
          <button type="button" className={styles.plannedToggle} aria-expanded={openPlan === session.id} onClick={() => { setOpenPlan(openPlan === session.id ? null : session.id); setOpenBlock(null); }}><strong>{session.title}</strong><span>Planned ⌄</span></button>
          {openPlan === session.id ? session.blocks.length ? <ol>{session.blocks.map((block, index) => <TrainingBlock key={index} block={block} index={index} open={openBlock === index} onToggle={() => setOpenBlock(openBlock === index ? null : index)} compact numbered={false} viewer="coach" planned />)}</ol> : <p className={styles.muted}>Exercise details unavailable.</p> : null}
        </article>)}
        {!busy && !shown.length && !dayPlans.length && day !== fightDate ? <p className={styles.empty}>No sessions recorded or planned for this day.</p> : null}
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
const WEIGH_INS = 3;

// The athlete's own weigh-ins and target, shown as recorded. Progress only:
// nothing here suggests how to make weight.
function FightWeight({ fighter: f, weight, failed }: { fighter: Fighter; weight: CoachFighterWeight | null; failed: boolean }) {
  const [range, setRange] = useState<BodyweightRange | null>(null);
  const [picked, setPicked] = useState<number | null>(null);
  const [showAll, setShowAll] = useState(false);
  const cut = f.share_weight ? coachCutStatus(weight, f.today) : null;
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
      {!f.share_weight ? <p className={styles.empty}>Not shared.</p> : failed ? (
        <p className={styles.empty}>Unable to load weight.</p>
      ) : !weight ? (
        <Loading />
      ) : !weight.shared ? (
        <p className={styles.empty}>Not shared.</p>
      ) : current === null && target === null ? (
        <p className={styles.empty}>No weight logged yet.</p>
      ) : (
        <>
          {cut ? <div className={styles.cutStatus} data-recovery-tone={cut.tone === "red" ? "concern" : cut.tone === "amber" ? "caution" : cut.tone === "green" ? "good" : "unknown"}>
            <small>Weight-cut safety</small><strong>{cut.label}</strong><p>{cut.detail}</p>{weight.current_weight_recorded_at ? <small>Weight recorded · {formatAppDate(weight.current_weight_recorded_at.slice(0, 10))}</small> : null}
          </div> : null}
          <div className={`${styles.tiles} ${styles.weightTiles}`}>
            {[
              ["Current", formatWeight(current)],
              ["Target", target === null ? "Not set" : formatWeight(target)],
              ...(gap === null
                ? []
                : [
                    [
                      gap === 0
                        ? "At target weight"
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
            <div className={`${weightStyles.trendCard} ${styles.compactWeight}`}>
              <div className={styles.sectionHeading}>
                <span className={styles.muted}>
                  {selected
                    ? `${formatWeight(selected.weight_kg)} · ${formatAppDate(selected.date)}`
                    : `No weights in the last ${activeRange === "7D" ? 7 : 30} days`}
                </span>
                <DiscoveryHint id="coach-weight-range" text="Pick a time range to see the change." />
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
              {trend.length > 1 ? (
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
              {showAll ? "Show fewer" : `View all ${logged.length} weigh-ins`}
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}
