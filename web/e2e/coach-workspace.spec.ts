import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { setup } from "./custom-plan-fixture";
import type { Connection, Fighter } from "../lib/coach";
import type {
  CoachFighterWeight,
  SessionDetails,
} from "../lib/api-schema.generated";
import type { TrainingHistorySession } from "../lib/training-history";
const fighterId = "00000000-0000-4000-8000-000000000003";
// Isolated test responses only. Never written to production.
const connection: Connection = {
  id: "test-connection",
  coach_id: "coach-test",
  athlete_id: fighterId,
  invited_email: "fighter@example.invalid",
  coach_name: "Test Coach",
  status: "accepted",
};
const fighter: Fighter = {
  id: fighterId,
  connection_id: connection.id,
  name: "Test Fighter",
  avatar_url: null,
  sport: "boxing",
  today: "2026-11-15",
  timezone: "Europe/London",
  fight_date: "2026-12-12",
  plan_id: "test-plan",
  plan_name: "Fight preparation",
  share_recovery: true,
  share_injuries: false,
  share_weight: true,
  schedule: [
    {
      day: "2026-11-09",
      id: "strength",
      type: "strength_power",
      title: "Strength & power",
    },
    {
      day: "2026-11-15",
      id: "spar",
      type: "sparring",
      title: "Technical sparring",
    },
  ],
  sessions: [
    {
      id: "session-1",
      day: "2026-11-15",
      session_id: "spar",
      plan_id: "test-plan",
      type: "programme",
      source: "unlxck",
      status: "done",
      rpe: 6,
      recorded_at: "2026-11-15T12:00:00Z",
    },
    {
      id: "session-2",
      day: "2026-11-09",
      session_id: "strength",
      plan_id: "test-plan",
      type: "programme",
      source: "unlxck",
      status: "done",
      rpe: 7,
      recorded_at: "2026-11-09T12:00:00Z",
    },
  ],
  recovery: [
    { day: "2026-11-15", sleep: "poor", body: "flat", source: "today" },
    {
      day: "2026-10-20",
      readiness: 4,
      fatigue: 2,
      sleep_quality: 4,
      source: "daily",
    },
  ],
  injuries: null,
  sparring_reports: [
    {
      id: "report-1",
      day: "2026-11-15",
      intensity: "technical",
      rounds: 6,
      round_seconds: 180,
      head_contact: null,
      rocked: null,
    },
  ],
};
const strengthSession: TrainingHistorySession = {
  id: "00000000-0000-4000-8000-0000000000a1",
  session_id: "strength",
  source: "unlxck",
  completion_origin: "reported",
  training_day: "2026-11-09",
  status: "modified",
  title: "Strength & power",
  activity: null,
  duration_minutes: 55,
  effort: null,
  plan_id: "test-plan",
  session_rpe: 7,
};
const history: TrainingHistorySession[] = [
  strengthSession,
  // An older block, so the list has more than its first ten rows.
  ...Array.from({ length: 11 }, (_, index) => ({
    ...strengthSession,
    id: `00000000-0000-4000-8000-0000000000b${index.toString(16)}`,
    training_day: `2026-10-${String(25 - index).padStart(2, "0")}`,
    status: "done" as const,
    title: `Camp session ${index + 1}`,
  })),
];
const details: SessionDetails = {
  session: strengthSession as SessionDetails["session"],
  prescription_source: "snapshot",
  message: null,
  sparring: null,
  runs: null,
  blocks: [
    {
      name: "Trap bar deadlift",
      kind: "exercise",
      source: "plan",
      format: null,
      prescribed: { sets: 4, reps: 6 },
      actual: {
        sets: 3,
        reps: 5,
        load: { value: 140, unit: "kg" },
      } as SessionDetails["blocks"][number]["actual"],
      status: "modified",
      reason: "fatigue",
      notes: null,
      completed_dose: null,
      responses: [],
    },
  ],
};
const weight: CoachFighterWeight = {
  shared: true,
  current_weight_kg: 73.6,
  target_weight_kg: 70,
  rolling_7_day_average_weight: 73.9,
  weekly_pace_kg: -0.4,
  entries: [
    {
      date: "2026-11-12",
      weight_kg: 74.2,
      time: "07:00",
      is_fasted: true,
      notes: null,
    },
    {
      date: "2026-11-15",
      weight_kg: 73.6,
      time: null,
      is_fasted: null,
      notes: null,
    },
  ],
};
async function fixture(
  page: Page,
  baseURL: string,
  role = "coach",
  empty = false,
  athleteConnection: Partial<Connection> = {},
  roster: Fighter[] = [fighter],
) {
  await setup(page, baseURL, role);
  let rows = empty ? [] : roster;
  // Sessions the coach records, newest first, as the API would return them.
  const logged: (TrainingHistorySession & { athlete: string })[] = [];
  let connections: Connection[] = empty
    ? []
    : [
        {
          ...connection,
          status: role === "coach" || role === "admin" ? "accepted" : "pending",
          ...athleteConnection,
        },
      ];
  const actions: Record<string, unknown>[] = [];
  await page.route("**/rest/v1/rpc/**", async (route) => {
    const name = new URL(route.request().url()).pathname.split("/").at(-1);
    const body = route.request().postDataJSON();
    if (name === "coach_connection_command") {
      actions.push(body);
      if (["revoke", "remove", "decline"].includes(body.p_action)) {
        rows = [];
        connections = [];
      }
      if (body.p_action === "accept" || body.p_action === "consent")
        connections = connections.map((c) => ({
          ...c,
          status: "accepted",
          share_recovery: body.p_recovery,
          share_injuries: body.p_injuries,
          share_weight: body.p_weight,
          allow_session_logging: body.p_logging,
          health_consent: c.health_consent ?? true,
        }));
      if (body.p_action === "invite")
        connections = [
          { ...connection, invited_email: body.p_email, status: "pending" },
        ];
      return route.fulfill({ json: "test-connection" });
    }
    return route.fulfill({
      json:
        name === "coach_connections_list"
          ? connections
          : rows
              .filter((f) => !body.p_athlete_id || f.id === body.p_athlete_id)
              .map((f) => ({
                ...f,
                schedule: body.p_athlete_id ? f.schedule : [],
              })),
    });
  });
  // The coach API checks the connection on every read, like the RPC.
  await page.route("**/api/coach/fighters/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const athlete = path.split("/")[4];
    if (!rows.some((row) => row.id === athlete))
      return route.fulfill({ status: 404, json: { detail: "not found" } });
    if (path.endsWith("/training/sessions")) {
      const body = route.request().postDataJSON();
      const row = {
        ...strengthSession,
        ...body,
        id: `00000000-0000-4000-8000-0000000000c${logged.length}`,
        source: "external" as const,
        status: "done" as const,
        title: "Pads",
        training_day: "2026-11-15",
        plan_id: null,
        session_rpe: null,
        coach_exercises_editable: true,
        logged_by_coach: true,
        logged_by_coach_name: "Test Coach",
        athlete,
      };
      logged.unshift(row);
      return route.fulfill({ status: 201, json: row });
    }
    if (path.endsWith("/exercises"))
      return route.fulfill({ json: { entries: [] } });
    return route.fulfill({
      json: path.endsWith("/weight")
        ? rows[0].share_weight
          ? weight
          : { ...weight, shared: false, entries: [] }
        : path.endsWith("/training/history")
          ? {
              sessions: [
                ...logged.filter((row) => row.athlete === athlete),
                ...history,
              ],
              next_before_day: null,
            }
          : { ...details, session: logged.find(row => row.id === path.split("/").at(-1)) ?? details.session },
    });
  });
  return {
    actions,
    logged,
    revoke: () => {
      rows = [];
      connections = [];
    },
    // The athlete changes what they share; the coach sees it on next refresh.
    share: (sharing: Partial<Fighter>) => {
      rows = rows.map((f) => ({ ...f, ...sharing }));
    },
  };
}
for (const width of [1440, 390, 320]) {
  test(`coach roster, search and fighter overview at ${width}px`, async ({
    page,
    baseURL,
  }, info) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, baseURL!);
    await page.goto("/coach");
    await expect(
      page.getByRole("heading", { name: "Your corner. In focus." }),
    ).toBeVisible();
    await expect(
      page.getByText("Boxing · 2 sessions", { exact: false }),
    ).toBeVisible();
    await expect(
      page
        .getByRole("link", { name: /Test Fighter.*sessions/ })
        .getByText("Needs attention"),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: info.outputPath(`coach-roster-${width}.png`),
      fullPage: true,
    });
    const dashboardAxe = await new AxeBuilder({ page })
      .include("main")
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    expect(
      dashboardAxe.violations.filter((v) =>
        ["serious", "critical"].includes(v.impact ?? ""),
      ),
    ).toEqual([]);
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await page.getByRole("searchbox").fill("does not exist");
    await expect(
      page.getByText("No fighters match this search."),
    ).toBeVisible();
    await page.getByRole("searchbox").fill("Test Fighter");
    await page.getByRole("link", { name: /Test Fighter.*sessions/ }).click();
    await expect(
      page.getByRole("heading", { name: "Fighter overview" }),
    ).toBeVisible();
    await expect(
      page.getByText("Poor sleep and fatigue reported"),
    ).toBeVisible();
    const strength = page
      .getByRole("article")
      .filter({ hasText: "S&C completed" });
    await expect(strength).toContainText("1/1");
    await page.getByRole("button", { name: "Previous week" }).click();
    await expect(strength).toContainText("—");
    await page.getByRole("button", { name: "Next week" }).click();
    await expect(strength).toContainText("1/1");
    await expect(page.getByText("6 rounds · technical")).toBeVisible();

    // Fight weight: the athlete's own weigh-ins and target.
    const fightWeight = page
      .locator("section")
      .filter({ has: page.getByRole("heading", { name: "Fight weight" }) });
    await expect(fightWeight).toContainText("73.6 kg");
    await expect(fightWeight).toContainText("Above target");
    await expect(fightWeight).toContainText("3.6 kg");
    await expect(fightWeight).toContainText("Days to fight");
    await expect(fightWeight.getByRole("slider")).toBeVisible();

    // Training history opens the athlete's recorded session.
    const trainingHistory = page
      .locator("section")
      .filter({ has: page.getByRole("heading", { name: "Training history" }) });
    await expect(trainingHistory.getByRole("listitem")).toHaveCount(10);
    await trainingHistory
      .getByRole("button", { name: "Show earlier sessions" })
      .click();
    await expect(trainingHistory.getByRole("listitem")).toHaveCount(12);
    await page
      .getByRole("button", { name: /View Strength & power details/ })
      .click();
    const record = page.getByRole("dialog");
    await expect(
      record.getByRole("heading", { name: "Strength & power" }),
    ).toBeVisible();
    await expect(record).toContainText("Trap bar deadlift");
    await expect(record).toContainText("140 kg");
    await expect(record).not.toContainText("by you");
    await expect(
      record.getByRole("button", { name: /Edit|Delete/ }),
    ).toHaveCount(0);
    await record.getByRole("button", { name: "Trap bar deadlift" }).click();
    await expect(record).toContainText("Reported vs prescribed");
    await record.getByRole("button", { name: "Close session details" }).click();
    await expect(record).toHaveCount(0);

    // Recovery: the last seven days, with older check-ins on request.
    await expect(page.getByText("Sleep: poor · Body: flat")).toBeVisible();
    await expect(page.getByText("Readiness: 4/5")).toBeHidden();
    await page.screenshot({
      path: info.outputPath(`coach-fighter-compact-${width}.png`),
      fullPage: true,
    });
    await page.getByText("Older check-ins (1)", { exact: true }).click();
    await expect(page.getByText("Readiness: 4/5")).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: info.outputPath(`coach-fighter-${width}.png`),
      fullPage: true,
    });
    expect(pageErrors).toEqual([]);
    const axe = await new AxeBuilder({ page })
      .include("main")
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    expect(
      axe.violations.filter((v) =>
        ["serious", "critical"].includes(v.impact ?? ""),
      ),
    ).toEqual([]);
  });
}
test("empty roster and invitation", async ({ page, baseURL }) => {
  const state = await fixture(page, baseURL!, "coach", true);
  await page.goto("/coach");
  await expect(page.getByText("Your corner starts here.")).toBeVisible();
  await page.goto("/settings/coach-access");
  await expect(
    page.getByRole("textbox", { name: "Athlete email" }),
  ).toBeVisible();
  await page
    .getByRole("textbox", { name: "Athlete email" })
    .fill("fighter@example.invalid");
  await page.getByRole("button", { name: "Invite", exact: true }).click();
  await expect(page.getByText("fighter@example.invalid")).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
  expect(state.actions[0].p_action).toBe("invite");
});
test("athlete accepts with one tap, then disconnects", async ({
  page,
  baseURL,
}, info) => {
  const state = await fixture(page, baseURL!, "athlete");
  await page.setViewportSize({ width: 390, height: 1000 });
  await page.goto("/settings/coach-access");
  await expect(page.getByText("Test Coach")).toBeVisible();
  await expect(
    page.getByText(
      "Accepting shares training, weight, recovery and injuries, and lets your coach add sessions",
      { exact: false },
    ),
  ).toBeVisible();
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await page.screenshot({
    path: info.outputPath("coach-invitation-mobile.png"),
    fullPage: true,
  });
  const axe = await new AxeBuilder({ page })
    .include("main")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(
    axe.violations.filter((v) =>
      ["serious", "critical"].includes(v.impact ?? ""),
    ),
  ).toEqual([]);
  await page.getByRole("button", { name: "Accept", exact: true }).click();
  await expect(page.getByRole("button", { name: "Disconnect" })).toBeVisible();
  expect(state.actions[0]).toMatchObject({
    p_action: "accept",
    p_recovery: true,
    p_injuries: true,
    p_weight: true,
    p_logging: true,
  });
  await expect(
    page.getByText(
      "Connected · Sharing training, weight, recovery and injuries",
    ),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("coach-connected-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Disconnect" }).click();
  await expect(page.getByText("No coach invitations yet.")).toBeVisible();
  expect(state.actions[1]).toMatchObject({ p_action: "revoke" });
});
test("athlete declines an invitation without sharing anything", async ({
  page,
  baseURL,
}) => {
  const state = await fixture(page, baseURL!, "athlete");
  await page.goto("/settings/coach-access");
  await page.getByRole("button", { name: "Decline" }).click();
  await expect(page.getByText("No coach invitations yet.")).toBeVisible();
  expect(state.actions).toEqual([
    expect.objectContaining({
      p_action: "decline",
      p_recovery: false,
      p_injuries: false,
      p_weight: false,
    }),
  ]);
});
test("coach logs a session for a chosen fighter, then from a profile", async ({
  page,
  baseURL,
}, info) => {
  const secondId = "00000000-0000-4000-8000-000000000004";
  const state = await fixture(page, baseURL!, "coach", false, {}, [
    fighter,
    { ...fighter, id: secondId, name: "Second Fighter", recovery: [] },
  ]);
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto("/coach");
  await page.getByRole("button", { name: "Log session" }).click();
  const sheet = page.getByRole("dialog", { name: "Log session" });
  // The session cannot be saved until a connected fighter is chosen.
  await expect(sheet.getByRole("combobox")).toHaveValue("");
  await expect(sheet.getByRole("option")).toHaveText([
    "Choose a fighter",
    "Test Fighter",
    "Second Fighter",
  ]);
  await sheet.getByRole("combobox").selectOption({ label: "Second Fighter" });
  await sheet.getByRole("button", { name: "Pads", exact: true }).click();
  await sheet.getByRole("button", { name: "60", exact: true }).click();
  await sheet.getByRole("button", { name: "Hard", exact: true }).click();
  await page.screenshot({ path: info.outputPath("coach-log-session.png") });
  await sheet.getByRole("button", { name: "Save Session" }).click();
  await expect(
    page.getByText("saved to Second Fighter's History", { exact: false }),
  ).toBeVisible();
  expect(state.logged).toHaveLength(1);
  expect(state.logged[0]).toMatchObject({
    athlete: secondId,
    activity: "boxing",
    sport: "boxing",
    session_type: "pads",
    duration_minutes: 60,
    effort: "hard",
  });
  // Exercises go on the session the coach just recorded.
  const exercises = page.waitForRequest((request) =>
    request
      .url()
      .endsWith(
        `/api/coach/fighters/${secondId}/training/sessions/${state.logged[0].id}/exercises`,
      ),
  );
  await page.getByRole("button", { name: "Add exercises" }).click();
  await exercises;

  // From a profile the fighter is already chosen, and History shows the credit.
  await page.goto(`/coach/fighters/${fighterId}`);
  await page.getByRole("button", { name: "Log session" }).click();
  await expect(sheet.getByText("For Test Fighter")).toBeVisible();
  await expect(sheet.getByRole("combobox")).toHaveCount(0);
  await sheet.getByRole("button", { name: "Pads", exact: true }).click();
  await sheet.getByRole("button", { name: "45", exact: true }).click();
  await sheet.getByRole("button", { name: "Moderate", exact: true }).click();
  await sheet.getByRole("button", { name: "Save Session" }).click();
  await expect(
    page.getByText("saved to Test Fighter's History", { exact: false }),
  ).toBeVisible();
  expect(state.logged[0]).toMatchObject({
    athlete: fighterId,
    effort: "moderate",
  });
  await expect(
    page
      .getByRole("listitem")
      .filter({ hasText: "Logged by coach Test Coach" }),
  ).toHaveCount(1);
  await page.screenshot({
    path: info.outputPath("coach-logged-history.png"),
    fullPage: true,
  });
});

