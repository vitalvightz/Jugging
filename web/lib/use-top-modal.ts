import { type RefObject, useLayoutEffect } from "react";

/**
 * Opens a <dialog> as a modal in the browser's top layer, so a full-screen
 * viewer sits above any sheet that is itself a modal dialog (session details,
 * the coach's fighter sheet) instead of underneath it. Focus returns to
 * whatever opened it.
 */
export function useTopModal(ref: RefObject<HTMLDialogElement | null>) {
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const opener = document.activeElement as HTMLElement | null;
    if (typeof node.showModal === "function" && !node.open) node.showModal();
    else node.setAttribute("open", "");
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = overflow;
      if (node.open && typeof node.close === "function") node.close();
      opener?.focus?.();
    };
  }, [ref]);
}
