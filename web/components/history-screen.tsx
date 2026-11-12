"use client";

import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useAppSession } from "@/components/auth-provider";
import { useOnReconnect } from "@/components/connection-status-banner";
import { EmptyState } from "@/components/empty-state";
import { Skeleton } from "@/components/skeleton";
import {
  listInjuryFlags,
  getExerciseLibrary,
  getTrainingHistory,
  trackProductEvent,
  HISTORY_SESSIONS_PATH,
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
import { recallView } from "@/lib/shared-reads";
import type { TodayDecisionTone } from "@/lib/today";
import type { TrainingHistoryPage } from "@/lib/training-history";
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
import { useSeenOnce } from "./once-hint";
import { SparringLogPrompt, type SparringDraft } from "./today/sparring-log-prompt";
import { TrainingSummaryCard } from "./training-summary-card";
import { TrainingRhythm } from "./training-rhythm";
import { filterTakeaways } from "@/lib/training-summary";
import { trainingTitle } from "@/lib/training-disciplines";
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
  const [selectedDay, setSelectedDay] = useState("");
  const [exercisesFor, setExercisesFor] = useState<ExerciseTarget | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [takeawaysOnly, setTakeawaysOnly] = useState(false);
  const [search, setSearch] = useState("");
  const [sparringDraft, setSparringDraft] = useState<SparringDraft | null>(null);
  const [selected, setSelected] = useState<TrainingHistorySession | null>(null);
  const [editHint, dismissEditHint] = useSeenOnce("discovery-history-edit-seen");
  // A run's edit sheet opened from its details, which reopen once it closes.
  const [editRun, setEditRun] = useState<TrainingHistorySession | null>(null);
  const followUpSparring = (row: TrainingHistorySession) => setSparringDraft(manualSparringDraft(row));
  async function remove(row: TrainingHistorySession) {
    if (deleting) return false;
    // A plan session goes back to not done on the plan, so confirm first.
    if (row.source === "unlxck" && !window.confirm("Remove from History? This marks the session as not done on your plan.")) return false;
    setDeleting(row.id);
    setError(null);
    try { await onDelete(row); return true; }
    catch (reason) { setError(reason instanceof Error ? reason.message.replace("The plan service", "The training service") : "Unable to delete session. Try again."); return false; }
    finally { setDeleting(null); }
  }
  const week = today
    ? countRecentBy(rows.filter(row => row.source === "external" || row.completion_origin === "reported"),
      { day: (row) => row.training_day, key: (row) => row.status, today, days: 7 })
    : null;
  const takeawayCount = rows.filter(row => row.takeaway?.trim()).length;
  const shown = takeawaysOnly ? filterTakeaways(rows, search) : rows;
  return (
    <div className="history-tab-body">
      <div className="history-day-picker">
        <label className="history-date-control">
          <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="3" /><path d="M7 3v4m10-4v4M3 11h18m-13 4h2m4 0h2m-8 3h2" /></svg>
          <span>{selectedDay ? formatAppDate(selectedDay) : "Choose date"}</span>
          <svg className="history-date-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="m5 9 7 7 7-7" /></svg>
          <input type="date" aria-label="Training day" value={selectedDay} max={today ?? undefined}
            onClick={event => { try { event.currentTarget.showPicker?.(); } catch { /* Native date input remains available. */ } }}
            onChange={event => { const day = event.target.value; if (!day || (today && day <= today)) setSelectedDay(day); }} />
        </label>
        {selectedDay ? <button type="button" className="history-date-clear" onClick={() => setSelectedDay("")}>All days <span aria-hidden="true">×</span></button> : null}
      </div>
      {error ? <p className="error-banner" role="alert">{error}</p> : null}
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
      {editHint && rows.some(row => row.source === "external" || row.history_editable) && !selected ? <div className="history-discovery-hint" role="note">
        <span>Made a mistake? Open a session to review it, then use Edit when available.</span>
        <button type="button" onClick={dismissEditHint} aria-label="Dismiss session editing tip">×</button>
      </div> : null}
      {(selectedDay ? [selectedDay] : [...new Set(shown.map(row => row.training_day))]).map(day => (
        <section className="history-day-group" key={day} aria-label={formatAppDate(day)}>
          <div className="history-day-heading">
            <h2><RowDate day={day} today={today} /></h2>
            {token && today && day <= today ? <LogTraining onSparringFollowUp={followUpSparring} accessToken={token} initialTrainingDay={day} historyMode onSaved={onUpdate} /> : null}
          </div>
          <ul className="history-list">
            {shown.filter(row => row.training_day === day).map(row => (
              <li key={`${row.source}-${row.id}`} className="history-row history-session-row" data-tone={sessionStatusTone(row.status)}>
                <button type="button" className="history-session-open" aria-haspopup="dialog"
                  aria-label={`View ${trainingTitle(row) || "session"} details for ${formatAppDate(row.training_day)}`}
                  onClick={() => { dismissEditHint(); setSelected(row); }} />
                <div className="history-row-head">
                  <span className="history-row-title">{trainingTitle(row)?.trim() || "Session"}</span>
                  <StatusBadge tone={sessionStatusTone(row.status)} label={sessionStatusLabel(row.status)} />
                </div>
                <div className="history-row-meta">
                  {runSummary(row) ? <span>{runSummary(row)}</span> : row.duration_minutes ? <span>{row.duration_minutes} min</span> : null}
                  {row.source === "external" && row.exercise_count ? <span>{plural(row.exercise_count, "exercise")}</span> : null}
                  {row.session_rpe != null ? <span>RPE {row.session_rpe}/10</span> : null}
                </div>
                {row.takeaway?.trim() ? <p className="history-row-takeaway">{row.takeaway}</p> : null}
              </li>
            ))}
          </ul>
          {!shown.some(row => row.training_day === day) ? <p className="muted">No sessions loaded for this day. Log completed training above.</p> : null}
        </section>
      ))}
      {!rows.length && !selectedDay ? <p className="muted">No sessions logged yet. Choose a training day to add completed activity.</p> : null}
      {takeawaysOnly && !shown.length ? <p className="muted">No matching takeaways in the loaded history.</p> : null}
      <details className="history-trends"><summary>Training summary & trends</summary>
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
      </details>
      {moreError ? <p className="error-banner" role="alert">{moreError}</p> : null}
      {loadedFrom !== null ? <button type="button" className="secondary-button" disabled={loadingMore} onClick={() => void onMore()}>
        {loadingMore ? "Loading…" : "Load earlier sessions"}
      </button> : null}
      {sparringDraft && token ? <SparringLogPrompt key={sparringDraft.externalSessionId} token={token} draft={sparringDraft}
        onSaved={onSparringSaved} onDismiss={() => setSparringDraft(null)} /> : null}
      {selected && token ? <SessionDetails key={`${selected.source}:${selected.id}`} row={selected} token={token}
        cache={detailsCache} onDismiss={() => setSelected(null)}
        actions={<div className="history-detail-actions">
          {error ? <p className="error-banner" role="alert">{error}</p> : null}

          {selected.source === "external" ? <>
            <LogTraining onSparringFollowUp={followUpSparring} accessToken={token} entry={selected} historyMode onSaved={row => { onUpdate(row); setSelected(null); }} />
            {canRepeatTraining(selected) ? <LogTraining onSparringFollowUp={followUpSparring} accessToken={token} repeatEntry={selected} historyMode onSaved={row => { onUpdate(row); setSelected(null); }} /> : null}
            <TakeawayEditor token={token} row={selected} onSaved={row => { onUpdate(row); setSelected(null); }} />
            {selected.activity === "sparring" && sparringLogs ? <button type="button" className="history-session-delete" onClick={() => {
              const log = sparringLogs.get(selected.id);
              setSparringDraft(log ? sparringReportDraft(selected.id, selected.title, log) : manualSparringDraft(selected)); setSelected(null);
            }}>Sparring details</button> : null}
          </> : selected.history_editable ? <PlanSessionEditor token={token} row={selected} onSaved={row => { onUpdate(row); setSelected(null); }} /> : null}
          {selected.source === "external" || selected.history_editable ? <details className="history-actions-menu"><summary aria-label="More session actions">···</summary>
          <div className="history-actions-items"><button className="history-session-delete" type="button" disabled={deleting !== null}
            onClick={() => { void remove(selected).then(removed => { if (removed) setSelected(null); }); }}>Delete session</button></div></details> : null}
        </div>}
        onEditRun={row => { setEditRun(row); setSelected(null); }}
        onEditExercises={selected.source === "external" ? row => { setExercisesFor({ row, reopenDetails: true }); setSelected(null); } : undefined} /> : null}
      {editRun && token ? <LogTraining onSparringFollowUp={followUpSparring} key={`edit-${editRun.id}`} accessToken={token} entry={editRun} autoOpen historyMode
        onSaved={updated => { detailsCache.delete(`external:${updated.id}`); onUpdate(updated); setEditRun(updated); }}
        onClosed={() => { setSelected(editRun); setEditRun(null); }} /> : null}
      {exercisesFor && token ? <ManualExerciseLogger key={exercisesFor.row.id} token={token} session={exercisesFor.row} reference={exercisesFor.reference}
        onClose={entries => {
          const target = exercisesFor;
          setExercisesFor(null);
          const updated = entries ? { ...target.row, exercise_count: entries.length } : target.row;
          if (entries) {
            onExercisesChanged();
            onUpdate(updated);
          }
          if (target.reopenDetails) setSelected(updated);
        }} /> : null}
    </div>
  );
}

