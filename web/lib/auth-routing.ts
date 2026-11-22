import { requiresComplianceAcceptance } from "@/lib/compliance";
import type { MeResponse } from "@/lib/types";

/**
 * The default route to a first plan: Quick Build asks only what changes the
 * plan or the safety call (sport, fight date, schedule, goals, injuries).
 * Advanced Intake stays available for athletes who want more control, but it is
 * never the default path for someone who has not trained with UNLXCK yet.
 *
 * The plan-building destination remains available from either starting path.
 */
export const FIRST_PLAN_HREF = "/quick-build";
export const STARTING_PATH_HREF = "/get-started";

export function needsStartingPath(me: MeResponse | null): boolean {
  return Boolean(me?.profile.role === "athlete" && me.profile.starting_path === null
    && !me.latest_plan?.plan_id && me.plan_count === 0 && !me.latest_intake
    && !Object.keys(me.profile.onboarding_draft ?? {}).length);
}

/**
 * Where an athlete belongs once signup is complete: their latest plan if they
 * have one, otherwise their saved starting experience.
 */
export function getAthleteWorkspaceHref(me: MeResponse | null): string {
  // An athlete with a plan lands on Today: their next action, not the
  // programme. The full plan stays one tap away from there.
  if (me?.latest_plan?.plan_id) {
    return "/today";
  }

  // Tracking athletes keep the same Home, filled from what they log.
  if (me?.profile.starting_path === "tracking_first") return "/";
  if (needsStartingPath(me)) return STARTING_PATH_HREF;

  return FIRST_PLAN_HREF;
}

/** Only known internal deep links may survive a sign-in redirect. */
export function getSafeAuthReturnHref(href: string | null): string | null {
  if (href === "/settings/coach-access") return href;
  return href && /^\/plans\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(href) ? href : null;
}

export function getAuthenticatedLandingHref(me: MeResponse | null, next?: string | null): string {
  if (me?.profile.role === "admin") {
    return "/admin";
  }

  // Role-specific workspaces keep coaches and gym owners out of athlete intake.
  if (me?.profile.role === "coach") {
    return "/coach";
  }

  if (me?.profile.role === "gym_owner") {
    return "/gym-owner";
  }

  // Age and Terms come first: an athlete who has not accepted the Terms has not
  // completed signup, so they cannot be sent into intake. Only reached by
  // accounts that predate the consent fields — a signup through the current
  // form already satisfies this.
  if (requiresComplianceAcceptance(me)) {
    const query = new URLSearchParams();
    if (next === "/settings/coach-access") query.set("next", next);
    return query.size ? `/consent?${query}` : "/consent";
  }

  // The private trial briefing is no longer a gate between signup and the first
  // plan. It stays readable in Settings and at /private-trial.
  return getSafeAuthReturnHref(next ?? null) ?? getAthleteWorkspaceHref(me);
}
