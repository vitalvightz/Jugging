"use client";

import Link from "next/link";
import { type CSSProperties, useEffect, useState } from "react";

import { RequireAuth } from "@/components/auth-guard";
import { useAppSession } from "@/components/auth-provider";
import { useOnReconnect } from "@/components/connection-status-banner";
import { SafetyNote } from "@/components/safety-note";
import { Skeleton } from "@/components/skeleton";
import { useToast } from "@/components/toast-provider";
import { TargetSheet, WeighInSheet } from "@/components/weigh-in-sheet";
import { WeightTrendChart } from "@/components/weight-trend-chart";
import { getNutritionCurrent, trackProductEvent, updateNutritionCurrent } from "@/lib/api";
import { formatAppDate } from "@/lib/date-format";
import {
  type BodyweightRange,
  createBodyweightEntryKey,
  formatFastedState,
  formatWeight,
  formatWeightDelta,
  getBodyweightEntriesWithIndex,
  getBodyweightTrend,
  getCampWeight,
  getTargetGap,
  getTrendChange,
  RANGE_DAY_WINDOW,
} from "@/lib/nutrition-bodyweight";
import { localDateValue, type TargetAndWeighIn, withBodyweightLog, withTargetAndWeighIn } from "@/lib/nutrition-workspace";
import { WEIGHT_CUT_SAFETY } from "@/lib/safety-copy";
import type { NutritionBodyweightLogEntry, NutritionDerivedState, NutritionWorkspaceState } from "@/lib/types";
import { weighInMoment } from "@/lib/weight-cut-status";
import progress from "./training-progress.module.css";
import summary from "./training-summary.module.css";
import styles from "./weight-tracker.module.css";

const RANGES: Array<{ value: BodyweightRange; label: string }> = [
  { value: "7D", label: "7 days" },
  { value: "30D", label: "30 days" },
  { value: "All", label: "All" },
];
const LIST_PAGE = 10;

type Editing = { mode: "new" } | { mode: "edit"; sourceIndex: number };

const plural = (count: number, noun: string) => `${count} ${noun}${count === 1 ? "" : "s"}`;

function describeGap(gap: number): { value: string; label: string; tone?: "green" } {
  if (gap === 0) return { value: "0.0 kg", label: "On target", tone: "green" };
  return { value: `${Math.abs(gap).toFixed(1)} kg`, label: gap > 0 ? "Above target" : "Under target", tone: gap < 0 ? "green" : undefined };
}

function describeDaysOut(days: number): string {
  return days === 0 ? "Today" : `${days}`;
}

/**
 * The plan's own weight-cut band, read from the API, never recalculated here.
 * Only an escalated cut (high or severe) earns a warning; the copy names who
 * should oversee it and never says how to cut.
 */
function cutWarning(derived: NutritionDerivedState): string | null {
  const band = derived.weight_cut_risk_band ?? "none";
  if (band !== "high" && band !== "severe" && !derived.weight_cut_supervision_required) return null;
  const days = derived.days_until_fight;
  const context = `${derived.weight_cut_pct.toFixed(1)}% of your bodyweight to target${days != null && days >= 0 ? ` with ${plural(days, "day")} to go` : ""}`;
  return band === "severe"
    ? `Severe weight cut: ${context}. Don't attempt this without your coach or a medical professional overseeing it.`
    : `High-strain weight cut: ${context}. A cut like this needs supervision from your coach or a qualified practitioner.`;
}