/** Completed plan sparring remains history even without a separate rounds report. */
export function unreportedSparringSessions(sessions: TrainingHistorySession[], logs: SparringLogRecord[]): TrainingHistorySession[] {
  const reported = new Set(logs.map(log => log.external_session_id).filter(Boolean));
  return sessions.filter(row => {
    if (row.status === "skipped" || row.completion_origin === "day_credit") return false;
    if (row.source === "external") return row.activity === "sparring" && !reported.has(row.id);
    // Match the completed session, never other work alongside contact that day.
    const isSparring = row.activity === "sparring" || /^(?:(?:boxing|muay thai|kickboxing|mma|bjj|grappling|wrestling)\s*(?:[·—–:-]\s*)?)?(?:hard |light |technical )?sparring(?:$|[\s(—–:-])/i.test(row.title.trim());
    return isSparring && !logs.some(log => log.plan_id === row.plan_id
      && log.session_id === row.session_id && log.training_day === row.training_day);
  });
}

function SparringRows({ history, sessions, token, onSaved, hasMore }: {
  history: SparringLogHistoryResponse; sessions: TrainingHistorySession[]; token: string | null;
  onSaved: (log: SparringLogRecord) => void; hasMore: boolean;
}) {
  const { logs, current_training_day: today } = history;
  const [draft, setDraft] = useState<SparringDraft | null>(null);
  const unreported = unreportedSparringSessions(sessions, logs);
  const items = [
    ...logs.map(log => ({ day: log.training_day, log, session: null as TrainingHistorySession | null })),
    ...unreported.map(session => ({ day: session.training_day, log: null as SparringLogRecord | null, session })),
  ].sort((a, b) => b.day.localeCompare(a.day));
  if (items.length === 0) {
    return (
      <EmptyState
        eyebrow="Sparring history"
        title={hasMore ? "No sparring in the loaded history." : "No sparring logged yet."}
        description={hasMore ? "Load earlier sessions to find older sparring completions." : "Complete a sparring session or log your sparring on Today to see it here."}
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
    <div className="history-tab-body history-records">
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
        {items.map(({ log: row, session }) => {
          if (!row) {
            const manual = session!;
            return (
              <li key={`manual-${manual.id}`} className="history-row" data-tone="neutral">
                <div className="history-row-head">
                  <span className="history-row-date"><RowDate day={manual.training_day} today={today} /></span>
                  <StatusBadge tone="neutral" label="No details" />
                </div>
                <div className="history-row-meta">
                  <span>{manual.title?.trim() || "Sparring"}</span>
                  <span>{manual.source === "unlxck" ? "UNLXCK plan" : "Added by you"}</span>
                  {manual.duration_minutes ? <span>{manual.duration_minutes} min</span> : null}
                  {manual.effort ? <span>{manual.effort.charAt(0).toUpperCase() + manual.effort.slice(1)}</span> : null}
                </div>
                {manual.takeaway?.trim() ? <p className="history-row-takeaway">{manual.takeaway}</p> : null}
                {token && manual.source === "external" ? (
                  <div className="history-session-actions">
                    <button className="history-session-delete" type="button" aria-haspopup="dialog"
                      aria-label={`Add sparring details for ${formatAppDate(manual.training_day)}`}
                      onClick={() => setDraft(manualSparringDraft(manual))}>
                      Add details
                    </button>
                  </div>
                ) : null}
              </li>
            );
          }
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
    <div className="history-tab-body history-records">
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
      <div className="history-row-meta">
        <span data-tone={injurySeverityTone(row.severity)}>{row.severity.charAt(0).toUpperCase() + row.severity.slice(1)}</span>
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
    <div className="history-tab-body history-records">
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
  // A revisit shows the sessions page this visit last read at once, then reads
  // it again. History is a training log, not a safety decision, and any write
  // drops the remembered page (lib/shared-reads.ts), so an edit is never undone
  // on screen by an older copy.
  const [recalledSessions] = useState(() => token ? recallView<TrainingHistoryPage>(token, HISTORY_SESSIONS_PATH) : null);
  const sessionsRefreshDue = useRef(Boolean(recalledSessions));
  const [sessions, setSessions] = useState<TabData<TrainingHistorySession[]>>(() => ({
    rows: recalledSessions?.sessions ?? null,
    error: null,
  }));
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
  const [sessionCursor, setSessionCursor] = useState<string | null>(() => recalledSessions?.next_before_day ?? null);
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
      // Both completed plan sparring and manual sparring belong in this tab.
      void load(sessions, async accessToken => {
        const page = await getTrainingHistory(accessToken, true);
        if (!cancelled) setSessionCursor(page.next_before_day ?? null);
        return page.sessions;
      }, setSessions);
    } else if (tab === "checkins") {
      void load(checkins, listTodayCheckinHistory, setCheckins);
    } else {
      void load(injuries, (accessToken) => listInjuryFlags(accessToken, true), setInjuries);
    }
    return () => {
      cancelled = true;
    };
  }, [tab, token, userId, sessions, exercises, sparring, checkins, injuries, hasManualSparring]);

  // The background read behind a remembered page: it replaces the rows when it
  // lands, and on failure the remembered rows simply stay.
  useEffect(() => {
    if (!token || !sessionsRefreshDue.current) return;
    let cancelled = false;
    loadHistoryWithAuthRecovery({ access_token: token, user_id: userId }, accessToken => getTrainingHistory(accessToken, true), () => cancelled)
      .then(page => {
        if (cancelled) return;
        sessionsRefreshDue.current = false;
        setSessions({ rows: page.sessions, error: null });
        setSessionCursor(page.next_before_day ?? null);
        void trackProductEvent(token, "history_opened", "history");
      })
      .catch(() => { /* The remembered rows stay; the next visit reads again. */ });
    return () => { cancelled = true; };
  }, [token, userId]);

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
        ? { error: sparring.error ?? sessions.error, rows: sparring.rows !== null && sessions.rows !== null ? sparring.rows : null }
        : tab === "checkins"
          ? checkins
          : injuries;

  function retryActiveTab() {
    if (tab === "sessions") setSessions({ rows: null, error: null });
    else if (tab === "exercises") setExercises({ rows: null, error: null });
    else if (tab === "sparring") {
      if (sparring.error) setSparring({ rows: null, error: null });
      if (sessions.error) setSessions({ rows: null, error: null });
    } else if (tab === "checkins") setCheckins({ rows: null, error: null });
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
        sparring.rows ? <>
          <SparringRows history={sparring.rows} sessions={sessions.rows ?? []} token={token} onSaved={onSparringSaved} hasMore={sessionCursor !== null} />
          {moreSessionsError ? <p className="error-banner" role="alert">{moreSessionsError}</p> : null}
          {sessionCursor !== null ? <button type="button" className="secondary-button" disabled={loadingMoreSessions}
            onClick={() => void loadMoreSessions()}>
            {loadingMoreSessions ? "Loading…" : moreSessionsError ? "Retry loading earlier sessions" : "Load earlier sessions"}
          </button> : null}
        </> : <ListSkeleton />
      ) : tab === "checkins" ? (
        <CheckinRows rows={checkins.rows ?? []} today={today} />
      ) : (
        <InjuryRows rows={injuries.rows ?? []} />
      )}
    </section>
  );
}
