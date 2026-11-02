"use client";

import { useEffect, useState } from "react";

import { getTrainingFocus } from "@/lib/api";
import { TRAINING_FOCUS_CHANGED_EVENT } from "@/lib/training-focus";
import type { TrainingFocus } from "@/lib/types";

/** The athlete's last sparring focus; null until read, or when unavailable. */
export function useTrainingFocus(token: string, enabled: boolean): TrainingFocus | null {
  const [focus, setFocus] = useState<TrainingFocus | null>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const bump = () => setVersion((value) => value + 1);
    window.addEventListener(TRAINING_FOCUS_CHANGED_EVENT, bump);
    return () => window.removeEventListener(TRAINING_FOCUS_CHANGED_EVENT, bump);
  }, []);
  useEffect(() => {
    if (!enabled || !token) return;
    const controller = new AbortController();
    // Context only: a failed read simply shows no reminder.
    getTrainingFocus(token, controller.signal).then(setFocus).catch(() => undefined);
    return () => controller.abort();
  }, [token, enabled, version]);
  return enabled ? focus : null;
}
