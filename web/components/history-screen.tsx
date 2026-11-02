"use client";

import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useAppSession } from "@/components/auth-provider";
import { useOnReconnect } from "@/components/connection-status-banner";
import { EmptyState } from "@/components/empty-state";
import { GlossaryTooltip } from "@/components/glossary-tooltip";
import { Skeleton } from "@/components/skeleton";
import {
  listInjuryFlags,
  getExerciseLibrary,
  getTrainingHistory,
  trackProductEvent,
  deleteExternalTraining,
  deletePlanCompletion,
  listSparringLogHistory,
  listTodayCheckinHistory,
} from "@/lib/api";
import { athleteTrainingDayISO } from "@/lib/camp-map";
import { formatAppDate } from "@/lib/date-format";
import {
  checkinChips,
  countRecentBy,
  daysAgoLabel,
  HARD_SPARRING_DAYS_PER_WEEK,
  type HistoryChip,
  injuryReportedTone,
  injurySeverityTone,
  injuryStatusLabel,
  injuryStatusTone,
  recommendationLabel,
  recommendationTone,
  sessionStatusLabel,
  sessionStatusTone,
  sparringIntensityLabel,
  sparringIntensityTone,
  sparringPlanDifference,
  sparringRowMeta,
  sparringWeekNote,
  trainingDaysAgo,
} from "@/lib/history";
import { formatInjuryDetail, normalizeInjuryLabel } from "@/lib/injury-display";
import { loadHistoryWithAuthRecovery } from "@/lib/history-request";
import type { TodayDecisionTone } from "@/lib/today";
import type {
  InjuryFlagRecord,
  SparringIntensity,
  SparringLogHistoryResponse,
  SparringLogRecord,
  TodayCheckinHistoryRecord,
} from "@/lib/types";
import { LogTraining, manualSparringDraft } from "./log-training";
import { TakeawayEditor } from "./session-takeaway";
import { PlanSessionEditor } from "./plan-session-editor";
import { runSummary } from "@/lib/run-time";
import { markHistoryHintSeen } from "@/lib/history-hint";
import { SparringLogPrompt, type SparringDraft } from "./today/sparring-log-prompt";
import { TrainingSummaryCard } from "./training-summary-card";
import { TrainingRhythm } from "./training-rhythm";
import { filterTakeaways } from "@/lib/training-summary";
import { canRepeatTraining, sortTrainingHistory, type TrainingHistorySession } from "@/lib/training-history";
import { useTrainingDay } from "@/lib/use-training-day";
import { SessionDetails, type SessionDetailsCache } from "./session-details";
import { ManualExerciseLogger } from "./manual-exercise-logger";
import { ExerciseLibrary, type ExerciseHistoryCache } from "./exercise-library";
import type { ExerciseLibraryResponse } from "@/lib/api-schema.generated";

type HistoryTab = "sessions" | "exercises" | "sparring" | "checkins" | "injuries";

const TABS: Array<{ id: HistoryTab; label: string }> = [
  { id: "sessions", label: "Sessions" },
  { id: "exercises", label: "Exercises" },
  { id: "sparring", label: "Sparring" },
  { id: "checkins", label: "Check-ins" },
  { id: "injuries", label: "Injuries" },
];

type TabData<T> = {
  rows: T | null;
  error: string | null;
};

function StatusBadge({ tone, label }: { tone: string; label: string }) {
  return (
    <span className="badge history-status-badge" data-tone={tone}>
      {label}
    </span>
  );
}

function ListSkeleton() {
  return (
    <div className="history-list" aria-hidden="true">
      <Skeleton height={72} />
      <Skeleton height={72} />
      <Skeleton height={72} />
    </div>
  );
}

type SummaryStat = { label: string; value: number; tone?: TodayDecisionTone };

