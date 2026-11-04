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
  if (me?.latest_plan?.plan_id) {
    return `/plans/${me.latest_plan.plan_id}`;
  }

  // Tracking athletes keep the same Home, filled from what they log.
  if (me?.profile.starting_path === "tracking_first") return "/";
  if (needsStartingPath(me)) return STARTING_PATH_HREF;

  return FIRST_PLAN_HREF;
}

/** Only internal plan links may survive a sign-in redirect. */
export function getSafePlanReturnHref(href: string | null): string | null {
  return href && /^\/plans\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(href) ? href : null;
}

export function getAuthenticatedLandingHref(me: MeResponse | null, next?: string | null): string {
  if (me?.profile.role === "admin") {
    return "/admin";
  }

  // Coach and gym_owner are not live yet. No account currently holds these
  // roles, but route defensively to the protected "Coming soon" pages so they
  // never land in the athlete intake/dashboard flow if one is ever assigned.
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
    return "/consent";
  }

  // The private trial briefing is no longer a gate between signup and the first
  // plan. It stays readable in Settings and at /private-trial.
  return getSafePlanReturnHref(next ?? null) ?? getAthleteWorkspaceHref(me);
}
