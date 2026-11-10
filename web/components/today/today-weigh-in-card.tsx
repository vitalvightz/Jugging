"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { useToast } from "@/components/toast-provider";
import { WeighInSheet } from "@/components/weigh-in-sheet";
import { getNutritionCurrent, updateNutritionCurrent } from "@/lib/api";
import { formatWeight, getBodyweightEntriesWithIndex, getCampWeight } from "@/lib/nutrition-bodyweight";
import { localDateValue, withBodyweightLog } from "@/lib/nutrition-workspace";
import type { NutritionBodyweightLogEntry, NutritionWorkspaceState } from "@/lib/types";
import { type CutStatusView, cutProgress, daysLeftLabel, daysToFight, describeCutStatus, weighInDetails } from "@/lib/weight-cut-status";
import styles from "./today-weigh-in-card.module.css";

function StatusIcon({ tone }: { tone: CutStatusView["tone"] }) {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {tone === "green" ? <><circle cx="12" cy="12" r="9" /><path d="m8 12.5 2.7 2.7L16 9.8" /></>
        : tone === "amber" ? <><path d="M12 3.5 21 19.5H3z" /><path d="M12 10v4M12 17h.01" /></>
          : <><circle cx="12" cy="12" r="9" /><path d="M12 7.5v5.5M12 16.5h.01" /></>}
    </svg>
  );
}

function CalendarIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3.5" y="5" width="17" height="15.5" rx="2.5" /><path d="M3.5 9.5h17M8 3v4M16 3v4" />
    </svg>
  );
}

/**
 * Weight-to-target and a one-tap weight entry for a camp's final weeks. The parent
 * shows it only for an adult with health-data consent inside the window
 * (lib/weight-cut-status inFinalWeeks), so no other Today load reads nutrition.
 * The status is the API's, from the plan's own weight-cut severity score.
 */
export function TodayWeighInCard({ token, fightDate, trainingDay }: {
  token: string;
  fightDate: string;
  trainingDay: string;
}) {
  const { showToast } = useToast();
  const [workspace, setWorkspace] = useState<NutritionWorkspaceState | null>(null);
  const [failed, setFailed] = useState(false);
  const [editing, setEditing] = useState<{ sourceIndex: number | null } | null>(null);

  useEffect(() => {
    let active = true;
    getNutritionCurrent(token)
      .then(next => { if (active) setWorkspace(next); })
      .catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [token]);

  // A failed read leaves Today as it was; the tracker stays one tap away on Home.
  if (failed || !workspace) return null;

  const entries = workspace.nutrition_monitoring.daily_bodyweight_log;
  const today = localDateValue();
  const newestFirst = getBodyweightEntriesWithIndex(entries).filter(entry => entry.date <= today);
  const latest = newestFirst[0] ?? null;
  const todays = newestFirst.find(entry => entry.date === today || entry.date === trainingDay) ?? null;
  const shared = workspace.shared_camp_context;
  const derived = workspace.derived;
  const campWeight = getCampWeight(workspace);
  const target = shared.target_weight_kg ?? null;
  const toGo = derived.kg_to_target ?? (campWeight != null && target != null ? Math.max(0, Math.round((campWeight - target) * 10) / 10) : null);
  const status = describeCutStatus(derived);
  const days = daysToFight(fightDate, trainingDay);
  const weighIn = weighInDetails(shared.fight_date || fightDate, shared.weigh_in_type, shared.weigh_in_time);
  const progress = cutProgress({ trend: derived.bodyweight_trend, fightDate: shared.fight_date || fightDate, currentKg: campWeight, targetKg: target });

  async function save(next: NutritionBodyweightLogEntry[], message: string) {
    setWorkspace(await updateNutritionCurrent(token, withBodyweightLog(workspace!, next)));
    setEditing(null);
    showToast(message, { tone: "success" });
  }

  return (
    <section className={styles.card} aria-label="Weight cut">
      <div className={styles.head}>
        <p className={styles.kicker}>
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 3v18M7 21h10M4 7h16M5 7l-3 8h6L5 7Zm14 0-3 8h6l-3-8Z" /></svg>
          <span>Weight cut{days != null ? <span className={styles.countdown}> · {daysLeftLabel(days)}</span> : null}</span>
        </p>
        {status ? <span className={styles.status} data-tone={status.tone}><StatusIcon tone={status.tone} />{status.label}</span> : null}
      </div>

      {target != null && toGo != null ? (
        <>
          <p className={styles.main}><strong>{toGo.toFixed(1)}</strong> kg to go</p>
          <div className={styles.journey}>
            <div className={styles.ends}>
              <span>{progress ? `Starting ${formatWeight(progress.startKg)}` : campWeight != null ? `Current weight ${formatWeight(campWeight)}` : ""}</span>
              <span>Target {formatWeight(target)}</span>
            </div>
            {progress ? (
              <>
                <div className={styles.track} role="progressbar" aria-label="Progress to target"
                  aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress.fraction * 100)}>
                  <span style={{ width: `${progress.fraction * 100}%` }} />
                </div>
                <p className={styles.meta}>
                  {campWeight != null && Math.abs(campWeight - progress.startKg) >= 0.05
                    ? `Current weight ${formatWeight(campWeight)}`
                    : "Progress updates after each weight entry"}
                </p>
              </>
            ) : null}
          </div>
          {status && status.tone !== "green" ? <p className={styles.detail}>{status.detail}</p> : null}
        </>
      ) : null}

      {weighIn ? (
        <div className={styles.weighIn}>
          <span className={styles.when}><CalendarIcon />{weighIn.when ?? "Time not set"}</span>
          <span>{weighIn.label}</span>
        </div>
      ) : null}

      <div className={styles.actions}>
        <Link href="/nutrition" className={styles.link}>{target == null ? "Add target weight" : "Manage target weight"}</Link>
        {todays ? (
          <div className={styles.done}>
            <span>Logged today · <strong>{formatWeight(todays.weight_kg)}</strong></span>
            <button type="button" className={styles.edit} aria-haspopup="dialog" onClick={() => setEditing({ sourceIndex: todays.sourceIndex })}>Edit</button>
          </div>
        ) : (
          <button type="button" className={styles.log} aria-label="Log today's weight" aria-haspopup="dialog" onClick={() => setEditing({ sourceIndex: null })}>
            Log weight <span aria-hidden="true">→</span>
          </button>
        )}
      </div>

      {editing ? (
        <WeighInSheet
          key={editing.sourceIndex ?? "new"}
          entry={editing.sourceIndex != null ? entries[editing.sourceIndex] ?? null : null}
          lastWeight={latest?.weight_kg ?? null}
          onClose={() => setEditing(null)}
          onSave={entry => editing.sourceIndex != null
            ? save(entries.map((item, index) => (index === editing.sourceIndex ? entry : item)), "Weight updated.")
            : save([...entries, entry], "Weight saved.")}
          onDelete={editing.sourceIndex != null
            ? () => save(entries.filter((_, index) => index !== editing.sourceIndex), "Weight deleted.")
            : undefined}
        />
      ) : null}
    </section>
  );
}
