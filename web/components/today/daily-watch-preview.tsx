"use client";

import { useEffect, useRef } from "react";
import type { DailyWatch } from "@/lib/api-schema.generated";
import styles from "./daily-watch-preview.module.css";

export function DailyWatchPreview({ watch, locked, reveal }: { watch: DailyWatch; locked: boolean; reveal: boolean }) {
  const vibrated = useRef(false);
  useEffect(() => {
    if (!reveal || vibrated.current) return;
    vibrated.current = true;
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) navigator.vibrate?.(18);
  }, [reveal]);

  return (
    <section className={styles.watch} aria-labelledby="daily-watch-heading">
      <h2 id="daily-watch-heading">Today&apos;s Watch</h2>
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
    </section>
  );
}
