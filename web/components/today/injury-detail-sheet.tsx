"use client";

import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import { useSheetExit } from "@/lib/use-sheet-exit";

/** Injury details stay off the daily card; reuse the app's iOS scroll lock.
 * `variant="screen"` fills the viewport for a full screen (Today's session
 * preview). Sheets stack: only the topmost one answers Escape and Tab. */
export function InjuryDetailSheet({ title, children, onClose, busy = false, inactive = false, variant = "sheet", closeLabel = "Close", panelClassName, entrance = "rise" }: {
  title: string; children: ReactNode; onClose: () => void; busy?: boolean;
  /** A stacked editor temporarily owns focus and screen reader navigation. */
  inactive?: boolean;
  variant?: "sheet" | "screen"; closeLabel?: string;
  /** "push": this sheet replaces another (a menu choosing Edit), so it slides
   * in from the right over a backdrop that is already there, rather than
   * rising up as a fresh pop-up. */
  entrance?: "rise" | "push";
  panelClassName?: string;
}) {
  const titleId = useId();
  const panel = useRef<HTMLDivElement>(null);
  // A sheet slides away before the parent removes it. The full screen is
  // closed by its caller through the fade to black instead.
  const { closing, close: exitSheet } = useSheetExit(onClose);
  const dismiss = variant === "screen" ? onClose : exitSheet;
  const state = useRef({ busy, onClose: dismiss });
  useEffect(() => { state.current = { busy, onClose: dismiss }; }, [busy, dismiss]);
  useEffect(() => {
    const currentPanel = panel.current;
    const previous = document.activeElement as HTMLElement | null;
    const release = lockOverlayScroll();
    // iOS's keyboard changes the visual viewport without resizing the layout
    // viewport. The injury editor uses these bounds to keep its header in view.
    const fit = () => {
      const viewport = window.visualViewport;
      const backdrop = panel.current?.parentElement;
      backdrop?.style.setProperty("--sheet-height", `${viewport?.height ?? window.innerHeight}px`);
      backdrop?.style.setProperty("--sheet-top", `${viewport?.offsetTop ?? 0}px`);
    };
    fit();
    window.visualViewport?.addEventListener("resize", fit);
    window.visualViewport?.addEventListener("scroll", fit);
    window.addEventListener("resize", fit);
    panel.current?.focus({ preventScroll: true });
    function keydown(event: KeyboardEvent) {
      const dialogs = document.querySelectorAll('[role="dialog"][aria-modal="true"]');
      if (dialogs[dialogs.length - 1] !== panel.current) return;
      if (event.key === "Escape") {
        event.preventDefault();
        if (!state.current.busy) state.current.onClose();
      }
      if (event.key !== "Tab") return;
      const controls = Array.from(panel.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), select:not(:disabled), input:not(:disabled), textarea:not(:disabled), summary, a[href], [tabindex="0"]',
      ) ?? []).filter(control => {
        if (control.closest("[hidden], [inert]")) return false;
        const details = control.closest("details");
        return !details || details.open || control.tagName === "SUMMARY";
      });
      const first = controls[0], last = controls.at(-1);
      if (!first) { event.preventDefault(); panel.current?.focus(); }
      else if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.current)) {
        event.preventDefault(); first.focus();
      }
    }
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      window.visualViewport?.removeEventListener("resize", fit);
      window.visualViewport?.removeEventListener("scroll", fit);
      window.removeEventListener("resize", fit);
      release();
      // Fixing the last issue can remove its trigger while an editor is open.
      const target = previous?.isConnected ? previous : Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"]')).filter(dialog => dialog !== currentPanel && !dialog.closest('[inert]')).at(-1);
      target?.focus({ preventScroll: true });
    };
  }, []);
  if (typeof document === "undefined") return null;
  return createPortal(<div inert={inactive} aria-hidden={inactive || undefined} className="injury-sheet-backdrop" data-variant={variant} data-entrance={entrance} data-closing={closing || undefined} onClick={() => { if (!busy) dismiss(); }}>
    <div className={`injury-sheet${panelClassName ? ` ${panelClassName}` : ""}`} data-variant={variant} data-entrance={entrance} ref={panel} role="dialog" aria-modal="true" aria-labelledby={titleId}
      aria-busy={busy} tabIndex={-1} onClick={event => event.stopPropagation()}>
      <div className="injury-sheet-header"><h2 id={titleId}>{title}</h2>
        <button type="button" className="injury-sheet-close" aria-label={closeLabel} disabled={busy} onClick={() => dismiss()}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d={variant === "screen" ? "m15 5-7 7 7 7" : "m6 6 12 12M6 18 18 6"} /></svg>
        </button>
      </div>
      {children}
    </div>
  </div>, document.body);
}

export function InjuryChevron() {
  return <svg className="injury-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>;
}
