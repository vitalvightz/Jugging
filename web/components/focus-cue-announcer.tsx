"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { getNoteFocusCue } from "@/lib/api";
import {
  CUE_POLL_DELAYS_MS, NOTE_FOCUS_CUE_WATCH_EVENT, noteFocusCueMessage, type NoteFocusCueWatch, type NoteFocusSourceKind,
} from "@/lib/focus-cue-watch";
import { announceTrainingFocusChanged } from "@/lib/training-focus";
import { useToast } from "./toast-provider";

const CUE_TOAST_MS = 9_000;

/**
 * App-wide: after a note is saved, poll for the cue the AI drew from it and
 * show it once. Lives beside the toasts so it survives the save sheet closing
 * or the athlete moving on. A note re-saved while a poll runs restarts it.
 */
export function FocusCueAnnouncer({ delaysMs = CUE_POLL_DELAYS_MS }: { delaysMs?: readonly number[] } = {}) {
  const { showToast } = useToast();
  const [card, setCard] = useState<{ focus: string; kind: NoteFocusSourceKind } | null>(null);
  const pathname = usePathname();

  useEffect(() => setCard(null), [pathname]);

  useEffect(() => {
    const running = new Map<string, () => void>();

    function watch({ token, kind, sourceId }: NoteFocusCueWatch) {
      const key = `${kind}:${sourceId}`;
      const path = window.location.pathname;
      running.get(key)?.();
      let stopped = false;
      let timer: ReturnType<typeof setTimeout> | undefined;
      const stop = () => { stopped = true; if (timer) clearTimeout(timer); running.delete(key); };
      running.set(key, stop);

      const poll = (attempt: number) => {
        if (stopped || attempt >= delaysMs.length) { stop(); return; }
        timer = setTimeout(async () => {
          try {
            const cue = await getNoteFocusCue(token, kind, sourceId);
            if (stopped) return;
            if (cue.status === "ready" && cue.focus) {
              stop();
              if (window.location.pathname === path) {
                setCard({ focus: cue.focus, kind });
              } else {
                showToast(noteFocusCueMessage(cue.focus), { tone: "info", durationMs: CUE_TOAST_MS });
              }
              announceTrainingFocusChanged();
              return;
            }
            if (cue.status === "none") { stop(); return; }
          } catch {
            // A failed read is not worth an error: the cue still shows on Today and in History.
          }
          poll(attempt + 1);
        }, delaysMs[attempt]);
      };
      poll(0);
    }

    const onWatch = (event: Event) => {
      const detail = (event as CustomEvent<NoteFocusCueWatch>).detail;
      if (detail) watch(detail);
    };
    window.addEventListener(NOTE_FOCUS_CUE_WATCH_EVENT, onWatch);
    return () => {
      window.removeEventListener(NOTE_FOCUS_CUE_WATCH_EVENT, onWatch);
      for (const stop of [...running.values()]) stop();
    };
  }, [showToast, delaysMs]);

  return card ? (
    <aside className="unlxck-coach-cue" role="region" aria-label="UNLXCK Coach feedback" aria-live="polite">
      <div className="unlxck-coach-cue-heading">
        <Image src="/icons/icon-192x192.png" width={40} height={40} alt="" className="unlxck-coach-cue-mark" />
        <div><strong>UNLXCK COACH</strong><span>Session feedback</span></div>
        <button type="button" className="unlxck-coach-cue-close" aria-label="Close UNLXCK Coach feedback"
          onClick={() => setCard(null)}>×</button>
      </div>
      <p>{noteFocusCueMessage(card.focus)}</p>
      <div className="unlxck-coach-cue-footer">
        <span>Saved to History</span>
        <Link href={`/history?tab=${card.kind === "sparring_log" ? "sparring" : "sessions"}`} onClick={() => setCard(null)}>View in History →</Link>
      </div>
      <button type="button" className="unlxck-coach-cue-dismiss" onClick={() => setCard(null)}>Got it</button>
    </aside>
  ) : null;
}
