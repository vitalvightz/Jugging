/**
 * Opt-in speed diagnostics for real devices (temporary; see
 * docs/performance/navigation-2026-11-05.md). Off unless switched on with
 * `?perf=1` or the Settings toggle; `?perf=0` or the toggle turns it off and
 * deletes what it recorded. Nothing is sent anywhere: the athlete copies or
 * shares the report themselves.
 *
 * What a report may contain: timings, paths with every id and
 * query value removed, status codes, sizes, server-timing names/durations, the
 * device class and connection type. Never: tokens or other headers, response bodies,
 * screen text, query values, ids or anything typed.
 */

export const PERF_FLAG_KEY = "unlxck-perf-diagnostics";
export const PERF_LOG_KEY = "unlxck-perf-diagnostics-log";
export const PERF_TOGGLE_EVENT = "unlxck:perf-diagnostics";
const MAX_NAVIGATIONS = 40;
const MAX_REQUESTS_PER_NAVIGATION = 40;

export type PerfRequest = {
  kind: "api" | "route";
  path: string;
  /** ms from the navigation's tap. */
  start: number;
  ttfb: number | null;
  /** -1 while the body was still arriving when the navigation was recorded. */
  end: number;
  bytes: number | null;
  status: number | null;
  server: Array<{ name: string; ms: number }>;
  /** The API's random per-request id, to find this request in server logs. */
  requestId?: string | null;
  /**
   * Browser-measured phases (PerformanceResourceTiming), ms. `setup` is queueing,
   * DNS, TCP and TLS before the request was sent; `wait` is request sent to first
   * byte; `download` is first to last byte. `wait` minus the server's `queue` and
   * `app` is the network and proxy hops between the phone and the API.
   */
  phases?: { setup: number; wait: number; download: number } | null;
  /**
   * Route payload requests only: Next marked it a background prefetch. Some
   * of Next's full prefetches carry no marker, so `false` does not by itself
   * mean the navigation waited on it; see `blockedRoute`.
   */
  prefetch?: boolean;
  /**
   * Route payload requests only: it started before the screen changed and its
   * response arrived before the screen changed, so the navigation plausibly
   * waited on it. False when the screen changed first (a background request).
   * Null when the screen never changed or the response never arrived.
   */
  blockedRoute?: boolean | null;
};

export type PerfNavigation = {
  from: string;
  to: string;
  /** Wall clock of the tap, for matching server logs (UTC ISO). */
  at: string;
  /** ms from tap. null when it never happened in the window. */
  routeChanged: number | null;
  firstFrame: number | null;
  firstContent: number | null;
  placeholdersGone: number | null;
  /** Placeholder count seen right after the route changed. */
  placeholdersAtStart: number;
  /** Total ms of frames longer than 50 ms between the tap and settling. */
  blockedMs: number;
  longestFrameMs: number;
  requests: PerfRequest[];
  timedOut: boolean;
  /** The next tap came before this screen settled. */
  interrupted?: boolean;
  /** Where the tapped link was: the tab bar / side nav, or a link on the page. */
  via?: "nav" | "page";
  /** The tapped link with query keys (never values), e.g. `/today?preview=`. */
  href?: string;
};

export type PerfReport = {
  version: 1;
  createdAt: string;
  build: string;
  device: {
    userAgent: string;
    standalone: boolean;
    viewport: string;
    pixelRatio: number;
    cpuCores: number | null;
    memoryGb: number | null;
    connection: string | null;
  };
  appStart: Record<string, number | null>;
  navigations: PerfNavigation[];
  summary: Array<{ route: string; visits: number; medianContentMs: number | null; medianSettledMs: number | null; medianRouteMs: number | null; medianApiMs: number | null }>;
};

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;

/** A path with ids, numbers and query values removed: `/api/plans/:id?limit=`. */
export function sanitizePath(raw: string, origin = "https://x.invalid"): string {
  let url: URL;
  try {
    url = new URL(raw, origin);
  } catch {
    return "(unparseable)";
  }
  const path = url.pathname
    .replace(UUID, ":id")
    .split("/")
    .map(part => (/\d/.test(part) && part !== ":id" ? ":id" : part))
    .join("/");
  const keys = [...new Set([...url.searchParams.keys()].filter(key => key !== "_rsc"))].sort();
  return keys.length ? `${path}?${keys.map(key => `${key}=`).join("&")}` : path;
}

/** Whether a fetch's own headers mark it as a Next router prefetch. */
export function isRouterPrefetch(headers: HeadersInit | undefined): boolean {
  if (!headers) return false;
  try {
    return new Headers(headers).has("next-router-prefetch");
  } catch {
    return false;
  }
}

