"use client";

import { useEffect, useRef, useState } from "react";
import type { DailyWatch } from "@/lib/api-schema.generated";
import { useFadeThrough } from "@/components/fade-through";
import { InjuryDetailSheet } from "./injury-detail-sheet";
import styles from "./daily-watch-preview.module.css";

export function DailyWatchPreview({ watch, locked, reveal, onPreviewSession }: {
  watch: DailyWatch; locked: boolean; reveal: boolean; onPreviewSession?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const fadeThrough = useFadeThrough();
  const vibrated = useRef(false);
  useEffect(() => {
    if (!reveal || vibrated.current) return;
    vibrated.current = true;
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) navigator.vibrate?.(18);
  }, [reveal]);

  return (
    <section className={styles.watch} aria-labelledby="daily-watch-heading">
      <h2 id="daily-watch-heading">Today&apos;s Watch</h2>
      {!locked && (
        <button type="button" className={styles.open} aria-label="Open today's Watch"
          onClick={() => fadeThrough(() => setOpen(true))}>
          <span aria-hidden="true">→</span>
        </button>
      )}
      <div className={styles.study} data-locked={locked || undefined} data-reveal={reveal || undefined}>
        <div className={styles.content} aria-hidden={locked || undefined}>
          <h3>{watch.title}</h3>
          <p>{watch.insight}</p>
        </div>
        <div className={styles.cover} aria-hidden={!locked || undefined}>
          <a href={locked ? "#today-checkin" : undefined} className={styles.unlock}>
            <span className={styles.mark} data-watch-mark aria-hidden="true" />
            Check in to unlxck
          </a>
        </div>
      </div>
      {open && !locked && (
        <InjuryDetailSheet title="Today's Watch" variant="screen" closeLabel="Back to Today"
          panelClassName={styles.screen} onClose={() => fadeThrough(() => setOpen(false))}>
          <div className={`today-preview-screen ${styles.briefing}`}>
            <header>
              <p className="today-preview-meta">Film study</p>
              <h1>{watch.title}</h1>
              <p className={styles.insight}>{watch.insight}</p>
            </header>
            {watch.objective && (
              <section><h2>Your mission</h2><p className={styles.mission}>{watch.objective}</p></section>
            )}
            {Boolean(watch.instructions?.length) && (
              <section><h2>Tactical cues</h2><ol>
                {watch.instructions.map((cue, index) => <li key={index}>{cue}</li>)}
              </ol></section>
            )}
            <button type="button" className="cta" onClick={() => fadeThrough(() => {
              setOpen(false);
              onPreviewSession?.();
            })}>{onPreviewSession ? "Preview today’s session" : "Back to Today"}</button>
          </div>
        </InjuryDetailSheet>
      )}
    </section>
  );
}
