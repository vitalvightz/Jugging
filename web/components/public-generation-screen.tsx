"use client";

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";

import { describeGenerationFailure, GENERATION_FAILURE_ACTION_LABELS } from "@/lib/generation-failure";
import type { GenerationFailureKind } from "@/lib/generation-failure";
import type { GenerationUiPhase } from "@/lib/generation-controller";
import { buildTipDeck, LOADING_TIPS, readSeenTipIds, rememberSeenTip, selectLoadingTips, type LoadingTip } from "@/lib/loading-tips";
import { getPublicMilestoneIndex, getPublicProgress, PUBLIC_CAMP_MILESTONES } from "@/lib/public-generation-progress";
import type { PlanRequest, ProgressMilestone } from "@/lib/types";
import styles from "./public-generation-theme.module.css";

type Props = {
  phase: GenerationUiPhase;
  error?: string | null;
  failureKind?: GenerationFailureKind | null;
  milestones?: ProgressMilestone[];
  startedAtMs?: number | null;
  jobId?: string | null;
  readyToOpen?: boolean;
  intake?: PlanRequest | null;
  canRetry?: boolean;
  onRetry?: (() => void) | null;
  onReturnToWorkspace?: (() => void) | null;
  onRefineIntake?: (() => void) | null;
};

const PROGRESS_STORAGE_PREFIX = "unlxck:public-generation-progress:";
const TIP_ROTATION_MS = 7_000;
const subscribeToNothing = () => () => {};

function readSavedProgress(jobId: string | null): number {
  if (!jobId || typeof window === "undefined") return 0;
  const value = Number(window.localStorage.getItem(`${PROGRESS_STORAGE_PREFIX}${jobId}`));
  return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
}

