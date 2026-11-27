"use client";

import { useEffect, useRef } from "react";
import type { DailyWatch } from "@/lib/api-schema.generated";
import styles from "./daily-watch-preview.module.css";

export function DailyWatchPreview({ watch, reveal }: { watch?: DailyWatch | null; reveal: boolean }) {
  const vibrated = useRef(false);
  useEffect(() => {
    if (!reveal || vibrated.current) return;
    vibrated.current = true;
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) navigator.vibrate?.(18);
  }, [reveal]);

  return (
    <section className={styles.watch} aria-labelledby="daily-watch-heading">
      <h2 id="daily-watch-heading">Today&apos;s Watch</h2>
      <div className={styles.study} data-reveal={reveal || undefined}>
        {watch ? (
          <div aria-live="polite">
            <h3>{watch.title}</h3>
            <p>{watch.insight}</p>
          </div>
        ) : (
          <>
            <div className={styles.preview} aria-hidden="true">
              <span />
              <span />
              <span />
            </div>
            <a href="#today-checkin" className={styles.unlock}>
              <span className={styles.lock} aria-hidden="true">
                <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="6" y="10" width="12" height="10" rx="2" />
                  <path d="M8.5 10V7a3.5 3.5 0 0 1 7 0v3M12 14v2" />
                </svg>
              </span>
              Check in to unlxck
            </a>
          </>
        )}
      </div>
    </section>
  );
}