test("a connection accepted before weight sharing asks before sharing weight", async ({
  page,
  baseURL,
}) => {
  const state = await fixture(page, baseURL!, "athlete", false, {
    status: "accepted",
    share_recovery: true,
    share_injuries: false,
    share_weight: false,
    health_consent: true,
  });
  await page.goto("/settings/coach-access");
  await expect(
    page.getByText("Connected · Sharing training and recovery", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("Not sharing weight and injuries."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Share weight and injuries" }).click();
  await expect(
    page.getByText(
      "Connected · Sharing training, weight, recovery and injuries",
    ),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /^Share / })).toHaveCount(0);
  expect(state.actions).toEqual([
    expect.objectContaining({
      p_action: "consent",
      p_recovery: true,
      p_injuries: true,
      p_weight: true,
    }),
  ]);
});
test("withdrawn health consent shows only training as shared", async ({
  page,
  baseURL,
}) => {
  await fixture(page, baseURL!, "athlete", false, {
    status: "accepted",
    share_recovery: true,
    share_injuries: true,
    share_weight: true,
    health_consent: false,
  });
  await page.goto("/settings/coach-access");
  await expect(
    page.getByText("Connected · Sharing training", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Weight, recovery and injuries paused while health-data consent is off.",
    ),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /^Share / })).toHaveCount(0);
});
test("withdrawn sharing clears loaded weight and session records", async ({
  page,
  baseURL,
}) => {
  const state = await fixture(page, baseURL!);
  await page.goto(`/coach/fighters/${fighterId}`);
  const fightWeight = page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "Fight weight" }) });
  await expect(fightWeight).toContainText("73.6 kg");
  await page
    .getByRole("button", { name: /View Strength & power details/ })
    .click();
  await expect(page.getByRole("dialog")).toContainText("Trap bar deadlift");
  state.share({ share_weight: false, share_recovery: false });
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(fightWeight).toContainText("Not shared.");
  await expect(fightWeight).not.toContainText("73.6");
  // The open record closes; the history reloads under the new permissions.
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
test("lost sharing clears fighter overview on refresh", async ({
  page,
  baseURL,
}) => {
  const state = await fixture(page, baseURL!);
  await page.goto(`/coach/fighters/${fighterId}`);
  await expect(
    page.getByRole("heading", { name: "Test Fighter" }),
  ).toBeVisible();
  state.revoke();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(
    page.getByRole("heading", { name: "Fighter unavailable" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "Test Fighter" })).toHaveCount(
    0,
  );
});
test("RPC failure hides stale performance", async ({ page, baseURL }) => {
  await fixture(page, baseURL!);
  await page.route("**/rest/v1/rpc/coach_fighters", (r) =>
    r.fulfill({ status: 403, json: { message: "denied" } }),
  );
  await page.goto("/coach");
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "Access unavailable",
  );
  await expect(
    page.getByRole("heading", { name: "Your fighters" }),
  ).toHaveCount(0);
});

