import { expect, type Page } from "@playwright/test";
import { blankDose } from "../lib/custom-plan-dose";
import { customScheduleIssues } from "../lib/custom-plan-schedule";
import { isolateFromNetwork } from "./support";
import type { CustomPlan, CustomPlanInput, LibraryEntry } from "../lib/api-schema.generated";

const athlete = "00000000-0000-4000-8000-000000000001";
const storageKey = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
export const entries: LibraryEntry[] = ["Medicine Ball Hook Throw", "Standing Cable Punch Simulation"].map((name, index) => ({
  catalogue_id: `exact-training-id-${index}`,
  name,
  category: "training",
  content_type: "strength_power",
  context: "Strength",
  equipment: [],
  equipment_state: "known",
  equipment_requirements: null,
  focus: [],
  verified_goals: [],
  exercise_details: [],
  discovery_match: null,
  exercise_key: `exercise-key-${index}`,
  media: null,
  selection_boundary: "training_validation_required",
  prescription_allowed: false,
  log_format: null,
}));

export async function setup(page: Page, baseURL: string, role = "athlete") {
  await isolateFromNetwork(page, baseURL);
  await page.route("https://fonts.googleapis.com/**", (route) => route.fulfill({ contentType: "text/css", body: "" }));
  // These journeys drive the plan's day list; the Plan page itself opens on the
  // calendar by default, which training-plan-presentation covers.
  await page.addInitScript(() => { try { localStorage.setItem("unlxck.plan-view", "list"); } catch {} });
  await page.addInitScript(
    ([key, value]) => localStorage.setItem(key, value),
    [
      storageKey,
      JSON.stringify({
        access_token: "athlete-token",
        refresh_token: "test-refresh",
        token_type: "bearer",
        expires_in: 86400,
        expires_at: Math.floor(Date.now() / 1000) + 86400,
        user: {
          id: athlete,
          email: "fighter@example.com",
          aud: "authenticated",
          role: "authenticated",
          app_metadata: {},
          user_metadata: {},
        },
      }),
    ],
  );
  const saved = new Map<string, CustomPlan>();
  const mutations: string[] = [];
  let drills = entries;
  if (process.env.CUSTOM_PLAN_TEST_API) {
    const response = await page.request.get(
      `${process.env.CUSTOM_PLAN_TEST_API}/api/training/library?q=Medicine%20Ball%20Hook%20Throw`,
      { headers: { Authorization: "Bearer athlete-token" } },
    );
    expect(response.ok()).toBe(true);
    drills = (await response.json()).entries;
    expect(drills.some((entry) => entry.name === entries[0].name)).toBe(true);
  }
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    if (request.method() !== "GET") mutations.push(url.pathname);
    if (process.env.CUSTOM_PLAN_TEST_API && /\/api\/(me$|plans(?:\/|$)|today(?:\/|$))/.test(url.pathname)) {
      const response = await page.request.fetch(`${process.env.CUSTOM_PLAN_TEST_API}${url.pathname}${url.search}`, {
        method: request.method(), headers: { Authorization: "Bearer athlete-token", "Content-Type": "application/json" },
        data: request.postData() ?? undefined,
      });
      return route.fulfill({ status: response.status(), contentType: "application/json", body: await response.body() });
    }
    if (url.pathname === "/api/xp/activity") {
      expect(request.postData()).toBeNull();
    }
    if (url.pathname.startsWith("/api/custom-plans") || url.pathname.startsWith("/api/training/library")) {
      expect(request.headers().authorization).toBe("Bearer athlete-token");
      if (process.env.CUSTOM_PLAN_TEST_API) {
        const response = await page.request.fetch(`${process.env.CUSTOM_PLAN_TEST_API}${url.pathname}${url.search}`, {
          method: request.method(),
          headers: {
            Authorization: "Bearer athlete-token",
            "Content-Type": "application/json",
          },
          data: request.postData() ?? undefined,
        });
        return route.fulfill({ response });
      }
      if (url.pathname.startsWith("/api/training/library")) {
        if (url.pathname.endsWith("/featured")) return json({ entry: null });
        if (url.pathname.endsWith("/entry"))
          return json({
            ...drills.find((entry) => entry.catalogue_id === url.searchParams.get("catalogue_id")),
            source: "exercise_bank.json",
            source_revision: "source-revision",
            row_revision: "row-revision",
            guidance: { Cues: ["Stay controlled."] },
            metadata_projection: [],
          });
        expect(url.searchParams.get("category")).toBe("training");
        const query = url.searchParams.get("q")?.toLowerCase() ?? "";
        const results = drills.filter((entry) => entry.name.toLowerCase().includes(query));
        return json({
          entries: results,
          total: results.length,
          catalogue_total: 2855,
          offset: 0,
          limit: 24,
          next_offset: null,
          revision: "fixture",
          facets: {
            type: [{ value: "strength_power", label: "Strength & power", count: 2 }],
          },
        });
      }
      if (url.pathname.endsWith("/source-suggestion"))
        return json({
          dose: { ...blankDose("intervals"), rounds: 5, work_seconds: 300, rest_seconds: 30 },
          original: "5x5min, 30s rest",
          source: "conditioning_bank.json",
          source_revision: "current-source",
          row_revision: "current-row",
        });
      const id = url.pathname.split("/")[3];
      if (request.method() === "GET") return json(id ? saved.get(id) : [...saved.values()]);
      if (request.method() === "DELETE") {
        saved.delete(id);
        return route.fulfill({ status: 204 });
      }
      const body = request.postDataJSON() as CustomPlanInput;
      expect(Object.keys(body).sort()).toEqual(["name", "sessions"]);
      const sessions = (body.sessions ?? []).map((session) => ({
        ...session,
        session_type: session.session_type ?? "mixed",
        weekday: session.weekday ?? null,
        exercises: (session.exercises ?? []).map((exercise) => {
          const dose = { ...blankDose(), ...exercise.dose };
          const incomplete =
            dose.mode === "reps"
              ? !dose.sets || !dose.reps
              : dose.mode === "duration"
                ? !dose.duration_seconds
                : dose.mode === "intervals"
                  ? !dose.rounds || !dose.work_seconds || dose.rest_seconds == null
                  : !dose.prescription;
          if (exercise.origin === "manual")
            return {
              ...exercise,
              exercise_type: exercise.exercise_type ?? null,
              coaching_cues: exercise.coaching_cues ?? null, stop_rules: exercise.stop_rules ?? null,
              equipment: exercise.equipment ?? null,
              notes: exercise.notes ?? null,
              media_id: exercise.media_id ?? null,
              community_id: exercise.community_id ?? null,
              community_revision_id: exercise.community_revision_id ?? null,
              dose,
              incomplete,
            };
          const entry = drills.find((entry) => entry.catalogue_id === exercise.catalogue_id)!;
          expect(entry).toBeTruthy();
          return {
            ...exercise,
            origin: "catalogue" as const,
            coaching_cues: exercise.coaching_cues ?? null, stop_rules: exercise.stop_rules ?? null,
            dose,
            incomplete,
            snapshot: {
              ...entry,
              category: "training" as const,
              source: "exercise_bank.json",
              source_revision: "source-revision",
              row_revision: "row-revision",
            },
          };
        }),
      }));
      const plan: CustomPlan = {
        id: id || crypto.randomUUID(),
        name: body.name,
        sessions,
        kind: "self_directed",
        incomplete: customScheduleIssues(sessions).length > 0,
        created_at: "2026-10-11T12:00:00Z",
        updated_at: "2026-10-11T12:00:00Z",
      };
      saved.set(plan.id, plan);
      return json(plan, id ? 200 : 201);
    }
    if (url.pathname === "/api/me")
      return json({
        profile: {
          athlete_id: athlete,
          email: "fighter@example.com",
          full_name: "Carlos",
          role,
          access_status: "approved",
          technical_style: ["boxing"],
          tactical_style: [],
          stance: "orthodox",
          professional_status: "professional",
          record: "",
          athlete_timezone: "Europe/London",
          athlete_locale: "en",
          appearance_mode: "dark",
          nutrition_profile: {},
          date_of_birth: "1995-01-01",
          meets_minimum_age: true,
          is_minor: false,
          terms_accepted: true,
          health_consent_granted: true,
          private_trial_ack_at: "2026-01-01T12:00:00Z",
        },
        plan_count: 0,
        latest_plan: null,
        latest_intake: null,
        username_rate_limit: {},
      });
    if (url.pathname === "/api/plans/active") return json({ detail: "No active plan" }, 404);
    if (url.pathname === "/api/plans" || url.pathname.startsWith("/api/admin/")) return json([]);
    return json({ notifications: [], unread_count: 0, level: 1, total_xp: 0 });
  });
  return { mutations, saved };
}
