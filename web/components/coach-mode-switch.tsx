"use client";

import type { CoachMode } from "@/lib/coach-mode";
import styles from "./coach-mode-switch.module.css";

export function CoachModeSwitch({ mode, onChange }: { mode: CoachMode; onChange: (mode: CoachMode) => void }) {
  return <div className={styles.group} role="group" aria-label="Coaching focus">
    {([["technical", "Technical"], ["sc", "S&C"]] as const).map(([value, label]) =>
      <button key={value} type="button" aria-pressed={mode === value}
        onClick={() => onChange(value)}>{label}</button>)}
  </div>;
}
