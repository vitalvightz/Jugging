import { expect, type Page } from "@playwright/test";
import fixtures from "./fixtures/rehab-today.json";
import { isolateFromNetwork } from "./support";

/**
 * Synthetic authenticated athlete for browser request-count journeys. Every
 * /api response is served here, so counts are actual browser network calls,
 * not API-helper invocations. No real backend or Supabase is reached.
 */
export type ApiCall = { method: string; path: string; status: number; bytes: number; at: number };

const { view: sourceView, profile: sourceProfile } = fixtures.chest;
const storageKey = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;

export type Persona = { token: string; athleteId: string; planId: string; name: string };
export const PRIMARY: Persona = { token: "athlete-a-token", athleteId: sourceProfile.profile.athlete_id, planId: sourceView.active_plan.id, name: "Athlete A" };
export const SECONDARY: Persona = { token: "athlete-b-token", athleteId: "7a1c2b3d-0000-4000-8000-00000000000b", planId: "c31d500b-6e91-457d-b583-7eaf5792ffef", name: "Athlete B" };

function profileFor(persona: Persona) {
  return { ...sourceProfile, profile: { ...sourceProfile.profile, athlete_id: persona.athleteId, full_name: persona.name, private_trial_ack_at: "2026-10-01T12:00:00Z" },
    plan_count: 1, latest_plan: { ...sourceProfile.latest_plan, plan_id: persona.planId } };
}

export function todayFor(persona: Persona, planName = `${persona.name} camp`) {
  const view = structuredClone(sourceView);
  view.active_plan = { ...view.active_plan, id: persona.planId, name: planName } as typeof view.active_plan;
  view.live_prescription.plan_id = persona.planId;
  return view;
}

export function planFor(persona: Persona, planName = `${persona.name} camp`) {
  const session = sourceView.live_prescription.session;
  return {
    plan_id: persona.planId, athlete_id: persona.athleteId, plan_name: planName, status: "ready", activation_state: "eligible",
    created_at: "2026-10-01T12:00:00Z", fight_date: null, custom_plan_id: null,
    schedule_context: { current_training_day: sourceView.today.training_day },
    outputs: {
      plan_text: `${planName}\nWeek 1\nMonday: ${session.title}`,
      structured_plan: { weeks: [{ week_index: 1, phase_label: "GPP", start_date: "2026-10-05", end_date: "2026-10-11",
        days: [{ date: sourceView.today.training_day, weekday: "Monday", sessions: [session] }] }] },
    },
  };
}

function summaryFor(persona: Persona, planName = `${persona.name} camp`) {
  // full_name and technical_style complete the PlanSummary contract.
  return { plan_id: persona.planId, athlete_id: persona.athleteId, plan_name: planName, status: "ready", activation_state: "eligible",
    created_at: "2026-10-01T12:00:00Z", fight_date: null, custom_plan_id: null, full_name: persona.name, technical_style: [] };
}

const historyRows = Array.from({ length: 12 }, (_, index) => ({
  id: `manual-${index}`, source: "external", title: "Boxing", activity: "boxing", duration_minutes: 45, effort: "moderate", status: "done",
  training_day: new Date(Date.parse("2026-10-05T12:00:00Z") - index * 86_400_000).toISOString().slice(0, 10),
}));

export type MockOptions = {
  latencyMs?: number; persona?: Persona; planName?: string;
  /** Per-request server time, overriding latencyMs (benchmarks). */
  latencyFor?: (method: string, path: string) => number | undefined;
};

