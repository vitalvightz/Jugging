"use client";

import { usePathname } from "next/navigation";
import { useLayoutEffect, useRef, type ReactNode } from "react";

import { prefersReducedMotion } from "@/lib/reduced-motion";

/**
 * The page area. Switching pages fades the new one in quickly rather than
 * popping it in a frame; the first page of a visit just appears.
 * Opacity only, so fixed sheets and timers inside are never repositioned.
 */
export function PageTransition({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const pageRef = useRef<HTMLDivElement>(null);
  const shownPath = useRef(pathname);

  // Before paint: after it, the new page would show at full strength for a
  // frame first, the very flicker this removes.
  useLayoutEffect(() => {
    if (shownPath.current === pathname) return;
    shownPath.current = pathname;
    // A fade through black already covers this change; a second opacity
    // animation exposes the page beneath fixed overlays and makes text shimmer.
    if (document.querySelector(".fade-through")) return;
    const page = pageRef.current;
    if (!page || typeof page.animate !== "function" || prefersReducedMotion()) return;
    page.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 260, easing: "ease-out" });
  }, [pathname]);

  return <div ref={pageRef} className="page">{children}</div>;
}
