"use client";

import { useEffect, useRef, useState, type PointerEvent } from "react";

/** Slide this far left before letting go to cancel a held recording. */
export const SLIDE_TO_CANCEL_PX = 90;

/**
 * Press and hold to record, release to send, slide left to cancel — the
 * gesture people know from messaging apps. `start` runs on press; `finish`
 * runs on release with `send` false when the finger slid away or the touch
 * was interrupted. Release is watched on the window, so it still counts once
 * the button under the finger has been swapped for the recording bar.
 */
export function useHoldToRecord(start: () => void, finish: (send: boolean) => void) {
  const [slide, setSlide] = useState(0);
  const [holding, setHolding] = useState(false);
  const release = useRef<(() => void) | null>(null);
  const latest = useRef({ start, finish });
  useEffect(() => { latest.current = { start, finish }; });
  useEffect(() => () => release.current?.(), []);

  function onPointerDown(event: PointerEvent<HTMLElement>) {
    if (event.button !== 0 || release.current) return;
    event.preventDefault();
    const startX = event.clientX;
    const move = (e: globalThis.PointerEvent) => setSlide(Math.max(0, startX - e.clientX));
    const end = (e: globalThis.PointerEvent) => {
      stop();
      latest.current.finish(e.type === "pointerup" && startX - e.clientX < SLIDE_TO_CANCEL_PX);
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      release.current = null;
      setHolding(false);
      setSlide(0);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    release.current = stop;
    setSlide(0);
    setHolding(true);
    latest.current.start();
  }

  return { onPointerDown, holding, slide, cancelling: slide >= SLIDE_TO_CANCEL_PX };
}