/** Signs a persona in and serves the app's API from fixtures with fixed latency. */
export async function mockAthlete(page: Page, baseURL: string, options: MockOptions = {}) {
  const latencyMs = options.latencyMs ?? 0;
  let persona = options.persona ?? PRIMARY;
  let planName = options.planName;
  const calls: ApiCall[] = [];
  const pending = new Set<string>();
  let failNext: string | null = null;
  await isolateFromNetwork(page, baseURL);
  await page.route("https://fonts.googleapis.com/**", route => route.fulfill({ contentType: "text/css", body: "" }));
  await page.clock.setFixedTime(new Date(`${sourceView.today.training_day}T12:05:00Z`));
  await page.addInitScript(([key, value]) => { if (!localStorage.getItem(key)) localStorage.setItem(key, value); }, [storageKey, sessionFor(persona)]);
  await page.route("**/api/**", async route => {
    const request = route.request(); const url = new URL(request.url());
    const path = `${url.pathname}${url.search}`; const method = request.method();
    // Whose data is served follows the bearer token, as the backend does.
    const bearer = (await request.allHeaders()).authorization?.replace(/^Bearer /, "");
    const who = bearer === SECONDARY.token ? SECONDARY : bearer === PRIMARY.token ? PRIMARY : null;
    const id = `${method} ${path} ${calls.length}`;
    pending.add(id);
    try {
      const delayMs = options.latencyFor?.(method, url.pathname) ?? latencyMs;
      if (delayMs) await new Promise(resolve => setTimeout(resolve, delayMs));
      const fulfil = async (body: unknown, status = 200) => {
        const text = JSON.stringify(body);
        calls.push({ method, path, status, bytes: Buffer.byteLength(text), at: Date.now() });
        await route.fulfill({ status, contentType: "application/json", body: text });
      };
      if (!who) return await fulfil({ detail: "unauthenticated" }, 401);
      if (failNext && path.startsWith(failNext)) { failNext = null; return await fulfil({ detail: "Gateway timeout" }, 504); }
      const name = who === persona ? planName : undefined;
      const p = url.pathname;
      if (p === "/api/me") return await fulfil(profileFor(who));
      if (p === "/api/today") return await fulfil(todayFor(who, name));
      if (p === "/api/plans") return await fulfil([summaryFor(who, name)]);
      if (p === "/api/plans/active") return await fulfil(summaryFor(who, name));
      if (p === `/api/plans/${who.planId}` && method === "PATCH") {
        if (who === persona) planName = request.postDataJSON().plan_name;
        return await fulfil(planFor(who, request.postDataJSON().plan_name));
      }
      if (p === `/api/plans/${who.planId}`) return await fulfil(planFor(who, name));
      if (p === `/api/plans/${who.planId}/calendar`) return await fulfil({ plan_id: who.planId, structured_plan: planFor(who, name).outputs?.structured_plan ?? null });
      if (p === `/api/plans/${who.planId}/completions`) return await fulfil({ plan_id: who.planId, current_training_day: sourceView.today.training_day, completions: [] });
      if (p === "/api/custom-plans") return await fulfil([]);
      if (p === "/api/xp/progress") return await fulfil({ total_xp: 120, level: 2, recent_awards: [], streak: {} });
      if (p.startsWith("/api/training/history/")) return await fulfil({ session: historyRows[0], prescription_source: "manual", blocks: [], message: null });
      if (p === "/api/training/history") return await fulfil({ sessions: historyRows, next_before_day: null });
      if (/session-completions|sparring-logs|exercise-logs|pending|checkins/.test(p)) return await fulfil(p.includes("pending") ? { response_sets: [] } : []);
      if (p.startsWith("/api/plans/") || p.startsWith("/api/custom-plans/")) return await fulfil({ detail: "Not found" }, 404);
      return await fulfil({ notifications: [], unread_count: 0 });
    } finally {
      pending.delete(id);
    }
  });
  return {
    calls,
    switchTo(next: Persona, nextName?: string) { persona = next; planName = nextName; },
    rename(next: string) { planName = next; },
    failNextRequest(prefix: string) { failNext = prefix; },
    /** Resolves once no mocked API request has been in flight for `quietMs`. */
    async settle(quietMs = 400) {
      let quietSince = Date.now();
      while (Date.now() - quietSince < quietMs) {
        await page.waitForTimeout(50);
        if (pending.size) quietSince = Date.now();
      }
    },
  };
}

