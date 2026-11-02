"use client";

import { useTranslations } from "next-intl";
import { useEffect } from "react";

import { useAppSession } from "@/components/auth-provider";
import { OnceHint, useStoredFlag } from "@/components/once-hint";
import { armHistoryHint, historyHintKey, markHistoryHintSeen, STORED_FLAG_EVENT } from "@/lib/history-hint";

/** "See all your work in History", shown once per athlete after their first saved session. */
export function HistoryHint({ className }: { className: string }) {
  const t = useTranslations("Navigation");
  const athleteId = useAppSession().me?.profile.athlete_id ?? "";
  useEffect(() => {
    if (!athleteId) return;
    const arm = () => armHistoryHint(athleteId);
    arm();
    window.addEventListener(STORED_FLAG_EVENT, arm);
    return () => window.removeEventListener(STORED_FLAG_EVENT, arm);
  }, [athleteId]);
  const armed = useStoredFlag(athleteId ? historyHintKey(athleteId) : "") === "armed";
  return athleteId && armed
    ? <OnceHint className={className} label={t("historyHint")} onDismiss={() => markHistoryHintSeen(athleteId)} />
    : null;
}
