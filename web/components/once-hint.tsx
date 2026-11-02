"use client";

import { useState, useSyncExternalStore } from "react";

import { readStoredFlag, STORED_FLAG_EVENT, writeStoredFlag } from "@/lib/history-hint";

function subscribe(onChange: () => void): () => void {
  window.addEventListener("storage", onChange);
  window.addEventListener(STORED_FLAG_EVENT, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(STORED_FLAG_EVENT, onChange);
  };
}

/** A localStorage value that every mounted reader sees change. "server"
 * during server rendering, "blocked" when storage is unavailable, null when unset. */
export function useStoredFlag(key: string): string | null {
  return useSyncExternalStore(subscribe, () => readStoredFlag(key), () => "server");
}

/** Whether a one-time hint keyed by `key` should still show, and how to retire it.
 * Hidden on the server and when storage is blocked, so it never flashes for
 * returning users. */
export function useSeenOnce(key: string): [boolean, () => void] {
  const [dismissed, setDismissed] = useState(false);
  const unseen = useStoredFlag(key) === null;
  const dismiss = () => {
    setDismissed(true);
    writeStoredFlag(key, "1");
  };
  return [unseen && !dismissed, dismiss];
}

/** The dark one-time coach-mark pill: the whole pill dismisses it. */
export function OnceHint({ className = "once-hint", label, onDismiss }: { className?: string; label: string; onDismiss: () => void }) {
  return (
    <button type="button" className={className} onClick={onDismiss}>
      {label}
      <span aria-hidden="true">×</span>
    </button>
  );
}