export function sessionFor(persona: Persona) {
  return JSON.stringify({ access_token: persona.token, refresh_token: "fixture", token_type: "bearer",
    expires_at: Date.parse(`${sourceView.today.training_day}T12:05:00Z`) / 1000 + 86_400,
    user: { id: persona.athleteId, email: "fixture@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} } });
}

export const STORAGE_KEY = storageKey;
export const TODAY_BLOCK = sourceView.live_prescription.session.blocks[0].display_name;

/** Steps of the central athlete workflow. Each names what makes it usable. */
export const screens = {
  home: (page: Page) => page.locator(".overview-command-primary"),
  homeReady: (page: Page) => page.locator(".overview-command-primary a").first(),
  today: (page: Page) => page.getByRole("button", { name: TODAY_BLOCK, exact: false }).first(),
  plans: (page: Page) => page.getByRole("heading", { name: /camp/i, level: 2 }).first(),
  plansReady: (page: Page) => page.locator(`a[href^="/plans/"]`).filter({ hasText: /view|open/i }).first(),
  viewer: (page: Page) => page.getByRole("heading", { level: 1, name: /camp/i }).first(),
  viewerReady: (page: Page) => page.getByRole("link", { name: /Open Today|Open next session/ }).first(),
  history: (page: Page) => page.getByRole("button", { name: /View Boxing details/ }).first(),
  details: (page: Page) => page.getByRole("dialog"),
};

export async function navigate(page: Page, name: "Home" | "Today" | "Plans" | "History") {
  // Client-side navigation through the real app nav keeps module state, as a user would.
  const href = { Home: "/", Today: "/today", Plans: "/plans", History: "/history" }[name];
  await page.locator(`nav a[href="${href}"]:visible`).first().click();
  await expect(page).toHaveURL(new RegExp(`${href === "/" ? "/$" : href}$`));
}

export type StepResult = {
  journey: string;
  requests: Array<{ method: string; path: string; bytes: number }>;
  usefulMs: number;
  readyMs: number;
};

type Mock = Awaited<ReturnType<typeof mockAthlete>>;
type Step = { journey: string; url: RegExp; act: (page: Page) => Promise<unknown>; useful: (page: Page) => ReturnType<Page["locator"]>; ready: (page: Page) => ReturnType<Page["locator"]>; settleMs?: number };

// Every step waits for its own route first, so no locator can match the
// screen being left.
const URLS = { home: /\/$/, today: /\/today$/, plans: /\/plans$/, viewer: /\/plans\/[0-9a-f-]{36}$/, history: /\/history$/ };

const planLink = (page: Page) => page.locator(`main a[href="/plans/${PRIMARY.planId}"]`).first();

/** The central athlete workflow, in the order an athlete moves through it. */
export const CORE_JOURNEY: Step[] = [
  // Startup checks deliberately deferred by ~1s still belong to startup.
  { journey: "Login → Home", url: URLS.home, act: page => page.goto("/"), useful: screens.home, ready: screens.homeReady, settleMs: 1_500 },
  { journey: "Home → Today", url: URLS.today, act: page => navigate(page, "Today"), useful: screens.today, ready: screens.today },
  { journey: "Today → Viewer", url: URLS.viewer, act: page => planLink(page).click(), useful: screens.viewer, ready: screens.viewerReady },
  { journey: "Viewer → Today", url: URLS.today, act: page => screens.viewerReady(page).click(), useful: screens.today, ready: screens.today },
  { journey: "Today → Home", url: URLS.home, act: page => navigate(page, "Home"), useful: screens.home, ready: screens.homeReady },
  { journey: "Home → Plans", url: URLS.plans, act: page => navigate(page, "Plans"), useful: page => planLink(page), ready: page => planLink(page) },
  { journey: "Plans → Viewer", url: URLS.viewer, act: page => planLink(page).click(), useful: screens.viewer, ready: screens.viewerReady },
  { journey: "Viewer → History", url: URLS.history, act: page => navigate(page, "History"), useful: screens.history, ready: screens.history },
  { journey: "History → Details", url: URLS.history, act: page => screens.history(page).click(), useful: screens.details, ready: screens.details },
  { journey: "History → Home (return)", url: URLS.home, act: async page => { await page.keyboard.press("Escape"); await navigate(page, "Home"); }, useful: screens.home, ready: screens.homeReady },
];

/** Runs each step, timing visible/usable content and the API calls it caused. */
export async function runJourney(page: Page, api: Mock, steps = CORE_JOURNEY): Promise<StepResult[]> {
  const results: StepResult[] = [];
  for (const step of steps) {
    const first = api.calls.length;
    const started = Date.now();
    await step.act(page);
    await page.waitForURL(step.url);
    await step.useful(page).waitFor({ state: "visible" });
    const usefulMs = Date.now() - started;
    await step.ready(page).waitFor({ state: "visible" });
    const readyMs = Date.now() - started;
    await api.settle(step.settleMs);
    results.push({ journey: step.journey, usefulMs, readyMs,
      requests: api.calls.slice(first).map(({ method, path, bytes }) => ({ method, path, bytes })) });
  }
  return results;
}

/** Identical GETs repeated within one step: candidates for waste, not proof. */
export function duplicateGets(requests: StepResult["requests"]): string[] {
  const seen = new Set<string>(); const repeated: string[] = [];
  for (const { method, path } of requests) {
    if (method !== "GET") continue;
    if (seen.has(path)) repeated.push(path); else seen.add(path);
  }
  return repeated;
}
