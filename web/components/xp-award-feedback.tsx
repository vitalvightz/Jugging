"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { useToast, useToastIdle } from "@/components/toast-provider";
import { useXp, type XpFeedbackEvent } from "@/components/xp-provider";

/** Let the action's own confirmation land before the reward follows it. */
const ROUTINE_DELAY_MS = 500;
/** Pause after the toasts clear so the level-up arrives on a quiet screen. */
const LEVEL_UP_SETTLE_MS = 400;
/** Never hold a level-up back longer than this, even if toasts keep coming. */
const LEVEL_UP_MAX_WAIT_MS = 3_500;
const LEVEL_UP_VISIBLE_MS = 7_000;
/** Matches xp-level-up-exit in xp-interface.css. */
const LEVEL_UP_EXIT_MS = 260;

type LevelUpEvent = Extract<XpFeedbackEvent, { kind: "level_up" }>;

export function XpAwardFeedback() {
  const { feedback, dismissFeedback } = useXp();
  const { showToast } = useToast();

  useEffect(() => {
    if (!feedback || feedback.kind !== "routine") return;
    showToast(`+${feedback.amount} XP: ${feedback.label}`, {
      tone: "success",
      durationMs: 4_500,
      delayMs: ROUTINE_DELAY_MS,
      merge: { suffix: `+${feedback.amount} XP`, keys: feedback.actions },
    });
    dismissFeedback();
  }, [dismissFeedback, feedback, showToast]);

  if (!feedback || feedback.kind !== "level_up") return null;

  return (
    <LevelUpFeedback
      key={`${feedback.level}:${feedback.awardIds.join(",")}`}
      feedback={feedback}
      onDone={dismissFeedback}
    />
  );
}

function LevelUpFeedback({ feedback, onDone }: { feedback: LevelUpEvent; onDone: () => void }) {
  const toastsIdle = useToastIdle();
  const [phase, setPhase] = useState<"waiting" | "shown" | "leaving">("waiting");
  const reveal = useCallback(() => setPhase((current) => (current === "waiting" ? "shown" : current)), []);
  const close = useCallback(() => setPhase("leaving"), []);

  useEffect(() => {
    const timer = window.setTimeout(reveal, LEVEL_UP_MAX_WAIT_MS);
    return () => window.clearTimeout(timer);
  }, [reveal]);

  useEffect(() => {
    if (phase !== "waiting" || !toastsIdle) return;
    const timer = window.setTimeout(reveal, LEVEL_UP_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [phase, reveal, toastsIdle]);

  useEffect(() => {
    if (phase === "shown") {
      const timer = window.setTimeout(close, LEVEL_UP_VISIBLE_MS);
      return () => window.clearTimeout(timer);
    }
    if (phase === "leaving") {
      const timer = window.setTimeout(onDone, LEVEL_UP_EXIT_MS);
      return () => window.clearTimeout(timer);
    }
  }, [close, onDone, phase]);

  if (phase === "waiting") return null;

  return (
    <section
      className={`xp-level-up-feedback${phase === "leaving" ? " is-leaving" : ""}`}
      role="dialog"
      aria-modal="false"
      aria-labelledby="xp-level-up-title"
      aria-describedby="xp-level-up-message"
    >
      <button
        type="button"
        className="xp-level-up-dismiss"
        onClick={close}
        aria-label="Dismiss level-up message"
      >
        ×
      </button>
      <p className="xp-level-up-kicker">LEVEL UP</p>
      <h2 id="xp-level-up-title">
        LEVEL {feedback.level} UNLOCKED: {feedback.title.toUpperCase()}
      </h2>
      <p id="xp-level-up-message">{feedback.message}</p>
      <Link href="/progress" onClick={onDone}>
        View progress
      </Link>
    </section>
  );
}