test("administrator can coach while keeping admin navigation", async ({
  page,
  baseURL,
}) => {
  await fixture(page, baseURL!, "admin");
  await page.goto("/coach");
  await expect(
    page.getByRole("heading", { name: "Your corner. In focus." }),
  ).toBeVisible();
  await expect(page.locator('a[href="/admin"]').first()).toBeVisible();
});


test("coach reopens exercises on their own saved session", async ({ page, baseURL }) => {
  const state = await fixture(page, baseURL!);
  state.logged.push({ ...strengthSession, id: "00000000-0000-4000-8000-0000000000c9", athlete: fighterId,
    source: "external", activity: "strength", title: "Strength", logged_by_coach: true,
    logged_by_coach_name: "Test Coach", coach_exercises_editable: true });
  await page.goto(`/coach/fighters/${fighterId}`);
  await page.getByRole("listitem").filter({ hasText: "Logged by coach Test Coach" }).getByRole("button").click();
  const request = page.waitForRequest(req => req.url().endsWith(`/api/coach/fighters/${fighterId}/training/sessions/00000000-0000-4000-8000-0000000000c9/exercises`));
  await page.getByRole("button", { name: "Edit exercises" }).click();
  await request;
  await expect(page.getByRole("dialog", { name: "Add exercises" })).toBeVisible();
  await expect(page.getByRole("searchbox")).toBeVisible();
});
