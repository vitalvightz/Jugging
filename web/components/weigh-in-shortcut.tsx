"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { getNutritionCurrent } from "@/lib/api";
import type { NutritionDerivedState } from "@/lib/types";
import { describeCutStatus } from "@/lib/weight-cut-status";

/**
 * Home's purple shortcut to the weight tracker. In a camp's final weeks
 * (`showStatus`, decided by Home with the same gate as Today's card) it reads
 * the camp's weight-to-target and the API's status; otherwise it makes no read.
 */
export function WeighInShortcut({ token, showStatus }: { token: string | null; showStatus: boolean }) {
  const [derived, setDerived] = useState<NutritionDerivedState | null>(null);

  useEffect(() => {
    if (!showStatus || !token) return;
    let active = true;
    getNutritionCurrent(token)
      .then(workspace => { if (active) setDerived(workspace.derived); })
      .catch(() => undefined);
    return () => { active = false; };
  }, [showStatus, token]);

  const status = showStatus ? describeCutStatus(derived) : null;
  const toGo = showStatus ? derived?.kg_to_target ?? null : null;
  const label = !status
    ? "Weight tracker: log today's weight"
    : derived?.weight_cut_status === "on_target" || toGo == null
      ? `Weight · ${status.label}`
      : `Weight · ${toGo.toFixed(1)} kg to go · ${status.label}`;

  return <Link href="/nutrition" className="overview-risk-more weigh-in-surface">{label}</Link>;
}