const REQUEST_ID = /^[a-z0-9-]{1,36}$/i;

/** The API's X-Request-ID when it looks like one, else null. */
export function sanitizeRequestId(value: string | null): string | null {
  return value && REQUEST_ID.test(value) ? value : null;
}

type ResourcePhaseSource = Pick<PerformanceResourceTiming, "startTime" | "requestStart" | "responseStart" | "responseEnd">;

/** Setup/wait/download from a resource entry; null when the browser withheld detail. */
export function resourcePhases(entry: ResourcePhaseSource | undefined): PerfRequest["phases"] {
  if (!entry || !(entry.requestStart > 0) || !(entry.responseStart >= entry.requestStart)) return null;
  const ms = (value: number) => Math.max(0, Math.round(value));
  return {
    setup: ms(entry.requestStart - entry.startTime),
    wait: ms(entry.responseStart - entry.requestStart),
    download: ms(entry.responseEnd - entry.responseStart),
  };
}

export function median(values: Array<number | null>): number | null {
  const present = values.filter((value): value is number => typeof value === "number" && Number.isFinite(value)).sort((a, b) => a - b);
  if (!present.length) return null;
  return Math.round(present[Math.floor(present.length / 2)]);
}

export function summarize(navigations: PerfNavigation[]): PerfReport["summary"] {
  const byRoute = new Map<string, PerfNavigation[]>();
  for (const navigation of navigations) byRoute.set(navigation.to, [...(byRoute.get(navigation.to) ?? []), navigation]);
  return [...byRoute.entries()].map(([route, visits]) => ({
    route,
    visits: visits.length,
    medianRouteMs: median(visits.map(visit => visit.routeChanged)),
    medianContentMs: median(visits.map(visit => visit.firstContent)),
    medianSettledMs: median(visits.map(visit => visit.placeholdersGone)),
    medianApiMs: median(visits.map(visit => {
      const api = visit.requests.filter(request => request.kind === "api");
      return api.length ? Math.max(...api.map(request => request.end)) : null;
    })),
  }));
}

/** Whether a route request plausibly held up the screen change (see `blockedRoute`). */
export function routeRequestBlocked(request: Pick<PerfRequest, "start" | "ttfb">, routeChanged: number | null): boolean | null {
  if (routeChanged === null || request.ttfb === null) return null;
  return request.start <= routeChanged && request.ttfb <= routeChanged;
}

/** Marks each route request with whether the navigation waited on it. */
export function withBlockedRoutes(navigation: PerfNavigation): PerfNavigation {
  return {
    ...navigation,
    requests: navigation.requests.map(request => (request.kind === "route"
      ? { ...request, blockedRoute: routeRequestBlocked(request, navigation.routeChanged) }
      : request)),
  };
}

export function appendNavigation(log: PerfNavigation[], navigation: PerfNavigation): PerfNavigation[] {
  const bounded = { ...navigation, requests: navigation.requests.slice(0, MAX_REQUESTS_PER_NAVIGATION) };
  return [...log, bounded].slice(-MAX_NAVIGATIONS);
}

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function isPerfDiagnosticsEnabled(): boolean {
  return storage()?.getItem(PERF_FLAG_KEY) === "1";
}

export function setPerfDiagnosticsEnabled(enabled: boolean): void {
  const store = storage();
  try {
    if (enabled) store?.setItem(PERF_FLAG_KEY, "1");
    else {
      store?.removeItem(PERF_FLAG_KEY);
      store?.removeItem(PERF_LOG_KEY);
    }
  } catch {
    // Storage unavailable: diagnostics simply stay off.
  }
  if (typeof window !== "undefined") window.dispatchEvent(new Event(PERF_TOGGLE_EVENT));
}

export function readPerfLog(): PerfNavigation[] {
  try {
    const parsed = JSON.parse(storage()?.getItem(PERF_LOG_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function writePerfLog(log: PerfNavigation[]): void {
  try {
    storage()?.setItem(PERF_LOG_KEY, JSON.stringify(log));
  } catch {
    // A full or blocked storage only loses the log, never the app.
  }
}

/** Applies `?perf=1` / `?perf=0` from the current URL. */
export function applyPerfQueryFlag(search: string): void {
  const value = new URLSearchParams(search).get("perf");
  if (value === "1") setPerfDiagnosticsEnabled(true);
  else if (value === "0") setPerfDiagnosticsEnabled(false);
}