export function PublicGenerationScreen({ phase, error = null, failureKind = null, milestones = [], startedAtMs = null, jobId = null, readyToOpen = false, intake = null, canRetry = false, onRetry = null, onReturnToWorkspace = null, onRefineIntake = null }: Props) {
  const [now, setNow] = useState(() => Date.now());
  const [progressFloor, setProgressFloor] = useState(() => readSavedProgress(jobId));
  const terminal = ["failed", "review_paused", "already_generated"].includes(phase);
  useEffect(() => {
    if (terminal || phase === "finalizing") return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [phase, terminal]);

  useEffect(() => {
    setProgressFloor(readSavedProgress(jobId));
  }, [jobId]);

  // Contextual tips come from the intake already on this page; no extra
  // requests. The rest of the bank follows so a long build never runs dry.
  const tipPool = useMemo(() => selectLoadingTips(intake), [intake]);
  // Keyed by content, so a refreshed but equivalent intake keeps the deck.
  const tipPoolKey = tipPool.map((tip) => tip.id).join("|");
  const [tips, setTips] = useState<{ poolKey: string; pool: LoadingTip[]; deck: LoadingTip[]; position: number }>(() => ({
    poolKey: tipPoolKey,
    pool: tipPool,
    deck: buildTipDeck(tipPool, { recentlySeen: readSeenTipIds() }),
    position: 0,
  }));
  if (tips.poolKey !== tipPoolKey) {
    setTips({ poolKey: tipPoolKey, pool: tipPool, deck: buildTipDeck(tipPool, { recentlySeen: readSeenTipIds() }), position: 0 });
  }
  // The deck is shuffled per client, so a server-rendered pass marks no tip
  // active; otherwise hydration would keep the server's pick alongside ours.
  const isClient = useSyncExternalStore(subscribeToNothing, () => true, () => false);
  const activeTip = isClient ? tips.deck[tips.position] ?? null : null;
  // Tips rotate only while the camp is still being built; a finished or
  // stopped build keeps whichever tip is showing.
  const isBuilding = !terminal && phase !== "finalizing" && !readyToOpen;
  const rotateTips = isBuilding && tips.deck.length > 1;
  useEffect(() => {
    if (!rotateTips) return;
    const timer = window.setInterval(() => {
      setTips((current) => {
        if (current.position + 1 < current.deck.length) {
          return { ...current, position: current.position + 1 };
        }
        const lastId = current.deck[current.position]?.id ?? null;
        return {
          ...current,
          deck: buildTipDeck(current.pool, { avoidFirstId: lastId, recentlySeen: readSeenTipIds() }),
          position: 0,
        };
      });
    }, TIP_ROTATION_MS);
    return () => window.clearInterval(timer);
  }, [rotateTips]);
  const activeTipId = activeTip?.id ?? null;
  useEffect(() => {
    if (activeTipId) rememberSeenTip(activeTipId);
  }, [activeTipId]);

  const activeIndex = getPublicMilestoneIndex(phase, milestones);
  const progress = getPublicProgress(phase, milestones, startedAtMs, now, progressFloor, readyToOpen);
  const failure = phase === "failed" ? describeGenerationFailure(failureKind, error) : null;
  const takingLonger = !terminal && phase !== "finalizing" && startedAtMs !== null && now - startedAtMs > 10 * 60_000;

  useEffect(() => {
    if (!jobId || progress <= progressFloor) return;
    setProgressFloor(progress);
    window.localStorage.setItem(`${PROGRESS_STORAGE_PREFIX}${jobId}`, String(progress));
  }, [jobId, progress, progressFloor]);

  if (failure) {
    return <section className={`public-build public-build-terminal ${styles.theme}`}><p className="public-build-kicker">Build stopped</p><h1>{failure.headline}</h1><p>{failure.detail}</p><div className="public-build-actions">{canRetry && onRetry ? <button className="cta" onClick={onRetry}>{GENERATION_FAILURE_ACTION_LABELS.retry}</button> : null}{onRefineIntake ? <button className="cta ghost" onClick={onRefineIntake}>Fix my intake</button> : null}{onReturnToWorkspace ? <button className="cta ghost" onClick={onReturnToWorkspace}>Return to workspace</button> : null}</div></section>;
  }

  const statusKey = takingLonger ? "longer" : activeIndex;

  return (
    <section className={`public-build ${styles.theme}`}>
      <div className="public-build-hero">
        <header>
          <p className="public-build-kicker">
            {isBuilding ? <span className="public-build-live" aria-hidden="true" /> : null}
            Fight camp build
          </p>
          <h1>YOUR CAMP IS TAKING SHAPE</h1>
          <p className="public-build-estimate">Usually 5–10 minutes</p>
        </header>
        <div className="public-build-mark" aria-hidden="true"><span className="public-build-ring public-build-ring-one"/><span className="public-build-ring public-build-ring-two"/><span className="public-build-core">U</span></div>
        {/* Keys replay the fade-in on each new stage; the live region itself stays mounted so the change is announced. */}
        <div className="public-build-status" aria-live="polite"><strong key={`title-${statusKey}`}>{takingLonger ? "Taking longer than usual" : PUBLIC_CAMP_MILESTONES[activeIndex].title}</strong><span key={`detail-${statusKey}`}>{takingLonger ? "Final checks are still in progress." : PUBLIC_CAMP_MILESTONES[activeIndex].detail}</span></div>
        <div className="public-build-bar" role="progressbar" aria-label="Camp build progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.floor(progress)}><span style={{ width: `${progress}%` }}/></div>
      </div>
      <div className="public-build-track">
        <ol className="public-build-milestones">
          {PUBLIC_CAMP_MILESTONES.map((item, index) => { const state = index < activeIndex || (readyToOpen && index <= activeIndex) ? "complete" : index === activeIndex ? "active" : "future"; return <li key={item.code} className={`public-build-milestone public-build-milestone-${state}`}><span>{state === "complete" ? "✓" : ""}</span><div><strong>{item.title}</strong><small>{item.detail}</small></div></li>; })}
        </ol>
        {!terminal ? (
          <aside className="public-build-tip" aria-label="Unlxck tip">
            <p className="public-build-tip-label">Unlxck tip</p>
            {/* Every tip in the bank shares one grid cell, so the box is always
                as tall as the longest tip and never jumps when they rotate. */}
            <div className="public-build-tip-stack">
              {LOADING_TIPS.map((tip) => (
                <p key={tip.id} className={`public-build-tip-text${tip.id === activeTip?.id ? " public-build-tip-text-active" : ""}`} aria-hidden={tip.id === activeTip?.id ? undefined : true}>
                  {tip.text}
                </p>
              ))}
            </div>
          </aside>
        ) : null}
      </div>
      <p className="public-build-reassurance"><strong>Safe to leave.</strong> Your build continues in the background.</p>
    </section>
  );
}
