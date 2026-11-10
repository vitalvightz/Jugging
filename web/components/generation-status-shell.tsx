"use client";

import { useAppSession } from "./auth-provider";
import { GenerationStatusProvider, useGenerationStatus } from "./generation-status-provider";
import { GlobalGenerationStatus, getGenerationStatusTarget } from "./global-generation-status";
import { MobileTabBar } from "./mobile-tab-bar";
import { useEffect, useRef, type ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { getActivePlan } from "@/lib/api";
import { clearSharedReads } from "@/lib/shared-reads";
import { offerProgrammeArrival } from "@/lib/programme-arrival";
import { releaseOrphanedOverlayScroll } from "@/lib/overlay-scroll-lock";

// Observe tracked completions independently of the generation page or destination.
export function ProgrammeCompletionReceipt() {
  const { me, session } = useAppSession();
  const token = session?.access_token;
  const { phase, planId, terminalStatus, requiresAdminResume } = useGenerationStatus();
  useEffect(() => {
    if (me?.profile.role !== "athlete" || !token || phase !== "completed" || !planId
      || terminalStatus !== "completed" || requiresAdminResume) return;
    let cancelled = false;
    void getActivePlan(token).then(active => {
      if (!cancelled && active?.plan_id === planId) {
        clearSharedReads();
        offerProgrammeArrival(planId);
      }
    }).catch(() => { /* Unknown activation must not offer an unrelated plan. */ });
    return () => { cancelled = true; };
  }, [me?.profile.role, token, phase, planId, terminalStatus, requiresAdminResume]);
  return null;
}

function ApprovedGenerationHandoff() {
  const { me } = useAppSession();
  const router = useRouter();
  const { phase, jobId, planId, terminalStatus, source, athleteId } = useGenerationStatus();
  const followedJob = useRef<string | null>(null);

  useEffect(() => {
    if (me?.profile.role !== "athlete" || source !== "admin_triage_resume" || !jobId) return;
    if (phase === "queued" || phase === "running" || phase === "finalizing") {
      followedJob.current = jobId;
      return;
    }
    const target = getGenerationStatusTarget(phase, planId, terminalStatus, source, athleteId);
    if (phase === "completed" && target && followedJob.current === jobId) {
      followedJob.current = null;
      router.replace(target);
    }
  }, [athleteId, jobId, me?.profile.role, phase, planId, router, source, terminalStatus]);

  return null;
}

// A scroll lock left behind by an overlay that is no longer on screen froze
// the whole app (no scrolling, a dead band under the tab bar on iOS). Check
// after every navigation and whenever the page comes back into view.
function OrphanedScrollLockGuard() {
  const pathname = usePathname();

  useEffect(() => {
    const timer = window.setTimeout(releaseOrphanedOverlayScroll, 400);
    return () => window.clearTimeout(timer);
  }, [pathname]);

  useEffect(() => {
    const check = () => {
      if (document.visibilityState === "visible") releaseOrphanedOverlayScroll();
    };
    window.addEventListener("pageshow", check);
    document.addEventListener("visibilitychange", check);
    return () => {
      window.removeEventListener("pageshow", check);
      document.removeEventListener("visibilitychange", check);
    };
  }, []);

  return null;
}

interface GenerationStatusShellProps {
  children: ReactNode;
}

export function GenerationStatusShell({ children }: GenerationStatusShellProps) {
  const { session } = useAppSession();
  const token = session?.access_token ?? null;

  return (
    <GenerationStatusProvider token={token}>
      <ProgrammeCompletionReceipt />
      <ApprovedGenerationHandoff />
      <OrphanedScrollLockGuard />
      {children}
      <GlobalGenerationStatus />
      <MobileTabBar />
    </GenerationStatusProvider>
  );
}