/** One labelled row of plain counts in a tab's at-a-glance card. */
function SummaryGroup({ label, stats }: { label: string; stats: SummaryStat[] }) {
  return (
    <div className="history-summary-group">
      <p className="history-summary-label">{label}</p>
      <dl
        className="history-summary-stats"
        style={{ "--stat-count": stats.length } as CSSProperties}
      >
        {stats.map((stat) => (
          <div key={stat.label}>
            <dt>{stat.label}</dt>
            <dd data-tone={stat.value > 0 && stat.tone !== "neutral" ? stat.tone : undefined}>
              {stat.value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** The row's date, with "Today" / "Yesterday" / "N days ago" for the last week. */
function RowDate({ day, today }: { day: string; today: string | null }) {
  const ago = today ? trainingDaysAgo(day, today) : null;
  const when = ago !== null && ago < 7 ? daysAgoLabel(day, today ?? "") : null;
  return (
    <span>
      {formatAppDate(day)}
      {when ? <span className="history-row-when"> · {when}</span> : null}
    </span>
  );
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function Chips({ chips }: { chips: HistoryChip[] }) {
  return (
    <div className="history-chips">
      {chips.map((chip) => (
        <span
          key={chip.label}
          className="history-chip"
          data-tone={chip.tone === "neutral" ? undefined : chip.tone}
        >
          {chip.label}
        </span>
      ))}
    </div>
  );
}

/** Reopen a saved manual sparring report, or add one the athlete skipped. */
function sparringReportDraft(externalSessionId: string, title: string, existing?: SparringLogRecord,
  suggested?: SparringIntensity): SparringDraft {
  return {
    source: "free", planId: null, sessionId: null, plannedIntensity: null,
    title, rounds: null, roundSeconds: null, externalSessionId, existing, suggestedIntensity: suggested,
  };
}

/** The linked sparring report's headline facts on its training session. */
function sessionSparringChips(log: SparringLogRecord): HistoryChip[] {
  return [
    { label: sparringRowMeta(log).join(" · "), tone: "neutral" },
    ...(log.head_contact === "heavy" ? [{ label: "Heavy head contact", tone: "amber" as const }] : []),
    ...(log.rocked ? [{ label: "Rocked / dropped", tone: "red" as const }] : []),
  ];
}

type ExerciseTarget = { row: TrainingHistorySession; reference?: TrainingHistorySession | null; reopenDetails?: boolean };

function SessionRows({ rows, today, token, onUpdate, onDelete, loadedFrom, loadingMore, moreError, onMore, sparringLogs, onSparringSaved, detailsCache, onExercisesChanged }: {
  rows: TrainingHistorySession[]; today: string | null; token: string | null;
  onUpdate: (row: TrainingHistorySession) => void; onDelete: (row: TrainingHistorySession) => Promise<void>;
  /** Oldest fully loaded training day; null when no older sessions remain. */
  loadedFrom: string | null; loadingMore: boolean; moreError: string | null; onMore: () => Promise<void>;
  /** Linked manual sparring reports by session id; null until loaded. */
  sparringLogs: Map<string, SparringLogRecord> | null;
  onSparringSaved: (log: SparringLogRecord) => void;
  detailsCache: SessionDetailsCache;
  /** Exercise entries changed: personal exercise records must be re-read. */
  onExercisesChanged: () => void;
}) {
  const [saved, setSaved] = useState<string | null>(null);
  // A session just repeated from History, offered optional exercises.
  const [repeated, setRepeated] = useState<{ row: TrainingHistorySession; source: TrainingHistorySession } | null>(null);
  const [exercisesFor, setExercisesFor] = useState<ExerciseTarget | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [takeawaysOnly, setTakeawaysOnly] = useState(false);
  const [search, setSearch] = useState("");
  const [sparringDraft, setSparringDraft] = useState<SparringDraft | null>(null);
  const [selected, setSelected] = useState<TrainingHistorySession | null>(null);
  async function remove(row: TrainingHistorySession) {
    if (deleting) return;
    // A plan session goes back to not done on the plan, so confirm first.
    if (row.source === "unlxck" && !window.confirm("Remove from History? This marks the session as not done on your plan.")) return;
    setDeleting(row.id);
    setError(null);
    try { await onDelete(row); }
    catch (reason) { setError(reason instanceof Error ? reason.message.replace("The plan service", "The training service") : "Unable to delete session. Try again."); }
    finally { setDeleting(null); }
  }
  if (rows.length === 0) {
    return (
      <EmptyState
        eyebrow="Session history"
        title="No sessions logged yet."
        description="Completed training from Plan and sessions marked done, modified, or skipped on Today appear here."
        example="Thu 02 Jul 2026: Done · RPE 7/10"
        primaryAction={{ label: "Open Today", href: "/today" }}
      />
    );
  }
  const week = today
    ? countRecentBy(rows.filter(row => row.source === "external" || row.completion_origin === "reported"),
      { day: (row) => row.training_day, key: (row) => row.status, today, days: 7 })
    : null;
  const takeawayCount = rows.filter(row => row.takeaway?.trim()).length;
  const shown = takeawaysOnly ? filterTakeaways(rows, search) : rows;
  return (
    <div className="history-tab-body">
      {saved ? <div className="history-saved-line"><p className="muted" role="status">{saved}</p>
        {repeated && token ? <button type="button" className="history-session-delete history-session-report" aria-haspopup="dialog"
          onClick={() => setExercisesFor({ row: repeated.row, reference: repeated.source.exercise_count ? repeated.source : null })}>
          Add exercises</button> : null}</div> : null}
      {error ? <p className="error-banner" role="alert">{error}</p> : null}
      {today ? <TrainingSummaryCard rows={rows} today={today} loadedFrom={loadedFrom}
        loading={loadingMore} error={moreError} onLoadMore={onMore}>
        {token ? <TrainingRhythm rows={rows} today={today} loadedFrom={loadedFrom} loading={loadingMore}
          error={moreError} onLoadMore={onMore} token={token} detailsCache={detailsCache} /> : null}
      </TrainingSummaryCard> : null}
      {week ? (
        <section className="history-summary" aria-label="Sessions in the last 7 days">
          <SummaryGroup
            label="Last 7 days"
            stats={[
              { label: "Done", value: week.done ?? 0, tone: "green" },
              { label: "Modified", value: week.modified ?? 0, tone: "amber" },
              { label: "Skipped", value: week.skipped ?? 0, tone: "red" },
            ]}
          />
        </section>
      ) : null}
      {takeawayCount ? (
        <div className="history-takeaway-filter">
          <button type="button" className="history-chip" aria-pressed={takeawaysOnly}
            onClick={() => { setTakeawaysOnly(value => !value); setSearch(""); }}>
            Takeaways · {takeawayCount}
          </button>
          {takeawaysOnly ? (
            <label className="history-exercise-search">Search takeaways
              <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="e.g. jab, guard, footwork" />
            </label>
          ) : null}
        </div>
      ) : null}
      <ul className="history-list">
        {shown.map((row) => (
          <li key={`${row.source}-${row.id}`} className="history-row history-session-row" data-tone={sessionStatusTone(row.status)}>
            <button type="button" className="history-session-open" aria-haspopup="dialog"
              aria-label={`View ${row.title || "session"} details for ${formatAppDate(row.training_day)}`}
              onClick={() => setSelected(row)} />
            <div className="history-row-head">
              <span className="history-row-title">{row.title?.trim() || "Session"}</span>
              <StatusBadge tone={sessionStatusTone(row.status)} label={sessionStatusLabel(row.status)} />
            </div>
            <div className="history-row-meta">
              <RowDate day={row.training_day} today={today} />
              <span>{row.source === "external" ? "Added by you" : "Unlxck"}</span>
              {row.source === "unlxck" && row.completion_origin == null ? <span>Legacy completion · workout identity unverified</span> : null}
              {runSummary(row) ? <span>{runSummary(row)}</span> : row.duration_minutes ? <span>{row.duration_minutes} min</span> : null}
              {row.source === "external" && row.exercise_count ? <span>{plural(row.exercise_count, "exercise")}</span> : null}
              {row.effort ? <span>{row.effort.charAt(0).toUpperCase() + row.effort.slice(1)}</span> : null}
              {row.session_rpe != null ? (
                <span>
                  RPE {row.session_rpe}/10
                  <GlossaryTooltip term="RPE" />
                </span>
              ) : null}
              {row.pain_after != null ? <span>Pain after {row.pain_after}/10</span> : null}
            </div>
            {row.takeaway?.trim() ? <p className="history-row-takeaway">{row.takeaway}</p> : null}
            {row.source === "external" && row.activity === "sparring" && sparringLogs?.get(row.id) ? (
              <Chips chips={sessionSparringChips(sparringLogs.get(row.id)!)} />
            ) : null}
            {row.source === "external" ? <div className="history-session-actions">
              {row.activity === "sparring" && sparringLogs && token ? (() => {
                const log = sparringLogs.get(row.id);
                return <button className="history-session-delete history-session-report" type="button" aria-haspopup="dialog"
                  aria-label={`${log ? "Update sparring report" : "Add sparring details"} for ${formatAppDate(row.training_day)}`}
                  onClick={() => setSparringDraft(log ? sparringReportDraft(row.id, row.title, log) : manualSparringDraft(row))}>
                  {log ? "Update report" : "Add details"}
                </button>;
              })() : null}
              {canRepeatTraining(row) ? <LogTraining accessToken={token} repeatEntry={row} onSaved={created => { onUpdate(created); setRepeated({ row: created, source: row }); setSaved(`${created.title} logged successfully. Saved to Training History.`); }} /> : null}
              <LogTraining accessToken={token} entry={row} onSaved={onUpdate} />
              {token && row.activity !== "sparring" ? <TakeawayEditor token={token} row={row} onSaved={onUpdate} /> : null}
              <button className="history-session-delete" type="button" disabled={deleting !== null} aria-label={`Delete ${row.title}`} onClick={() => void remove(row)}>{deleting === row.id ? "Deleting…" : "Delete"}</button>
            </div> : row.source === "unlxck" && row.history_editable && token ? <div className="history-session-actions">
              <PlanSessionEditor token={token} row={row} onSaved={onUpdate} />
              <button className="history-session-delete" type="button" disabled={deleting !== null} aria-label={`Delete ${row.title}`} onClick={() => void remove(row)}>{deleting === row.id ? "Deleting…" : "Delete"}</button>
            </div> : null}
            {row.modification_reason ? (
              <p className="muted history-row-note">Reason: {row.modification_reason}</p>
            ) : null}
            {row.notes ? <p className="muted history-row-note">Notes: {row.notes}</p> : null}
          </li>
        ))}
      </ul>
      {takeawaysOnly && !shown.length ? <p className="muted">No matching takeaways in the loaded history.</p> : null}
      {moreError ? <p className="error-banner" role="alert">{moreError}</p> : null}
      {loadedFrom !== null ? <button type="button" className="secondary-button" disabled={loadingMore} onClick={() => void onMore()}>
        {loadingMore ? "Loading…" : "Load earlier sessions"}
      </button> : null}
      {sparringDraft && token ? <SparringLogPrompt key={sparringDraft.externalSessionId} token={token} draft={sparringDraft}
        onSaved={onSparringSaved} onDismiss={() => setSparringDraft(null)} /> : null}
      {selected && token ? <SessionDetails key={`${selected.source}:${selected.id}`} row={selected} token={token}
        cache={detailsCache} onDismiss={() => setSelected(null)}
        onEditExercises={selected.source === "external" ? row => { setExercisesFor({ row, reopenDetails: true }); setSelected(null); } : undefined} /> : null}
      {exercisesFor && token ? <ManualExerciseLogger key={exercisesFor.row.id} token={token} session={exercisesFor.row} reference={exercisesFor.reference}
        onClose={entries => {
          const target = exercisesFor;
          setExercisesFor(null);
          const updated = entries ? { ...target.row, exercise_count: entries.length } : target.row;
          if (entries) {
            onExercisesChanged();
            onUpdate(updated);
            if (repeated?.row.id === updated.id) setRepeated(null);
          }
          if (target.reopenDetails) setSelected(updated);
        }} /> : null}
    </div>
  );
}

function SparringRows({ history, token, onSaved }: {
  history: SparringLogHistoryResponse; token: string | null; onSaved: (log: SparringLogRecord) => void;
}) {
  const { logs, current_training_day: today } = history;
  const [draft, setDraft] = useState<SparringDraft | null>(null);
  if (logs.length === 0) {
    return (
      <EmptyState
        eyebrow="Sparring history"
        title="No sparring logged yet."
        description="Finish a sparring round timer on Today to log your rounds. They'll show here with weekly totals."
        example="Thu 02 Jul 2026: 5 × 3 min · Hard · Light head contact"
        primaryAction={{ label: "Open Today", href: "/today" }}
      />
    );
  }
  const week = history.last_7_days;
  const month = history.last_28_days;
  const weekNote = sparringWeekNote(week);
  const lastHard = history.last_hard_day ? daysAgoLabel(history.last_hard_day, today) : null;
  const lastRocked = history.last_rocked_day ? daysAgoLabel(history.last_rocked_day, today) : null;
  return (
    <div className="history-tab-body">
      <section className="history-summary" aria-label="Sparring totals">
        <SummaryGroup
          label="Last 7 days"
          stats={[
            { label: "Rounds", value: week.rounds },
            {
              label: "Hard days",
              value: week.hard_days,
              tone: week.hard_days > HARD_SPARRING_DAYS_PER_WEEK ? "amber" : undefined,
            },
            { label: "Heavy head contact", value: week.heavy_head_contact_sessions, tone: "amber" },
          ]}
        />
        <p className="history-summary-line">
          Last 28 days: {plural(month.sessions, "session")} · {plural(month.rounds, "round")} ·{" "}
          {plural(month.hard_days, "hard day")}
          {month.rocked_count > 0 ? (
            <span data-tone="red"> · rocked / dropped {plural(month.rocked_count, "time")}</span>
          ) : null}
        </p>
        <div className="history-row-meta">
          {history.last_hard_day ? (
            <span>
              Last hard spar: {formatAppDate(history.last_hard_day)}
              {lastHard ? ` (${lastHard})` : ""}
            </span>
          ) : null}
          {history.last_rocked_day ? (
            <span>
              Last rocked / dropped: {formatAppDate(history.last_rocked_day)}
              {lastRocked ? ` (${lastRocked})` : ""}
            </span>
          ) : null}
        </div>
        {weekNote ? (
          <p className="history-summary-note" role="note">
            {weekNote}
          </p>
        ) : null}
      </section>

      <ul className="history-list">
        {logs.map((row) => {
          const difference = sparringPlanDifference(row.planned_intensity, row.intensity);
          return (
            <li
              key={row.id}
              className="history-row"
              data-tone={row.rocked ? "red" : sparringIntensityTone(row.intensity)}
            >
              <div className="history-row-head">
                <span className="history-row-date">
                  <RowDate day={row.training_day} today={today} />
                </span>
                <StatusBadge
                  tone={sparringIntensityTone(row.intensity)}
                  label={sparringIntensityLabel(row.intensity)}
                />
              </div>
              <div className="history-row-meta">
                {sparringRowMeta(row).map((item) => (
                  <span key={item}>{item}</span>
                ))}
              </div>
              {row.rocked || row.head_contact === "heavy" || difference ? (
                <Chips
                  chips={[
                    ...(row.rocked ? [{ label: "Rocked / dropped", tone: "red" as const }] : []),
                    ...(row.head_contact === "heavy"
                      ? [{ label: "Heavy head contact", tone: "amber" as const }]
                      : []),
                    ...(difference
                      ? [{ label: difference.label, tone: difference.harder ? ("amber" as const) : ("neutral" as const) }]
                      : []),
                  ]}
                />
              ) : null}
              {row.focus?.trim() ? <p className="history-row-takeaway">Focus: {row.focus}</p> : null}
              {row.notes ? <p className="muted history-row-note">Notes: {row.notes}</p> : null}
              {row.external_session_id && token ? (
                <div className="history-session-actions">
                  <button className="history-session-delete" type="button" aria-haspopup="dialog"
                    aria-label={`Update sparring report for ${formatAppDate(row.training_day)}`}
                    onClick={() => setDraft(sparringReportDraft(row.external_session_id!, "Sparring", row))}>
                    Update report
                  </button>
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
      {draft && token ? <SparringLogPrompt key={draft.externalSessionId} token={token} draft={draft}
        onSaved={onSaved} onDismiss={() => setDraft(null)} /> : null}
    </div>
  );
}

function CheckinRows({ rows, today }: { rows: TodayCheckinHistoryRecord[]; today: string | null }) {
  if (rows.length === 0) {
    return (
      <EmptyState
        eyebrow="Check-in history"
        title="No check-ins yet."
        description="Your daily readiness check-ins and the training recommendation each one produced appear here."
        example="Thu 02 Jul 2026: Train as planned · Sleep good · Body normal · Pain none"
        primaryAction={{ label: "Check in on Today", href: "/today" }}
      />
    );
  }
  const week = today
    ? countRecentBy(rows, {
        day: (row) => row.training_day,
        key: (row) => row.recommendation_state,
        today,
        days: 7,
      })
    : null;
  return (
    <div className="history-tab-body">
      {week ? (
        <section className="history-summary" aria-label="Check-ins in the last 7 days">
          <SummaryGroup
            label="Last 7 days"
            stats={[
              { label: "Train as planned", value: week.train_as_planned ?? 0, tone: "green" },
              { label: "Modify", value: week.modify ?? 0, tone: "amber" },
              { label: "Pull back", value: week.pull_back ?? 0, tone: "red" },
            ]}
          />
        </section>
      ) : null}
      <ul className="history-list">
        {rows.map((row) => (
          <li key={row.id} className="history-row" data-tone={recommendationTone(row.recommendation_state)}>
            <div className="history-row-head">
              <span className="history-row-date">
                <RowDate day={row.training_day} today={today} />
              </span>
              <StatusBadge
                tone={recommendationTone(row.recommendation_state)}
                label={recommendationLabel(row.recommendation_state)}
              />
            </div>
            <Chips chips={checkinChips(row)} />
            {row.recommendation_reason ? (
              <p className="muted history-row-note">{row.recommendation_reason.split("\n")[0]}</p>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function InjuryRow({ row }: { row: InjuryFlagRecord }) {
  const title = row.label || normalizeInjuryLabel(row.body_area) || normalizeInjuryLabel(row.description) || "Injury";
  // The stored description carries the planner's taxonomy tokens for a
  // guided-intake injury, so the note line shows only the athlete-facing
  // part of it.
  const note = formatInjuryDetail(row.description, { bodyArea: row.body_area });
  return (
    <li className="history-row" data-tone={injuryStatusTone(row.status)}>
      <div className="history-row-head">
        <span className="history-row-title">{title}</span>
        <StatusBadge tone={injuryStatusTone(row.status)} label={injuryStatusLabel(row.status)} />
      </div>
      <Chips
        chips={[
          {
            label: `${row.severity.charAt(0).toUpperCase()}${row.severity.slice(1)}`,
            tone: injurySeverityTone(row.severity),
          },
        ]}
      />
      <div className="history-row-meta">
        <span>Reported {formatAppDate(row.created_at)}</span>
        {row.resolved_at ? <span>Resolved {formatAppDate(row.resolved_at)}</span> : null}
        {!row.resolved_at && row.latest_reported_status ? (
          <span data-tone={injuryReportedTone(row.latest_reported_status)}>
            Latest: {row.latest_reported_status}
          </span>
        ) : null}
      </div>
      {note && note !== title ? <p className="muted history-row-note">{note}</p> : null}
    </li>
  );
}

function InjuryRows({ rows }: { rows: InjuryFlagRecord[] }) {
  if (rows.length === 0) {
    return (
      <EmptyState
        eyebrow="Injury history"
        title="No injuries reported."
        description="Injuries you report during check-ins stay here, including resolved ones, so you have your full injury record."
        example="Left knee: Moderate · Open since Mon 15 Jun 2026"
        primaryAction={{ label: "Report on Today", href: "/today" }}
      />
    );
  }
  const active = rows.filter((row) => row.status !== "resolved");
  const resolved = rows.filter((row) => row.status === "resolved");
  return (
    <div className="history-tab-body">
      <section className="history-summary" aria-label="Injuries">
        <SummaryGroup
          label="Injuries"
          stats={[
            { label: "Active", value: active.length, tone: "amber" },
            { label: "Resolved", value: resolved.length, tone: "green" },
          ]}
        />
      </section>
      {active.length > 0 ? (
        <>
          <h2 className="history-section-title">Active</h2>
          <ul className="history-list">
            {active.map((row) => (
              <InjuryRow key={row.id} row={row} />
            ))}
          </ul>
        </>
      ) : null}
      {resolved.length > 0 ? (
        <>
          <h2 className="history-section-title">Resolved</h2>
          <ul className="history-list">
            {resolved.map((row) => (
              <InjuryRow key={row.id} row={row} />
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}

export function HistoryScreen() {
  const { session, me } = useAppSession();
  // Reaching History is what the one-time hint points to.
  const athleteId = me?.profile.athlete_id ?? "";
  useEffect(() => { markHistoryHintSeen(athleteId); }, [athleteId]);
  return <HistoryContent key={`${session?.access_token ?? "anonymous"}:${me?.profile.health_consent_granted ?? "unknown"}`} />;
}

function HistoryContent() {
  const { session, me } = useAppSession();
  const token = session?.access_token ?? null;
  const userId = session?.user_id ?? null;
  const [detailsCache] = useState<SessionDetailsCache>(() => new Map());
  // The training day for the "last 7 days" counts, in the athlete's own
  // timezone so it matches the server-stamped days on every row even when the
  // device is set elsewhere. useTrainingDay only supplies the client-only
  // mount gate and the minute tick that rolls the day over.
  const trainingDay = useTrainingDay();
  const today = trainingDay ? athleteTrainingDayISO(new Date(), me?.profile.athlete_timezone) : null;

  const [tab, setTab] = useState<HistoryTab>("sessions");
  const [sessions, setSessions] = useState<TabData<TrainingHistorySession[]>>({
    rows: null,
    error: null,
  });
  const [exercises, setExercises] = useState<TabData<ExerciseLibraryResponse>>({ rows: null, error: null });
  const [exerciseHistoryCache] = useState<ExerciseHistoryCache>(() => new Map());
  const [sparring, setSparring] = useState<TabData<SparringLogHistoryResponse>>({
    rows: null,
    error: null,
  });
  const [checkins, setCheckins] = useState<TabData<TodayCheckinHistoryRecord[]>>({
    rows: null,
    error: null,
  });
  const [injuries, setInjuries] = useState<TabData<InjuryFlagRecord[]>>({ rows: null, error: null });
  // Sessions page back by whole training days from this cursor.
  const [sessionCursor, setSessionCursor] = useState<string | null>(null);
  const [loadingMoreSessions, setLoadingMoreSessions] = useState(false);
  const [moreSessionsError, setMoreSessionsError] = useState<string | null>(null);
  const hasManualSparring = Boolean(sessions.rows?.some(row => row.source === "external" && row.activity === "sparring"));
  const sparringBySession = useMemo(() => sparring.rows ? new Map(sparring.rows.logs
    .filter(log => log.external_session_id).map(log => [log.external_session_id!, log])) : null, [sparring.rows]);

  // Load each tab on first open, then retain its cache for this signed-in visit.
  // Retry clears the error and reopens the same effect. The keyed parent drops
  // every cache immediately when the account/token changes.
  useEffect(() => {
    if (!token) {
      return;
    }
    let cancelled = false;

    const load = async <T,>(
      current: TabData<T>,
      fetcher: (accessToken: string) => Promise<T>,
      set: (data: TabData<T>) => void,
    ) => {
      if (current.rows !== null || current.error !== null) {
        return;
      }
      try {
        const rows = await loadHistoryWithAuthRecovery(
          { access_token: token, user_id: userId },
          fetcher,
          () => cancelled,
        );
        if (!cancelled) {
          set({ rows, error: null });
          void trackProductEvent(token, "history_opened", "history");
        }
      } catch (error) {
        if (!cancelled) {
          set({ rows: null, error: error instanceof Error ? error.message : "Unable to load history." });
        }
      }
    };

    if (tab === "sessions") {
      void load(sessions, async accessToken => {
        const page = await getTrainingHistory(accessToken, true);
        if (!cancelled) setSessionCursor(page.next_before_day ?? null);
        return page.sessions;
      }, setSessions);
      // Manual sparring sessions show their linked report, so load those too.
      if (hasManualSparring) void load(sparring, listSparringLogHistory, setSparring);
    } else if (tab === "exercises") {
      void load(exercises, getExerciseLibrary, setExercises);
    } else if (tab === "sparring") {
      void load(sparring, listSparringLogHistory, setSparring);
    } else if (tab === "checkins") {
      void load(checkins, listTodayCheckinHistory, setCheckins);
    } else {
      void load(injuries, (accessToken) => listInjuryFlags(accessToken, true), setInjuries);
    }
    return () => {
      cancelled = true;
    };
  }, [tab, token, userId, sessions, exercises, sparring, checkins, injuries, hasManualSparring]);

  // Stable, and guarded by a ref, so the summary can ask for pages from an effect.
  const loadingMore = useRef(false);
  const loadMoreSessions = useCallback(async () => {
    if (!token || !sessionCursor || loadingMore.current) return;
    const before = sessionCursor;
    loadingMore.current = true;
    setLoadingMoreSessions(true);
    setMoreSessionsError(null);
    try {
      const page = await loadHistoryWithAuthRecovery({ access_token: token, user_id: userId },
        accessToken => getTrainingHistory(accessToken, true, before));
      setSessions(previous => {
        const seen = new Set((previous.rows ?? []).map(row => `${row.source}-${row.id}`));
        const older = page.sessions.filter(row => !seen.has(`${row.source}-${row.id}`));
        return { ...previous, rows: [...(previous.rows ?? []), ...sortTrainingHistory(older)] };
      });
      setSessionCursor(page.next_before_day ?? null);
    } catch (error) {
      setMoreSessionsError(error instanceof Error ? error.message : "Unable to load earlier sessions.");
    } finally {
      loadingMore.current = false;
      setLoadingMoreSessions(false);
    }
  }, [token, userId, sessionCursor]);

  function onSparringSaved(log: SparringLogRecord) {
    if (log.external_session_id) detailsCache.delete(`external:${log.external_session_id}`);
    // Show the corrected report straight away, then refresh the windowed totals.
    setSparring(previous => previous.rows ? {
      ...previous,
      rows: { ...previous.rows, logs: [log, ...previous.rows.logs.filter(item => item.id !== log.id)]
        .sort((a, b) => b.training_day.localeCompare(a.training_day)) },
    } : previous);
    if (!token) return;
    void loadHistoryWithAuthRecovery({ access_token: token, user_id: userId }, listSparringLogHistory)
      .then(rows => setSparring({ rows, error: null }))
      .catch(() => { /* The updated row is already shown; totals refresh on the next visit. */ });
  }

  const active =
    tab === "sessions"
      ? sessions
      : tab === "exercises"
        ? exercises
      : tab === "sparring"
        ? sparring
        : tab === "checkins"
          ? checkins
          : injuries;

  function retryActiveTab() {
    if (tab === "sessions") setSessions({ rows: null, error: null });
    else if (tab === "exercises") setExercises({ rows: null, error: null });
    else if (tab === "sparring") setSparring({ rows: null, error: null });
    else if (tab === "checkins") setCheckins({ rows: null, error: null });
    else setInjuries({ rows: null, error: null });
  }
  useOnReconnect(retryActiveTab, Boolean(active.error));

  return (
    <section className="panel history-screen">
      <header className="form-section-header">
        <p className="kicker">Training record</p>
        <h1 className="form-section-title">History</h1>
        <p className="muted">
          Your sessions, sparring, check-ins and injuries.
        </p>
      </header>

      <div className="history-tabs" role="tablist" aria-label="History sections">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            className={`history-tab${tab === item.id ? " history-tab-active" : ""}`}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {active.error ? (
        <div className="error-banner" role="alert">
          <p>{active.error}</p>
          <button
            type="button"
            className="error-banner-retry"
            onClick={retryActiveTab}
          >
            Retry
          </button>
        </div>
      ) : active.rows === null ? (
        <ListSkeleton />
      ) : tab === "sessions" ? (
        <SessionRows rows={sessions.rows ?? []} today={today} token={token} detailsCache={detailsCache}
          onExercisesChanged={() => { exerciseHistoryCache.clear(); setExercises({ rows: null, error: null }); }}
          loadedFrom={sessionCursor} loadingMore={loadingMoreSessions} moreError={moreSessionsError}
          onMore={loadMoreSessions} sparringLogs={sparringBySession} onSparringSaved={onSparringSaved}
          onUpdate={row => {
            detailsCache.delete(`${row.source}:${row.id}`);
            setSessions(previous => {
              // Edits and takeaways don't recount exercises; keep the known count.
              const known = previous.rows?.find(item => item.source === row.source && item.id === row.id)?.exercise_count;
              const next = { ...row, exercise_count: row.exercise_count ?? known };
              return { ...previous, rows: sortTrainingHistory([next, ...(previous.rows ?? []).filter(item => !(item.source === row.source && item.id === row.id))]) };
            });
          }}
          onDelete={async row => {
            if (!token) return;
            if (row.source === "unlxck") await deletePlanCompletion(token, row.id);
            else await deleteExternalTraining(token, row.id);
            detailsCache.delete(`${row.source}:${row.id}`);
            setSessions(previous => ({ ...previous, rows: previous.rows?.filter(item => !(item.source === row.source && item.id === row.id)) ?? null }));
          }} />
      ) : tab === "exercises" ? (
        exercises.rows && token ? <ExerciseLibrary library={exercises.rows} token={token} cache={exerciseHistoryCache} /> : <ListSkeleton />
      ) : tab === "sparring" ? (
        sparring.rows ? <SparringRows history={sparring.rows} token={token} onSaved={onSparringSaved} /> : <ListSkeleton />
      ) : tab === "checkins" ? (
        <CheckinRows rows={checkins.rows ?? []} today={today} />
      ) : (
        <InjuryRows rows={injuries.rows ?? []} />
      )}
    </section>
  );
}
