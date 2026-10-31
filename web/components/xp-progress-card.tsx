"use client";

import Link from "next/link";
import { useMemo, type CSSProperties, type ReactNode } from "react";

import { Skeleton } from "@/components/skeleton";
import { useXp } from "@/components/xp-provider";
import { resolveXpLevel } from "@/lib/xp";
import type { StreakState, StreakValue, XpOpportunity, XpProgress } from "@/lib/xp-progress";

const numberFormatter = new Intl.NumberFormat("en-GB");

type XpProgressCardMode = "overview" | "page";

/** Streak icons follow the inline-SVG convention already used across the app
    (currentColor stroke, decorative, no icon package) so nothing new is added to
    the dependency tree for two glyphs. Stroke width is set for the 24-unit box
    to match the optical weight of the 20-unit icons elsewhere. */
function StreakFlameIcon() {
  return (
    <svg
      className="xp-streak-icon"
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.4-.5-2-1-3-1.1-2.1-.2-4 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.2.4-2.3 1-3a2.5 2.5 0 0 0 2.5 2.5Z"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function StreakBoltIcon() {
  return (
    <svg
      className="xp-streak-icon"
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d="M13.5 2.5 4.7 13.3a.6.6 0 0 0 .5 1h5.1l-.8 7.2 8.8-10.8a.6.6 0 0 0-.5-1h-5.1l.8-7.2Z"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Near-best copy. Returns null whenever the athlete has no established streak,
    so a fresh account never reads "0 more to match your best". A current value
    above the stored best is not treated as a new record: the payload cannot
    prove the record was just broken rather than lagging reconciliation. */
function streakBestMessage(streak: StreakValue): string | null {
  if (streak.current <= 0 || streak.best <= 0) return null;
  const distance = streak.best - streak.current;
  if (distance === 0) return "You’ve matched your best";
  if (distance > 0 && distance <= 3) return `${distance} more to match your best`;
  return null;
}

function StreakColumn({
  tone,
  label,
  labelId,
  streak,
  icon,
  detailed,
}: {
  tone: "training" | "app";
  label: string;
  labelId: string;
  streak: StreakValue;
  icon: ReactNode;
  detailed: boolean;
}) {
  const message = detailed ? streakBestMessage(streak) : null;
  // Only meaningful while the best is still ahead: 0/0 and matched bests get no bar.
  const showTrack =
    detailed && streak.best > 0 && streak.current > 0 && streak.current < streak.best;

  return (
    <div className="xp-streak" data-streak={tone} role="group" aria-labelledby={labelId}>
      <p className="xp-streak-label" id={labelId}>
        {icon}
        <span className="xp-streak-label-text">{label}</span>
      </p>
      <p className="xp-streak-value">{numberFormatter.format(streak.current)}</p>
      <p className="xp-streak-best">Best {numberFormatter.format(streak.best)}</p>
      {/* The optional copy shares one grid row so a column with a bar and a
          column without still line their supporting text up. */}
      {message || showTrack ? (
        <div className="xp-streak-extras">
          {message ? <p className="xp-streak-note">{message}</p> : null}
          {showTrack ? (
            <div
              className="xp-streak-track"
              role="progressbar"
              aria-label={`${label} against personal best`}
              aria-valuemin={0}
              aria-valuemax={streak.best}
              aria-valuenow={streak.current}
              aria-valuetext={`${numberFormatter.format(streak.current)} of a best of ${numberFormatter.format(streak.best)}`}
            >
              <span
                style={{ "--xp-streak-fill": `${(streak.current / streak.best) * 100}%` } as CSSProperties}
              />
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function StreakPanel({
  streaks,
  mode,
}: {
  streaks: StreakState;
  mode: XpProgressCardMode;
}) {
  const detailed = mode === "page";
  return (
    <section
      className={`xp-streak-panel${detailed ? " xp-streak-panel--page" : ""}`}
      aria-label="Streaks"
    >
      <StreakColumn
        tone="training"
        label="Training streak"
        labelId={`${mode}-training-streak-label`}
        streak={streaks.adherence}
        icon={<StreakFlameIcon />}
        detailed={detailed}
      />
      <StreakColumn
        tone="app"
        label="App streak"
        labelId={`${mode}-app-streak-label`}
        streak={streaks.login}
        icon={<StreakBoltIcon />}
        detailed={detailed}
      />
    </section>
  );
}

export type XpProgressCardViewProps = {
  progress: XpProgress;
  error?: string | null;
  mode?: XpProgressCardMode;
};

function OpportunityRow({
  opportunity,
  interactive,
}: {
  opportunity: XpOpportunity;
  interactive: boolean;
}) {
  const content: ReactNode = (
    <>
      <span className="xp-progress-action-label">{opportunity.label}</span>
      <span className="xp-progress-award">+{numberFormatter.format(opportunity.xp)} XP</span>
    </>
  );

  if (interactive) {
    return (
      <Link
        href={opportunity.href}
        className="xp-progress-detail-value xp-progress-action-row xp-progress-action-link"
      >
        {content}
      </Link>
    );
  }

  return <p className="xp-progress-detail-value xp-progress-action-row">{content}</p>;
}

export function XpProgressCardSkeleton({ mode = "overview" }: { mode?: XpProgressCardMode }) {
  if (mode === "overview") {
    return (
      <article className="status-card overview-command-card xp-progress-card xp-progress-card--overview xp-progress-card-skeleton" aria-busy="true">
        <div className="xp-overview-heading">
          <div className="xp-overview-heading-copy xp-overview-skeleton-copy">
            <Skeleton variant="text" width={120} height={19} />
            <Skeleton variant="text" width={124} height={13} />
          </div>
          <Skeleton variant="text" width={96} height={16} />
        </div>
        <div className="xp-overview-skeleton-track">
          <Skeleton variant="block" width="100%" height={5} style={{ borderRadius: 999 }} />
        </div>
        <div className="xp-overview-footer xp-overview-skeleton-footer">
          <Skeleton variant="text" width={92} height={14} />
          <Skeleton variant="text" width={72} height={14} />
        </div>
      </article>
    );
  }

  return (
    <article
      className={`status-card overview-command-card xp-progress-card xp-progress-card-skeleton${
        mode === "page" ? " xp-progress-card--page" : ""
      }`}
      aria-busy="true"
    >
      <div className="xp-progress-heading">
        <div className="xp-progress-skeleton-heading">
          <Skeleton variant="text" width={92} height={11} />
          <Skeleton variant="text" width={58} height={14} />
        </div>
        <Skeleton variant="block" width={74} height={20} style={{ borderRadius: 999 }} />
      </div>
      <div className="xp-progress-skeleton-total">
        <Skeleton variant="text" width="42%" height={44} />
      </div>
      <div className="xp-progress-skeleton-track">
        <Skeleton variant="block" width="100%" height={9} style={{ borderRadius: 999 }} />
      </div>
      <div className="xp-progress-skeleton-meta">
        <Skeleton variant="text" width={86} height={14} />
        <Skeleton variant="text" width={104} height={14} />
      </div>
      <div className="xp-progress-details">
        <section className="xp-progress-detail">
          <Skeleton variant="text" width={48} height={10} />
          <Skeleton variant="text" width="88%" height={14} />
        </section>
        <section className="xp-progress-detail">
          <Skeleton variant="text" width={74} height={10} />
          <Skeleton variant="text" width="82%" height={14} />
        </section>
      </div>
    </article>
  );
}

export function XpProgressCardView({
  progress,
  error = null,
  mode = "overview",
}: XpProgressCardViewProps) {
  const level = useMemo(() => resolveXpLevel(progress.state.totalXp), [progress.state.totalXp]);
  const ratio = level.nextLevel
    ? `${numberFormatter.format(progress.state.totalXp)} / ${numberFormatter.format(level.nextLevel.threshold)} XP`
    : null;
  const progressMaximum = level.nextLevel ? level.xpForNextLevel : 100;
  const progressNow = level.nextLevel ? level.xpWithinLevel : 100;
  const progressText = level.nextLevel
    ? `${numberFormatter.format(level.xpRemaining)} XP to Level ${level.nextLevel.level}`
    : "Maximum level reached";
  const progressRemaining = level.nextLevel
    ? `${numberFormatter.format(level.xpRemaining)} XP remaining`
    : "Max level reached";
  const [primaryOpportunity, ...remainingOpportunities] = progress.opportunities;
  const interactiveActions = mode === "page";

  if (mode === "overview") {
    return (
      <Link href="/progress" className="xp-progress-card-link xp-progress-card-link--overview" aria-label="Open XP progress">
        <article className="status-card overview-command-card xp-progress-card xp-progress-card--overview">
          <div className="xp-overview-heading">
            <div className="xp-overview-heading-copy">
              <p className="xp-overview-level">
                <strong>Level {level.currentLevel.level}</strong>
                <span>{level.currentLevel.title}</span>
              </p>
              <p className="xp-overview-remaining">{progressText}</p>
            </div>
            <p className="xp-overview-total">
              {ratio ?? `${numberFormatter.format(progress.state.totalXp)} XP`}
            </p>
          </div>
          <div
            className="xp-progress-track xp-overview-track"
            role="progressbar"
            aria-label={level.nextLevel ? `XP progress to Level ${level.nextLevel.level}` : "Maximum XP level reached"}
            aria-valuemin={0}
            aria-valuemax={progressMaximum}
            aria-valuenow={progressNow}
            aria-valuetext={progressText}
          >
            <span className="xp-progress-fill" style={{ "--xp-progress-width": `${level.percentage}%` } as CSSProperties} />
          </div>
          <div className="xp-overview-footer" role="group" aria-label="Streaks">
            <span className="xp-overview-streak" role="group" data-streak="training" aria-label={`Training streak: ${numberFormatter.format(progress.streaks.adherence.current)} ${progress.streaks.adherence.current === 1 ? "day" : "days"}`}>
              <StreakFlameIcon />
              <span>Training {numberFormatter.format(progress.streaks.adherence.current)}d</span>
            </span>
            <span className="xp-overview-streak" role="group" data-streak="app" aria-label={`App streak: ${numberFormatter.format(progress.streaks.login.current)} ${progress.streaks.login.current === 1 ? "day" : "days"}`}>
              <StreakBoltIcon />
              <span>App {numberFormatter.format(progress.streaks.login.current)}d</span>
            </span>
            <svg className="xp-overview-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
              <path d="m9 18 6-6-6-6" />
            </svg>
          </div>
          {error ? <p className="xp-progress-error">Showing your last saved XP view.</p> : null}
        </article>
      </Link>
    );
  }

  const card = (
    <article
      className={`status-card overview-command-card xp-progress-card${
        mode === "page" ? " xp-progress-card--page" : ""
      }`}
    >
      <div className="xp-progress-heading">
        <div>
          <p className="status-label">YOUR LEVEL</p>
          <p className="xp-progress-level">Level {level.currentLevel.level}</p>
        </div>
        <p className="xp-progress-rank">{level.currentLevel.title}</p>
      </div>

      <p
        className="xp-progress-total"
        aria-label={`${numberFormatter.format(progress.state.totalXp)} experience points`}
      >
        <span className="xp-progress-number">{numberFormatter.format(progress.state.totalXp)}</span>
        <span className="xp-progress-unit">XP</span>
      </p>

      <div
        className="xp-progress-track"
        role="progressbar"
        aria-label={level.nextLevel ? `XP progress to Level ${level.nextLevel.level}` : "Maximum XP level reached"}
        aria-valuemin={0}
        aria-valuemax={progressMaximum}
        aria-valuenow={progressNow}
        aria-valuetext={progressText}
      >
        <span
          className="xp-progress-fill"
          style={{ "--xp-progress-width": `${level.percentage}%` } as CSSProperties}
        />
      </div>

      <p className="xp-progress-meta">
        {ratio ? <span className="xp-progress-ratio">{ratio}</span> : null}
        <span>{progressRemaining}</span>
      </p>

      <StreakPanel streaks={progress.streaks} mode={mode} />

      <div className="xp-progress-details">
        <section className="xp-progress-detail" aria-labelledby={`${mode}-xp-next-label`}>
          <p id={`${mode}-xp-next-label`} className="xp-progress-section-label">NEXT</p>
          {primaryOpportunity ? (
            <OpportunityRow opportunity={primaryOpportunity} interactive={interactiveActions} />
          ) : (
            <p className="xp-progress-detail-value xp-progress-empty">No XP action is due right now.</p>
          )}
        </section>

        <section className="xp-progress-detail" aria-labelledby={`${mode}-xp-more-label`}>
          <p id={`${mode}-xp-more-label`} className="xp-progress-section-label">MORE XP</p>
          {remainingOpportunities.length > 0 ? (
            <div className="xp-progress-action-list">
              {remainingOpportunities.map((opportunity) => (
                <OpportunityRow
                  key={opportunity.code}
                  opportunity={opportunity}
                  interactive={interactiveActions}
                />
              ))}
            </div>
          ) : (
            <p className="xp-progress-detail-value xp-progress-empty">No other action is due.</p>
          )}
        </section>
      </div>

      {error ? <p className="xp-progress-error">Showing your last saved XP view.</p> : null}
    </article>
  );

  return card;
}

export function XpProgressCard() {
  const xp = useXp();
  if (!xp.isHydrated) return <XpProgressCardSkeleton />;
  return <XpProgressCardView progress={xp.progress} error={xp.error} />;
}
