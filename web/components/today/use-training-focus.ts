"use client";

import { useEffect, useState } from "react";

import { getTrainingFocus } from "@/lib/api";
import { TRAINING_FOCUS_CHANGED_EVENT } from "@/lib/training-focus";
import type { TrainingFocus } from "@/lib/types";

/**
 * The athlete's last sparring focus; null until read, when unavailable, or when
 * disabled. A value is only returned for the token it was read with, so a new
 * session never shows the previous athlete's focus while its own loads.
 */
export function useTrainingFocus(token: string, enabled: boolean): TrainingFocus | null {
  const [read, setRead] = useState<{ token: string; focus: TrainingFocus | null } | null>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const bump = () => setVersion((value) => value + 1);
    window.addEventListener(TRAINING_FOCUS_CHANGED_EVENT, bump);
    return () => window.removeEventListener(TRAINING_FOCUS_CHANGED_EVENT, bump);
  }, []);
  useEffect(() => {
    if (!enabled || !token) return;
    const controller = new AbortController();
    // Context only: a failed read shows no reminder rather than an old one.
    getTrainingFocus(token, controller.signal)
      .then((focus) => setRead({ token, focus }))
      .catch(() => { if (!controller.signal.aborted) setRead({ token, focus: null }); });
    return () => controller.abort();
  }, [token, enabled, version]);
  return enabled && read?.token === token ? read.focus : null;
}