export function WeightTracker() {
  const { session, me } = useAppSession();
  const { showToast } = useToast();
  // Server-derived age band. Under-18s get no weight-cut feature, so the target
  // weight and the gap to it are not shown; the backend strips the stored value too.
  const isMinorAthlete = Boolean(me?.profile.is_minor);
  const [workspace, setWorkspace] = useState<NutritionWorkspaceState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [range, setRange] = useState<BodyweightRange>("30D");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [targetOpen, setTargetOpen] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const loadFailed = !workspace && Boolean(error);
  const retryLoad = () => {
    setError(null);
    setReloadKey((key) => key + 1);
  };
  useOnReconnect(retryLoad, loadFailed);

  useEffect(() => {
    if (!session?.access_token) return;
    let active = true;
    getNutritionCurrent(session.access_token)
      .then((nextWorkspace) => {
        if (!active) return;
        void trackProductEvent(session.access_token, "nutrition_opened", "nutrition");
        setWorkspace(nextWorkspace);
      })
      .catch((loadError) => {
        if (!active) return;
        setError(loadError instanceof Error ? loadError.message : "Unable to load your logged weights.");
      });
    return () => {
      active = false;
    };
  }, [reloadKey, session?.access_token]);

  async function saveEntries(nextEntries: NutritionBodyweightLogEntry[]) {
    if (!session?.access_token || !workspace) throw new Error("Your session has expired. Sign in again.");
    setWorkspace(await updateNutritionCurrent(session.access_token, withBodyweightLog(workspace, nextEntries)));
  }

  async function saveTarget(values: TargetAndWeighIn) {
    if (!session?.access_token || !workspace) throw new Error("Your session has expired. Sign in again.");
    setWorkspace(await updateNutritionCurrent(session.access_token, withTargetAndWeighIn(workspace, values)));
  }

  const entries = workspace?.nutrition_monitoring.daily_bodyweight_log ?? [];
  const newestFirst = getBodyweightEntriesWithIndex(entries);
  // A future-dated weigh-in (only possible from before dates were checked) is
  // listed but never headlined, matching the API, which never uses it for camp weight.
  const today = localDateValue();
  const latest = newestFirst.find(entry => entry.date <= today) ?? null;
  // Camp calculations (gap, cut band, the plan) use the camp's current weight.
  // It follows the newest weigh-in unless a later weight was set in Camp Setup.
  const campWeight = workspace ? getCampWeight(workspace) : null;
  const campWeightDiffers = latest != null && campWeight != null && Math.abs(campWeight - latest.weight_kg) >= 0.05;
  const targetKg = isMinorAthlete ? null : (workspace?.shared_camp_context.target_weight_kg ?? null);
  const gap = getTargetGap(campWeight, targetKg);
  const derived = workspace?.derived ?? null;
  const average = derived?.rolling_7_day_average_weight ?? null;
  const pace = derived?.weekly_pace_kg ?? null;
  const daysOut = derived?.days_until_fight ?? null;
  const warning = derived && !isMinorAthlete ? cutWarning(derived) : null;
  const trend = getBodyweightTrend(entries, range);
  const trendChange = getTrendChange(trend);
  const keyOf = (entry: (typeof newestFirst)[number]) => createBodyweightEntryKey(entry, entry.sourceIndex);
  const selectedIndex = Math.max(0, trend.findIndex(entry => keyOf(entry) === selectedKey));
  const selected = trend.length ? trend[selectedKey ? selectedIndex : trend.length - 1] : null;
  // Change from the weigh-in before each one, across the whole log.
  const previousOf = (entry: (typeof newestFirst)[number]) => newestFirst[newestFirst.findIndex(item => item.sourceIndex === entry.sourceIndex) + 1] ?? null;
  const selectedPrevious = selected ? previousOf(selected) : null;
  const listed = showAll ? newestFirst : newestFirst.slice(0, LIST_PAGE);
  const tiles = [
    ...(gap != null ? [describeGap(gap)] : []),
    { value: average != null ? average.toFixed(1) : "--", label: "7-day average" },
    ...(pace != null ? [{ value: formatWeightDelta(pace), label: "Per week" }] : []),
    ...(daysOut != null && daysOut >= 0 ? [{ value: describeDaysOut(daysOut), label: daysOut === 0 ? "Fight day" : daysOut === 1 ? "Day to fight" : "Days to fight" }] : []),
  ];
  const editingEntry = editing?.mode === "edit" ? entries[editing.sourceIndex] ?? null : null;
  const shared = workspace?.shared_camp_context ?? null;
  const weighIn = shared ? weighInMoment(shared.fight_date, shared.weigh_in_type, shared.weigh_in_time) : null;

  return (
    <RequireAuth>
      <section className={`panel history-screen ${styles.screen}`}>
        <header className="form-section-header">
          <p className="kicker">Nutrition</p>
          <h1 className="form-section-title">Weight tracker</h1>
          <p className="muted">Your logged weights and your trend toward fight weight.</p>
        </header>

        {!workspace ? (
          loadFailed ? (
            <div className="error-banner" role="alert">
              <p>{error}</p>
              <button type="button" className="error-banner-retry" onClick={retryLoad}>Retry</button>
            </div>
          ) : (
            <div className={styles.loading} aria-busy="true" aria-label="Loading logged weights">
              <Skeleton height={220} style={{ borderRadius: 14 }} />
              <Skeleton height={300} style={{ borderRadius: 14 }} />
            </div>
          )
        ) : (
          <>
            <section className={`history-summary ${styles.card}`} aria-label="Weight summary">
              <div className={summary.head}>
                <p className="history-summary-label">Latest weight</p>
                {latest ? <span className={styles.lastLogged}>Last logged {formatAppDate(latest.date)}</span> : null}
              </div>
              {latest ? (
                <>
                  <div className={styles.headline}>
                    <strong>{latest.weight_kg.toFixed(1)}</strong>
                    <span>kg<br />{latest.is_fasted == null ? "Latest weight" : `${formatFastedState(latest.is_fasted)} weight`}</span>
                  </div>
                  <dl className={`history-summary-stats ${summary.stats}`} style={{ "--stat-count": tiles.length > 3 ? 2 : tiles.length } as CSSProperties}>
                    {tiles.map(tile => <div key={tile.label}><dt>{tile.label}</dt><dd data-tone={"tone" in tile ? tile.tone : undefined}>{tile.value}</dd></div>)}
                  </dl>
                  {campWeightDiffers ? (
                    <p className={progress.note}>
                      Your camp weight is <strong>{formatWeight(campWeight)}</strong>, set in <Link href="/onboarding" className="auth-text-link">Camp Setup</Link>.
                      {" "}Your plan{gap != null ? ", gap to target" : ""} and cut warnings use it until you log a newer weight.
                    </p>
                  ) : null}
                </>
              ) : (
                <div className={styles.empty}>
                  <h2>No weights logged yet</h2>
                  <p className="muted">Log your weight at the same time daily, ideally mornings after the toilet, so readings compare fairly.</p>
                </div>
              )}
              {/* Under-18s have no weight-cut feature: no target, no weigh-in for one. */}
              {!isMinorAthlete ? (
                targetKg == null || !weighIn ? (
                  <div className={styles.targetRow}>
                    <p>{targetKg == null ? "No target weight yet." : `Target ${formatWeight(targetKg)}. No weigh-in set yet.`}{weighIn ? ` Weigh-in ${weighIn}.` : ""}</p>
                    <button type="button" className="secondary-button" aria-haspopup="dialog" onClick={() => setTargetOpen(true)}>
                      {targetKg == null ? "Add target weight" : "Add weigh-in"}
                    </button>
                  </div>
                ) : (
                  <div className={styles.targetRow}>
                    <p className={progress.note}>Target {formatWeight(targetKg)} · Weigh-in {weighIn}</p>
                    <button type="button" className={progress.textButton} aria-haspopup="dialog" onClick={() => setTargetOpen(true)}>Change</button>
                  </div>
                )
              ) : null}
              <button type="button" className={`cta ${styles.logButton}`} onClick={() => setEditing({ mode: "new" })}>
                Log today&apos;s weight
              </button>
            </section>

            {warning ? <SafetyNote tone="warning">{warning}</SafetyNote> : null}

            {trend.length ? (
              <section className={`history-summary ${styles.card} ${styles.trendCard}`} aria-label="Weight trend">
                <div className={summary.head}>
                  <h2 className={styles.sectionTitle}>Weight trend</h2>
                  <div className={summary.toggle} role="group" aria-label="Trend range">
                    {RANGES.map(item => (
                      <button key={item.value} type="button" aria-pressed={range === item.value} onClick={() => { setRange(item.value); setSelectedKey(null); }}>
                        {item.label}
                      </button>
                    ))}
                  </div>
                </div>
                {selected ? (
                  <div className={styles.readout} aria-live="polite" aria-label="Selected weight">
                    <p><strong data-weight="">{formatWeight(selected.weight_kg)}</strong>
                      {selectedPrevious ? <span className={styles.change}>{formatWeightDelta(Math.round((selected.weight_kg - selectedPrevious.weight_kg) * 10) / 10)} vs previous</span> : null}</p>
                    <p className={styles.meta}>{[formatAppDate(selected.date), selected.time, selected.is_fasted == null ? null : formatFastedState(selected.is_fasted)].filter(Boolean).join(" · ")}</p>
                    <p className={styles.meta}>
                      {trend.length} {trend.length === 1 ? "entry" : "entries"}
                      {trendChange != null ? ` · ${trendChange === 0 ? "No change" : `${trendChange < 0 ? "Down" : "Up"} ${Math.abs(trendChange).toFixed(1)} kg`}` : ""}
                    </p>
                  </div>
                ) : null}
                <WeightTrendChart trend={trend} days={range === "All" ? null : RANGE_DAY_WINDOW[range]} targetKg={targetKg}
                  selectedIndex={selectedKey ? selectedIndex : trend.length - 1} inspecting={selectedKey != null}
                  onSelect={index => setSelectedKey(keyOf(trend[index]))} />
                {trend.length === 1 ? <p className={progress.note}>Log another weight to see how it changes.</p> : null}
                {selected ? (
                  <button type="button" className={`${progress.textButton} ${styles.edit}`} onClick={() => setEditing({ mode: "edit", sourceIndex: selected.sourceIndex })}>
                    Edit weight <span aria-hidden="true">→</span>
                  </button>
                ) : null}
                <details className={progress.about}>
                  <summary>About these numbers</summary>
                  <p>The chart shows each logged scale reading, joined in order; days without one are left as gaps, never filled in. Weight swings daily with water, food and training, so read the direction over weeks. Not an official weigh-in.</p>
                  <p>The 7-day average and weekly pace use one reading per day: your earliest fasted reading, or your earliest reading if none was fasted.</p>
                  <p>Weekly pace needs six days of entries over two weeks, including one in the last week.</p>
                </details>
              </section>
            ) : null}

            {newestFirst.length ? (
              <section className={styles.listSection} aria-label="Logged weights">
                <h2 className="history-section-title">Logged weights</h2>
                <ul className="history-list">
                  {listed.map(entry => {
                    const previous = previousOf(entry);
                    return (
                      <li key={keyOf(entry)} className={`history-row ${styles.row}`}>
                        <button type="button" className={styles.rowButton} aria-haspopup="dialog" onClick={() => setEditing({ mode: "edit", sourceIndex: entry.sourceIndex })}>
                          <span className={styles.rowMain}>
                            <span className="history-row-date">{formatAppDate(entry.date)}</span>
                            <span className="history-row-meta">
                              {[entry.time, entry.is_fasted == null ? null : formatFastedState(entry.is_fasted)].filter(Boolean).join(" · ") || "Weight"}
                            </span>
                            {entry.notes ? <span className={styles.rowNote}>{entry.notes}</span> : null}
                          </span>
                          <span className={styles.rowSide}>
                            <strong>{formatWeight(entry.weight_kg)}</strong>
                            {previous ? <span>{formatWeightDelta(Math.round((entry.weight_kg - previous.weight_kg) * 10) / 10)}</span> : null}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
                {newestFirst.length > LIST_PAGE ? (
                  <button type="button" className="secondary-button" onClick={() => setShowAll(value => !value)}>
                    {showAll ? "Show fewer" : `Show all ${newestFirst.length} entries`}
                  </button>
                ) : null}
              </section>
            ) : null}

            <SafetyNote tone="warning">{WEIGHT_CUT_SAFETY}</SafetyNote>
          </>
        )}

        {editing && workspace ? (
          <WeighInSheet
            key={editing.mode === "edit" ? `edit-${editing.sourceIndex}` : "new"}
            entry={editingEntry}
            lastWeight={latest?.weight_kg ?? null}
            onClose={() => setEditing(null)}
            onSave={async (entry) => {
              const next = editing.mode === "edit"
                ? entries.map((item, index) => (index === editing.sourceIndex ? entry : item))
                : [...entries, entry];
              await saveEntries(next);
              setSelectedKey(null);
              setEditing(null);
              showToast(editing.mode === "edit" ? "Weight updated." : "Weight saved.", { tone: "success" });
            }}
            onDelete={editing.mode === "edit" ? async () => {
              await saveEntries(entries.filter((_, index) => index !== editing.sourceIndex));
              setSelectedKey(null);
              setEditing(null);
              showToast("Weight deleted.", { tone: "success" });
            } : undefined}
          />
        ) : null}
        {targetOpen && workspace && shared ? (
          <TargetSheet
            existingTargetKg={targetKg}
            campWeightKg={campWeight}
            fightDate={shared.fight_date || null}
            weighInType={shared.weigh_in_type ?? null}
            weighInTime={shared.weigh_in_time ?? null}
            onClose={() => setTargetOpen(false)}
            onSave={async (values) => {
              await saveTarget(values);
              setTargetOpen(false);
              showToast(targetKg == null ? "Target weight saved." : "Target and weigh-in saved.", { tone: "success" });
            }}
          />
        ) : null}
      </section>
    </RequireAuth>
  );
}
