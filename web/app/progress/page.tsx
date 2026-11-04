"use client";

import Link from "next/link";
import { useMemo } from "react";

import { useAppSession } from "@/components/auth-provider";
import { Skeleton } from "@/components/skeleton";
import { XpProgressCardSkeleton, XpProgressCardView } from "@/components/xp-progress-card";
import { useXp } from "@/components/xp-provider";
import { isSafeAvatarImageUrl } from "@/lib/avatar-image-url";
import {
  getOptionLabels,
  TACTICAL_STYLE_OPTIONS,
  TECHNICAL_STYLE_OPTIONS,
} from "@/lib/intake-options";
import { XP_ACTIONS } from "@/lib/xp";
import { xpWeekHeading } from "@/lib/xp-progress";

const numberFormatter = new Intl.NumberFormat("en-GB");
const dateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
});
const dateTimeFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

function titleCase(value: string): string {
  return value
    .trim()
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function getInitials(name: string): string {
  const initials = name
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");

  return initials || "A";
}

function RefreshIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" fill="none" aria-hidden="true" focusable="false">
      <path
        d="M15.4 6.4A6.5 6.5 0 1 0 16.2 12M15.4 6.4V2.8M15.4 6.4h-3.6"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ProgressSkeleton() {
  return (
    <div className="xp-page xp-page--refined" aria-busy="true">
      <section className="xp-athlete-header">
        <Skeleton variant="block" width={52} height={52} style={{ borderRadius: 999 }} />
        <div className="xp-athlete-skeleton-copy">
          <Skeleton variant="text" width={72} height={10} />
          <Skeleton variant="text" width="46%" height={26} />
          <Skeleton variant="text" width="62%" height={13} />
        </div>
      </section>
      <XpProgressCardSkeleton mode="page" />
      <section className="xp-page-grid xp-page-summary-grid">
        {[0, 1].map((item) => (
          <article key={item} className="xp-page-panel">
            <Skeleton variant="text" width={100} height={11} />
            <Skeleton variant="text" width="72%" height={20} />
            <Skeleton variant="text" width="58%" height={14} />
          </article>
        ))}
      </section>
    </div>
  );
}

export default function ProgressPage() {
  const { session, isReady, isMeHydrated, me } = useAppSession();
  const xp = useXp();

  const fighterIdentity = useMemo(() => {
    if (!me) return [];

    const profile = me.profile;
    const labels = [
      ...getOptionLabels(TECHNICAL_STYLE_OPTIONS, profile.technical_style),
      ...getOptionLabels(TACTICAL_STYLE_OPTIONS, profile.tactical_style),
      profile.stance ? titleCase(profile.stance) : "",
    ].filter(Boolean);

    return [...new Set(labels)];
  }, [me]);

  if (!isReady || (session && !isMeHydrated) || !xp.isHydrated) {
    return <ProgressSkeleton />;
  }

  if (!session || !me || me.profile.role !== "athlete") {
    return (
      <section className="panel loading-card">
        <p className="kicker">Progress</p>
        <h1>Athlete account required</h1>
        <p className="muted">Sign in as an athlete to view XP.</p>
        <Link href="/login" className="cta">Sign in</Link>
      </section>
    );
  }

  const profile = me.profile;
  const athleteName = profile.full_name.trim() || "Your progress";
  const avatarUrl = isSafeAvatarImageUrl(profile.avatar_url) ? profile.avatar_url : null;
  const statusAndRecord = [
    profile.professional_status ? titleCase(profile.professional_status) : "",
    profile.record?.trim() || "",
  ].filter(Boolean);
  const visibleAwards = xp.progress.state.recentAwards.slice(0, 3);

  return (
    <div className="xp-page xp-page--refined">
      <header className="xp-athlete-header">
        <div className="xp-athlete-avatar" aria-hidden="true">
          {avatarUrl ? (
            <img src={avatarUrl} alt="" />
          ) : (
            <span>{getInitials(athleteName)}</span>
          )}
        </div>

        <div className="xp-athlete-identity">
          <p className="status-label">PROGRESS</p>
          <h1>{athleteName}</h1>
          {fighterIdentity.length > 0 ? (
            <p className="xp-athlete-style-line">{fighterIdentity.join(" · ")}</p>
          ) : (
            <p className="xp-athlete-style-line">Complete your profile to personalise this page.</p>
          )}
          {statusAndRecord.length > 0 ? (
            <p className="xp-athlete-record-line">{statusAndRecord.join(" · ")}</p>
          ) : null}
        </div>

        <button
          type="button"
          className="xp-refresh-button"
          onClick={() => void xp.refresh()}
          disabled={xp.isRefreshing}
          aria-label={xp.isRefreshing ? "Refreshing XP progress" : "Refresh XP progress"}
        >
          <RefreshIcon />
        </button>
      </header>

      {xp.error ? (
        <div className="xp-page-notice" role="status">
          {xp.error} The last valid progress view is still shown.
        </div>
      ) : null}

      <XpProgressCardView progress={xp.progress} mode="page" />

      <section className="xp-page-grid xp-page-summary-grid">
        <article className="xp-page-panel xp-week-panel" aria-labelledby="xp-week-title">
          <div className="xp-page-section-heading">
            <div>
              <p className="status-label">THIS WEEK</p>
              <h2 id="xp-week-title">
                {xp.progress.currentWeek
                  ? xpWeekHeading(xp.progress.currentWeek)
                  : "No active training week"}
              </h2>
            </div>
          </div>
          {xp.progress.currentWeek ? (
            <>
              <p className="xp-week-count">
                <strong>{xp.progress.currentWeek.completedSessions}</strong>
                <span>/ {xp.progress.currentWeek.plannedSessions} XP-eligible cards</span>
              </p>
              <p className="muted">App sessions and support cards. Coach-led days are separate.</p>
              <div className="xp-week-track" aria-hidden="true">
                <span
                  style={{
                    width: `${
                      xp.progress.currentWeek.plannedSessions > 0
                        ? (xp.progress.currentWeek.completedSessions /
                            xp.progress.currentWeek.plannedSessions) *
                          100
                        : 0
                    }%`,
                  }}
                />
              </div>
              <p className="muted">
                {xp.progress.currentWeek.complete
                  ? xp.progress.currentWeek.weekXpEarned
                    ? "+100 XP earned for the completed week."
                    : "Week complete. Updating your XP…"
                  : `${xp.progress.currentWeek.remainingSessions} card${
                      xp.progress.currentWeek.remainingSessions === 1 ? "" : "s"
                    } remaining. +100 XP when complete.`}
              </p>
            </>
          ) : (
            <p className="xp-page-empty">Activate a plan to track weekly progress.</p>
          )}
        </article>

        <article className="xp-page-panel" aria-labelledby="xp-milestones-title">
          <div className="xp-page-section-heading">
            <div>
              <p className="status-label">JOURNEY</p>
              <h2 id="xp-milestones-title">Plan milestones</h2>
            </div>
          </div>
          {xp.progress.majorMilestones.length > 0 ? (
            <ol className="xp-milestone-list">
              {xp.progress.majorMilestones.map((milestone) => (
                <li key={`${milestone.milestoneType}:${milestone.planId}:${milestone.id}`}>
                  <span className="xp-milestone-dot" aria-hidden="true" />
                  <div>
                    <strong>{milestone.displayLabel}</strong>
                    <span>{dateFormatter.format(new Date(milestone.completedAt))}</span>
                  </div>
                </li>
              ))}
            </ol>
          ) : (
            <p className="xp-page-empty">Completed phases, plans and camps appear here.</p>
          )}
        </article>
      </section>

      <section className="xp-page-panel xp-awards-panel" aria-labelledby="xp-awards-title">
        <div className="xp-page-section-heading">
          <div>
            <p className="status-label">RECENT XP</p>
            <h2 id="xp-awards-title">Latest earned</h2>
          </div>
          <span>Latest 3</span>
        </div>
        {visibleAwards.length > 0 ? (
          <div className="xp-award-list">
            {visibleAwards.map((award) => (
              <div key={award.id} className="xp-award-row">
                <div>
                  <strong>{XP_ACTIONS[award.action].label}</strong>
                  <span>{dateTimeFormatter.format(new Date(award.awardedAt))}</span>
                </div>
                <span className="xp-award-amount">+{numberFormatter.format(award.amount)} XP</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="xp-page-empty">Earn XP to see your activity here.</p>
        )}
      </section>

      <details className="xp-page-panel xp-explanation xp-explanation-disclosure">
        <summary>
          <span>
            <span className="status-label">HOW XP WORKS</span>
            <strong>UNLXCK XP tracks your progress inside the app.</strong>
          </span>
          <span className="xp-details-chevron" aria-hidden="true">⌄</span>
        </summary>
        <div className="xp-explanation-content">
          <p>Earn XP from training, check-ins and plan milestones. More XP, higher rank.</p>
          <p>Rank reflects personal progress, not official amateur or pro status.</p>
          <p>XP may later unlock discounts, rewards and opportunities.</p>
          <p>No public leaderboards during private beta.</p>
        </div>
      </details>
    </div>
  );
}
