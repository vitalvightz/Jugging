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
  can_leave_feedback: true,
  allow_session_logging: true,
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
  coach_feedback_allowed: true,
  athlete_id: fighterId,
  session: strengthSession as SessionDetails["session"],
  prescription_source: "snapshot",
  message: null,
  sparring: null,
  runs: null,
  blocks: [
    {
      feedback_ref: "deadlift",
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
  current_weight_recorded_at: "2026-11-15",
  cut_assessment: { weight_cut_status: "on_track", weight_cut_risk_band: "low", days_until_fight: 27, kg_to_target: 3.6, weight_cut_behind_kg: null },
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
  const feedback: { id: string; coach_name: string; note: string; exercise_ref: string; target_id: string; source: string; updated_at: string }[] = [];
  await page.route("**/rest/v1/coach_feedback?**", route => {
    const params = new URL(route.request().url()).searchParams;
    return route.fulfill({ json: feedback.filter(note => ["source", "target_id", "exercise_ref"].every(key => params.get(key) === `eq.${note[key as "source"]}`)) });
  });
  const actions: Record<string, unknown>[] = [];
  await page.route("**/rest/v1/rpc/**", async (route) => {
    const name = new URL(route.request().url()).pathname.split("/").at(-1);
    const body = route.request().postDataJSON();
    if (name === "save_coach_feedback") {
      const before = feedback.find(note => note.source === body.p_source && note.target_id === body.p_target_id && note.exercise_ref === body.p_exercise_ref);
      if (before) before.note = body.p_note;
      else feedback.push({ id: `feedback-${feedback.length}`, coach_name: "Test Coach", source: body.p_source, target_id: body.p_target_id, exercise_ref: body.p_exercise_ref, note: body.p_note, updated_at: "2026-11-16T10:00:00Z" });
      return route.fulfill({ json: "saved" });
    }
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
          share_media: body.p_media,
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
    if (path.endsWith("/plans")) return route.fulfill({ json: [] });
    if (path.endsWith("/training/plan") || path.endsWith("/training/attachments")) return route.fulfill({ json: [] });
    if (path.endsWith("/media")) return route.fulfill({ json: { share_media: false, progress_count: 0, recent: [] } });
    if (path.endsWith("/exercises"))
      return route.fulfill({ json: { entries: [] } });
    return route.fulfill({
      json: path.endsWith("/weight")
        ? rows.find(row => row.id === athlete)?.share_weight
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
    feedback,
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
    await expect(page.getByText("6 rounds", { exact: true })).toBeVisible();
    await expect(page.getByText("Technical", { exact: true })).toBeVisible();

    // Fight weight: the athlete's own weigh-ins and target.
    const fightWeight = page
      .locator("section")
      .filter({ has: page.getByRole("heading", { name: "Fight weight" }) });
    await expect(fightWeight).toContainText("73.6 kg");
    await expect(fightWeight).toContainText("Above target");
    await expect(fightWeight).toContainText("3.6 kg");
    await expect(fightWeight).toContainText("Days to fight");
    await expect(fightWeight.getByRole("slider")).toBeVisible();

    // A malformed attachment response should still leave the training record readable.
    if (width === 390) {
      await page.route("**/api/coach/fighters/*/training/attachments?**", route =>
        route.fulfill({ json: { detail: "Unexpected response" } }));
    }
    const trainingHistory = page
      .locator("section")
      .filter({ has: page.getByRole("heading", { name: "Training history" }) });
    await trainingHistory.getByRole("button", { name: "List", exact: true }).click();
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
    await expect(page.getByText("Sleep: Poor", { exact: true }).last()).toBeVisible();
    await expect(page.getByText("Body: Flat", { exact: true }).last()).toBeVisible();
    await expect(page.getByText("Readiness: 4/5")).toBeVisible();
    await page.screenshot({
      path: info.outputPath(`coach-fighter-compact-${width}.png`),
      fullPage: true,
    });
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
  await page.route("**/rest/v1/rpc/coach_chat_inbox", (route) => route.fulfill({ json: [{
    chat_id: "chat-1", other_id: "coach-test", name: "Test Coach", avatar_url: null, role: "athlete", active: true,
    last_message_at: new Date().toISOString(), last_preview: "How did sparring feel?", last_from_me: false, unread: 2,
    other_read_at: null, fight_date: null, attention: false, missed_checkin: false,
  }] }));
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
  // Once connected, the card is the way into the chat, with what is waiting there.
  const message = page.getByRole("link", { name: "Message Test Coach, 2 unread" });
  await expect(message).toHaveAttribute("href", "/chat?with=coach-test");
  await expect(page.getByText("Test: How did sparring feel?")).toBeVisible();
  expect(state.actions[0]).toMatchObject({
    p_action: "accept",
    p_recovery: true,
    p_injuries: true,
    p_weight: true,
    p_logging: true,
    // Accepting never shares photos or voice notes.
    p_media: false,
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
  await expect(page.getByRole("button", { name: "Share weight and injuries", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Share photos & voice notes", exact: true })).toBeVisible();
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
  await page.getByRole("button", { name: "List", exact: true }).click();
  await page
    .getByRole("button", { name: /View Strength & power details/ })
    .click();
  await expect(page.getByRole("dialog")).toContainText("Trap bar deadlift");
  state.share({ share_weight: false, share_recovery: false });
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(fightWeight).toContainText("Not shared.");
  await expect(fightWeight).not.toContainText("73.6");
  const recoveryPanel = page.locator("section").filter({ has: page.getByRole("heading", { name: "Recovery", exact: true }) });
  await expect(recoveryPanel.getByText("Recovery check-ins: Not shared", { exact: true })).toBeVisible();
  await expect(recoveryPanel.getByText("Sleep: Poor", { exact: true })).toHaveCount(0);
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
  await page.getByRole("button", { name: "List", exact: true }).click();
  await page.getByRole("listitem").filter({ hasText: "Logged by coach Test Coach" }).getByRole("button").click();
  const request = page.waitForRequest(req => req.url().endsWith(`/api/coach/fighters/${fighterId}/training/sessions/00000000-0000-4000-8000-0000000000c9/exercises`));
  await page.getByRole("button", { name: "Edit exercises" }).click();
  await request;
  await expect(page.getByRole("dialog", { name: "Add exercises" })).toBeVisible();
  await expect(page.getByRole("searchbox")).toBeVisible();
});


test("legacy athlete can allow logging without granting unshared health categories", async ({ page, baseURL }) => {
  const state = await fixture(page, baseURL!, "athlete", false, {
    status: "accepted", share_recovery: false, share_injuries: false, share_weight: false,
    allow_session_logging: false, health_consent: true,
  });
  await page.goto("/settings/coach-access");
  await page.getByRole("button", { name: "Allow session logging" }).click();
  await expect(page.getByRole("button", { name: "Allow session logging" })).toHaveCount(0);
  expect(state.actions[0]).toMatchObject({ p_action: "consent", p_logging: true,
    p_recovery: false, p_injuries: false, p_weight: false });
});


test("session logging offers only authorised fighters", async ({ page, baseURL }) => {
  await fixture(page, baseURL!, "coach", false, {}, [fighter,
    { ...fighter, id: "not-authorised", name: "Read Only Fighter", allow_session_logging: false },
    { ...fighter, id: "legacy", name: "Legacy Fighter", allow_session_logging: undefined },
  ]);
  await page.goto("/coach");
  await page.getByRole("button", { name: "Log session" }).click();
  await expect(page.getByRole("dialog", { name: "Log session" }).getByText("For Test Fighter")).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("combobox")).toHaveCount(0);
});

test("session logging is hidden without authorised fighters", async ({ page, baseURL }) => {
  await fixture(page, baseURL!, "coach", false, {}, [{ ...fighter, allow_session_logging: false }]);
  await page.goto("/coach");
  await expect(page.getByRole("heading", { name: "Your corner. In focus." })).toBeVisible();
  await expect(page.getByRole("button", { name: "Log session" })).toHaveCount(0);
  await page.goto(`/coach/fighters/${fighterId}`);
  await expect(page.getByRole("heading", { name: "Training history" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Log session" })).toHaveCount(0);
});

for (const role of ["coach", "admin", "athlete"]) {
  for (const width of [390, 1440]) {
    test(`${role} workspace navigation at ${width}px`, async ({ page, baseURL }) => {
      await fixture(page, baseURL!, role);
      await page.setViewportSize({ width, height: 1000 });
      await page.goto(role === "athlete" ? "/settings" : "/coach/fighters");
      if (role === "athlete") {
        await expect(page.locator('.mobile-tab-bar a[href="/today"]')).toHaveCount(1);
        await expect(page.getByRole("navigation", { name: "Coach navigation" })).toHaveCount(0);
        return;
      }
      await page.reload();
      const sidebar = page.locator('#app-sidebar');
      if (width < 720) {
        await expect(page.getByRole("navigation", { name: "Coach navigation" })).toBeVisible();
        await expect(page.locator('.mobile-tab-bar a')).toHaveText(["Overview", "Chats", "Fighters", "Plans"]);
        await page.getByRole("button", { name: "Open navigation", exact: true }).click();
      }
      await expect(sidebar.locator('a[href="/coach/invitations"]')).toBeVisible();
      await expect(sidebar.locator('a[href="/today"]')).toHaveCount(0);
      await sidebar.getByRole("link", { name: /^Plans/ }).click();
      await expect(page.getByRole("button", { name: "Create New Plan", exact: true })).toBeVisible();
      await page.goto('/coach/log');
      await expect(page.getByRole('dialog', { name: 'Log session' })).toBeVisible();
      await page.goto('/coach/settings');
      await page.getByRole("link", { name: "Coach access", exact: true }).click();
      await expect(page).toHaveURL(/\/coach\/invitations$/);
      await expect(page.getByRole("heading", { name: "Coach access" })).toBeVisible();
      await page.screenshot({ path: `test-results/coach-nav-${role}-${width}.png`, fullPage: true });
      await expect(page.locator('#app-sidebar a[href="/coach/fighters"]')).toHaveCount(1);
      if (role === "admin") {
        if (width < 720) await page.getByRole("button", { name: "Open navigation", exact: true }).click();
        await sidebar.getByRole("link", { name: "Switch Workspace" }).click();
        await expect(page).toHaveURL(/\/admin$/);
        await expect(sidebar.locator('a[href="/today"]')).toHaveCount(1);
      }
    });
  }
}


test("sparring totals use displayed reports and coach feedback stays attached to its record", async ({ page, baseURL }) => {
  const state = await fixture(page, baseURL!, "coach", false, {}, [{ ...fighter, share_injuries: true,
    sparring_reports: [
      { id: "report-hard", day: "2026-11-15", intensity: "hard", rounds: 5, round_seconds: 180, head_contact: "light", rocked: false },
      { id: "report-technical", day: "2026-11-14", intensity: "technical", rounds: 7, round_seconds: 180, head_contact: "heavy", rocked: true },
    ],
  }]);
  await page.setViewportSize({ width: 390, height: 1100 });
  await page.goto(`/coach/fighters/${fighterId}`);
  const sparring = page.locator("section").filter({ has: page.getByRole("heading", { name: "Sparring", exact: true }) });
  await expect(sparring.getByText("2 reports", { exact: true })).toBeVisible();
  await expect(sparring.getByText("12", { exact: true })).toBeVisible();
  await expect(sparring.getByText("Heavy head contact", { exact: false })).toBeVisible();
  await expect(sparring.getByText("Athlete reported being rocked", { exact: false })).toBeVisible();
  await sparring.getByRole("button", { name: "Add feedback", exact: true }).first().click();
  await sparring.getByRole("textbox", { name: "Feedback" }).fill("Keep your guard up.");
  await sparring.getByRole("button", { name: "Save feedback" }).click();
  await expect(sparring.getByText("Keep your guard up.", { exact: true })).toBeVisible();
  await expect.poll(() => state.feedback[0]).toMatchObject({ source: "sparring", target_id: "report-hard", exercise_ref: "" });
  await page.screenshot({ path: "test-results/coach-sparring-feedback-mobile.png", fullPage: true });
  await page.getByRole("button", { name: "List", exact: true }).click();
  await page.getByRole("listitem").filter({ hasText: "Strength & power" }).getByRole("button").first().click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Add feedback", exact: true }).first().click();
  await dialog.getByRole("textbox", { name: "Feedback" }).fill("Good session today.");
  await dialog.getByRole("button", { name: "Save feedback" }).click();
  await expect(dialog.getByText("Good session today.", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Trap bar deadlift", exact: true }).click();
  await dialog.getByRole("button", { name: "Add feedback", exact: true }).click();
  await dialog.getByRole("textbox", { name: "Feedback" }).fill("Control the descent.");
  await dialog.getByRole("button", { name: "Save feedback" }).click();
  await expect(dialog.getByText("Control the descent.", { exact: true })).toBeVisible();
  expect(state.feedback).toHaveLength(3);
  expect(state.feedback[2]).toMatchObject({ source: "unlxck", exercise_ref: "deadlift" });
});

for (const width of [320, 1440]) {
  for (const populated of [false, true]) {
    test(`fighter profile details populated=${populated} at ${width}px`, async ({ page, baseURL }) => {
      await fixture(page, baseURL!, "coach", false, {}, [{ ...fighter,
        age: populated ? 27 : null, record: populated ? "4-1-0" : null,
        technical_style: populated ? ["pressure_fighter"] : [], tactical_style: populated ? ["counter_puncher"] : [],
        stance: populated ? "orthodox" : null, sport: populated ? "boxing" : null,
        last_training: populated ? { day: "2026-11-14", activity: "boxing" } : null,
        sessions: populated ? fighter.sessions : [], schedule: [],
      }]);
      await page.setViewportSize({ width, height: 1000 });
      await page.goto('/coach/fighters');
      await expect(page.getByRole("heading", { name: "Your fighters" }).first()).toBeVisible();
      if (populated) {
        await expect(page.getByText(/Boxing · 27 years · 4-1-0/)).toBeVisible();
        await expect(page.getByText(/Last trained/)).toBeVisible();
      } else {
        await expect(page.getByText(/Last trained/)).toHaveCount(0);
      }
      await page.goto(`/coach/fighters/${fighterId}`);
      await expect(page.getByText("Camp timeline", { exact: true })).toHaveCount(0);
      await expect(page.getByText("Next fight", { exact: true })).toBeVisible();
      if (populated) {
        await expect(page.getByText(/Pressure fighter · Counter puncher · Orthodox/)).toBeVisible();
        await expect(page.getByText(/Last training · Boxing/)).toBeVisible();
      } else {
        await expect(page.getByText(/years|Last training|Orthodox/)).toHaveCount(0);
      }
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await page.screenshot({ path: `test-results/coach-profile-${populated}-${width}.png`, fullPage: true });
    });
  }
}


test("sparring stays visible when the selected week has no reports", async ({ page, baseURL }) => {
  await fixture(page, baseURL!);
  await page.goto(`/coach/fighters/${fighterId}`);
  await page.getByRole("button", { name: "Previous week" }).click();
  await expect(page.getByRole("heading", { name: "Sparring", exact: true })).toBeVisible();
  await expect(page.getByText("No sparring reports this week", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Next week" }).click();
  await expect(page.getByText("No sparring reports this week", { exact: true })).toHaveCount(0);
  await expect(page.getByText("6 rounds", { exact: true })).toBeVisible();
});

for (const width of [320, 1440]) {
  test(`coach calendar loads older months and opens distinct records at ${width}px`, async ({ page, baseURL }) => {
    await fixture(page, baseURL!);
    const row = { ...strengthSession, source: "external" as const, status: "done" as const, activity: "strength" as const,
      training_day: "2026-11-12", training_category: "strength" as const };
    const records = [
      { ...row, id: "a", title: "Strength A" }, { ...row, id: "b", title: "Strength B" },
      { ...row, id: "rehab", title: "Rehab", activity: null, training_category: "rehab" as const },
      { ...row, id: "modified", title: "Adjusted strength", status: "modified" as const },
    ];
    const cursors: (string | null)[] = [];
    await page.route("**/api/coach/fighters/**/training/history**", route => {
      const url = new URL(route.request().url());
      if (!url.pathname.endsWith("/history")) {
        const session = records.find(row => row.id === url.pathname.split("/").at(-1));
        return route.fulfill({ json: { ...details, session: session ?? strengthSession } });
      }
      const cursor = url.searchParams.get("before_day"); cursors.push(cursor);
      return route.fulfill({ json: cursor === null ? {
        sessions: [...records, records[0], { ...row, id: "skip", training_day: "2026-11-10", status: "skipped" },
          { ...row, id: "deleted", deleted_at: "2026-11-13" }, { ...row, id: "future", training_day: "2026-11-20" }], next_before_day: "2026-11-10",
      } : cursor === "2026-11-10" ? {
        sessions: [{ ...row, id: "oct30", training_day: "2026-10-30", title: "October strength" }], next_before_day: "2026-10-30",
      } : { sessions: [{ ...row, id: "oct2", training_day: "2026-10-02", title: "Older strength" }], next_before_day: null } });
    });
    await page.setViewportSize({ width, height: 1100 });
    await page.goto(`/coach/fighters/${fighterId}`);
    const history = page.locator("section").filter({ has: page.getByRole("heading", { name: "Training history", exact: true }) });
    await expect(history.getByRole("button", { name: "Calendar", exact: true })).toHaveAttribute("aria-pressed", "true");
    const day = history.getByRole("button", { name: /12 Nov 2026: 4 completed sessions/ });
    await day.click();
    await expect(history.getByRole("listitem")).toHaveCount(4);
    await expect(day.locator('[data-training-tone="strength"]')).toHaveCount(2);
    await expect(day.locator('[data-training-tone="rehab"]')).toHaveCount(1);
    await expect(day.locator('[data-training-tone="modified"]')).toHaveCount(1);
    await history.getByRole("button", { name: /View Strength A details/ }).click();
    await expect(page.getByRole("dialog").getByRole("heading", { name: "Strength A", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Close session details" }).click();
    await history.getByRole("button", { name: "Previous month" }).click();
    await expect(history.getByText("October 2026", { exact: true })).toBeVisible();
    await history.getByRole("button", { name: /2 Oct 2026: 1 completed sessions/ }).click();
    await expect(history.getByRole("listitem")).toHaveCount(1);
    await expect(history.getByText("Older strength", { exact: true })).toBeVisible();
    await history.getByRole("button", { name: "Next month" }).click();
    await expect(history.getByText("November 2026", { exact: true })).toBeVisible();
    await history.getByRole("button", { name: "Today", exact: true }).click();
    await expect(history.getByText("No sessions recorded or planned for this day.")).toBeVisible();
    await history.getByRole("button", { name: "List", exact: true }).click();
    await expect(history.getByRole("listitem")).toHaveCount(6);
    await expect(page).toHaveURL(new RegExp(`/coach/fighters/${fighterId}$`));
    expect(cursors).toEqual([null, "2026-11-10", "2026-10-30"]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await history.getByRole("button", { name: "Calendar", exact: true }).click();
    await history.getByRole("button", { name: /12 Nov 2026: 4 completed sessions/ }).click();
    await page.screenshot({ path: `test-results/coach-training-calendar-${width}.png`, fullPage: true });
  });
}

test("recovery merges dates, distinguishes mild injury from normal check-in, and limits recent reports", async ({ page, baseURL }) => {
  await fixture(page, baseURL!, "coach", false, {}, [{ ...fighter, share_injuries: true,
    technical_style: ["Boxing", "distance_striker"],
    injuries: [{ id: "injury", body_area: "Left bicep", severity: "mild", status: "open", updated_at: "2026-11-15" }],
    recovery: [
      { day: "2026-11-15", source: "today", sleep: "good", body: "normal", recorded_at: "2026-11-15T12:00:00Z" },
      { day: "2026-11-15", source: "daily", readiness: 4, fatigue: 2, sleep_quality: 4, recorded_at: "2026-11-15T10:00:00Z" },
      { day: "2026-11-14", source: "today", sleep: "poor", body: "flat" },
      { day: "2026-11-13", source: "today", sleep: "okay", body: "normal" },
      { day: "2026-11-12", source: "today", sleep: "good", body: "normal" },
    ],
    sparring_reports: [15, 14, 13, 12].map(day => ({ id: `report-${day}`, day: `2026-11-${day}`, intensity: "light", rounds: 2, round_seconds: 180, head_contact: "none", rocked: false })),
  }]);
  await page.setViewportSize({ width: 390, height: 1100 });
  await page.goto(`/coach/fighters/${fighterId}`);
  const recovery = page.locator("section").filter({ has: page.getByRole("heading", { name: "Recovery", exact: true }) });
  await expect(recovery.getByText("Recovery check-in: No new concerns", { exact: true })).toBeVisible();
  await expect(recovery.getByText("Mild · Open", { exact: true })).toHaveAttribute("data-recovery-tone", "caution");
  await expect(recovery.getByText("Sleep: Poor", { exact: true })).toHaveAttribute("data-recovery-tone", "concern");
  await expect(recovery.getByText("Body: Flat", { exact: true })).toHaveAttribute("data-recovery-tone", "caution");
  // One injury row plus three unique check-in dates, with both sources retained.
  await expect(recovery.getByRole("listitem")).toHaveCount(4);
  await expect(recovery.getByText("Readiness: 4/5", { exact: true })).toBeVisible();
  await expect(recovery.getByText("Fatigue: 2/5", { exact: true })).toBeVisible();
  await recovery.getByRole("button", { name: "View all check-ins" }).click();
  await expect(recovery.getByRole("listitem")).toHaveCount(5);
  const sparring = page.locator("section").filter({ has: page.getByRole("heading", { name: "Sparring", exact: true }) });
  await expect(sparring.getByRole("listitem")).toHaveCount(3);
  await sparring.getByRole("button", { name: "View all reports" }).click();
  await expect(sparring.getByRole("listitem")).toHaveCount(4);
  const identity = page.locator('section').filter({ has: page.getByRole("heading", { name: "Test Fighter", exact: true }) });
  await expect(identity.getByText("Boxing", { exact: true })).toHaveCount(1);
  await expect(identity.getByText("Distance striker", { exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/coach-recovery-grouped-mobile.png", fullPage: true });
});

test("missing recovery stays grey and skipped sessions do not populate either history view", async ({ page, baseURL }) => {
  await fixture(page, baseURL!, "coach", false, {}, [{ ...fighter, share_recovery: true, recovery: [], injuries: null }]);
  await page.route("**/api/coach/fighters/**/training/history?**", route => route.fulfill({ json: {
    sessions: [{ ...strengthSession, status: "skipped" }], next_before_day: null,
  } }));
  await page.goto(`/coach/fighters/${fighterId}`);
  const recovery = page.locator("section").filter({ has: page.getByRole("heading", { name: "Recovery", exact: true }) });
  await expect(recovery.getByText("Not recorded", { exact: true })).toHaveCount(2);
  await expect(recovery.locator('strong[data-recovery-tone="unknown"]')).toHaveCount(2);
  await expect(recovery.getByText("No check-ins recorded.", { exact: true })).toBeVisible();
  await expect(recovery.getByText("Recovery check-in: No new concerns", { exact: true })).toHaveCount(0);
  const history = page.locator("section").filter({ has: page.getByRole("heading", { name: "Training history", exact: true }) });
  await expect(history.locator('[data-training-tone]')).toHaveCount(6); // Legend only; no training dots.
  await expect(history.getByRole("listitem")).toHaveCount(0);
  await history.getByRole("button", { name: "List", exact: true }).click();
  await expect(history.getByText("No completed sessions recorded yet.", { exact: true })).toBeVisible();
});

for (const width of [320, 390]) {
  test(`coach permission buttons keep readable spacing at ${width}px`, async ({ page, baseURL }) => {
    await fixture(page, baseURL!, "athlete", false, { status: "accepted", share_weight: false, share_recovery: false, share_injuries: false, allow_session_logging: false, health_consent: true });
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/settings/coach-access');
    const share = page.getByRole('button', { name: /^Share weight/ });
    const logging = page.getByRole('button', { name: 'Allow session logging' });
    await expect(share).toBeVisible(); await expect(logging).toBeVisible();
    const a = (await share.boundingBox())!; const b = (await logging.boundingBox())!;
    expect(b.y).toBeGreaterThanOrEqual(a.y + a.height + 8);
    expect(a.width).toBeGreaterThan(240);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/coach-permissions-spacing-${width}.png`, fullPage: true });
  });
}

test("coach sections fold and reopen without losing their contents", async ({ page, baseURL }) => {
  await fixture(page, baseURL!);
  await page.setViewportSize({ width: 390, height: 1000 });
  await page.goto(`/coach/fighters/${fighterId}`);
  for (const [name, content] of [["Sparring", "Recent sparring activity"], ["Training history", "November 2026"], ["Recovery", "Recent check-ins"], ["Fight weight", "Days to fight"]] as const) {
    const section = page.locator("section").filter({ has: page.getByRole("heading", { name, exact: true }) });
    const details = section.locator("details").first();
    await expect(details).toHaveAttribute("open", "");
    await details.locator("summary").click();
    await expect(details).not.toHaveAttribute("open", "");
    await expect(section.getByText(content, { exact: true })).toBeHidden();
    await details.locator("summary").click();
    await expect(details).toHaveAttribute("open", "");
  }
});

for (const width of [390, 1280]) {
  test(`fight preparation calendar and priority concerns stay compact at ${width}px`, async ({ page, baseURL }) => {
    await fixture(page, baseURL!, "coach", false, {}, [{ ...fighter, share_injuries: true,
      injuries: [{ id: "arm", body_area: "Left bicep", severity: "mild", status: "open", updated_at: fighter.today }],
      sparring_reports: [{ id: "contact", day: "2026-11-12", rounds: 5, intensity: "technical", round_seconds: 180, head_contact: "heavy", rocked: true }],
    }]);
    await page.route("**/api/coach/fighters/*/weight", route => route.fulfill({ json: { ...weight,
      cut_assessment: { ...weight.cut_assessment, weight_cut_status: "too_aggressive", weight_cut_risk_band: "severe" },
    } }));
    await page.route("**/api/coach/fighters/*/training/plan", route => route.fulfill({ json: [
      { id: "future", plan_id: "test-plan", day: "2026-12-10", title: "Planned strength", type: "strength_power", blocks: [{ ...details.blocks[0], name: "Squat", status: null, actual: {} }] },
      // A completed occurrence also appearing in the plan must not be duplicated.
      { id: "strength", plan_id: "test-plan", day: "2026-11-09", title: "Duplicate", type: "strength_power", blocks: [] },
    ] }));
    const runtimeErrors: string[] = [];
    page.on("pageerror", error => runtimeErrors.push(error.message));
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`/coach/fighters/${fighterId}`);
    const attention = page.getByRole("region", { name: "Needs attention" });
    await expect(attention.getByRole("listitem")).toHaveCount(3);
    await expect(attention.getByRole("listitem").first()).toContainText("Cut too aggressive");
    await expect(attention.getByRole("listitem").nth(1)).toContainText("rocked");
    await attention.getByRole("button", { name: "View all", exact: true }).click();
    expect(await attention.getByRole("listitem").count()).toBeGreaterThan(3);
    await expect(attention.getByText(/Left bicep/)).toBeVisible();
    await page.locator('#fight-weight > details > summary').click();
    await attention.getByRole("link").first().click();
    await expect(page.locator('#fight-weight > details')).toHaveAttribute("open", "");
    await expect(page.locator('#fight-weight').getByText("Cut too aggressive", { exact: true })).toBeVisible();
    const calendar = page.locator('#training-history');
    await calendar.getByRole("button", { name: "Next month" }).click();
    await expect(calendar.getByText("December 2026", { exact: true })).toBeVisible();
    await calendar.getByRole("button", { name: /10 Dec 2026: 0 completed sessions, 1 planned/ }).click();
    await calendar.getByRole("button", { name: /Planned strength/ }).click();
    await expect(calendar.getByRole("button", { name: "Squat", exact: true })).toBeVisible();
    await calendar.getByRole("button", { name: /12 Dec 2026: 0 completed sessions, Fight day/ }).click();
    await expect(calendar.getByText("◆ Fight day", { exact: true }).last()).toBeVisible();
    await calendar.getByRole("button", { name: "Next month" }).click();
    await expect(calendar.getByText("January 2027", { exact: true })).toBeVisible();
    await calendar.getByRole("button", { name: "Today", exact: true }).click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(runtimeErrors).toEqual([]);
    await page.screenshot({ path: `/tmp/coach-fight-preparation-${width}.png`, fullPage: true });
  });
}

test("mobile weight keeps recent weigh-ins compact with full history available", async ({ page, baseURL }) => {
  await fixture(page, baseURL!);
  await page.route("**/api/coach/fighters/*/weight", route => route.fulfill({ json: { ...weight, entries: Array.from({ length: 10 }, (_, index) => ({ ...weight.entries[0], date: `2026-11-${String(15 - index).padStart(2, "0")}` })) } }));
  await page.setViewportSize({ width: 320, height: 900 });
  await page.goto(`/coach/fighters/${fighterId}`);
  const section = page.locator('#fight-weight');
  await expect(section.getByRole("listitem")).toHaveCount(3);
  await section.getByRole("button", { name: "View all 10 weigh-ins" }).click();
  await expect(section.getByRole("listitem")).toHaveCount(10);
  await section.getByRole("button", { name: "Show fewer", exact: true }).click();
  await expect(section.getByRole("listitem")).toHaveCount(3);
  for (const label of ["7 days", "30 days", "All"]) {
    await section.getByRole("button", { name: label, exact: true }).click();
    await expect(section.getByRole("button", { name: label, exact: true })).toHaveAttribute("aria-pressed", "true");
  }
  for (const label of ["Current", "Target", "Above target", "7-day average", "Per week", "Days to fight"]) {
    const text = section.getByText(label, { exact: true });
    const bounds = await text.boundingBox();
    const tile = await text.locator("..").boundingBox();
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(tile!.x + tile!.width);
  }
  const plot = await section.getByRole("slider").boundingBox();
  expect(plot!.height).toBeLessThanOrEqual(120);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await section.screenshot({ path: "/tmp/coach-weight-mobile.png" });
});

for (const age of [25, 17]) {
  test(`failed weight request shows authorised availability without a minor cut alert at age ${age}`, async ({ page, baseURL }) => {
    await fixture(page, baseURL!, "coach", false, {}, [{ ...fighter, age, share_recovery: false, share_injuries: false }]);
    await page.route("**/api/coach/fighters/*/weight", route => route.fulfill({ status: 503, json: { detail: "Unavailable" } }));
    await page.setViewportSize({ width: 390, height: 900 });
    await page.goto(`/coach/fighters/${fighterId}`);
    await expect(page.locator('#fight-weight').getByText("Unable to load weight.", { exact: true })).toBeVisible();
    const attention = page.getByRole("region", { name: "Needs attention" });
    if (age < 18) {
      await expect(attention).toHaveCount(0);
    } else {
      await expect(attention.getByRole("listitem")).toHaveAttribute("data-recovery-tone", "unknown");
      await page.locator('#fight-weight > details > summary').click();
      await attention.getByRole("link", { name: /Weight status unavailable/ }).click();
      await expect(page.locator('#fight-weight > details')).toHaveAttribute("open", "");
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("old contact reports stay in sparring history while only recent concerns need attention", async ({ page, baseURL }) => {
  const report = { id: "recent", day: "2026-11-14", intensity: "technical", rounds: 3, round_seconds: 180, head_contact: "heavy", rocked: true };
  await fixture(page, baseURL!, "coach", false, {}, [{ ...fighter, share_recovery: false, share_injuries: true, share_weight: false,
    sparring_reports: [{ ...report, id: "normal", day: fighter.today, head_contact: "none", rocked: false }, report, { ...report, id: "old", day: "2026-11-01" }],
  }]);
  await page.goto(`/coach/fighters/${fighterId}`);
  const attention = page.getByRole("region", { name: "Needs attention" });
  await expect(attention.getByRole("listitem")).toHaveCount(1);
  await expect(attention).toContainText("2026-11-14");
  await expect(attention).not.toContainText("2026-11-01");
  await page.getByRole("button", { name: "Previous week", exact: true }).click();
  await page.getByRole("button", { name: "Previous week", exact: true }).click();
  await expect(page.locator('#sparring').getByText("Sun 01 Nov 2026", { exact: true })).toBeVisible();
  await expect(page.locator('#sparring').getByText(/Athlete reported being rocked/)).toBeVisible();
  await expect(attention.getByRole("listitem")).toHaveCount(1);
});

for (const width of [320, 1280]) {
  test(`team overview includes severe cut risk despite normal recovery at ${width}px`, async ({ page, baseURL }) => {
    const jayId = "00000000-0000-4000-8000-000000000004";
    await fixture(page, baseURL!, "coach", false, {}, [
      { ...fighter, name: "Michael", share_injuries: true, share_weight: false, share_recovery: false,
        injuries: [{ id: "arm", body_area: "Arm", severity: "mild", status: "open", updated_at: fighter.today }] },
      { ...fighter, id: jayId, connection_id: "jay-connection", name: "Jay", age: 25,
        recovery: [{ day: fighter.today, source: "today", sleep: "good", body: "normal" }] },
    ]);
    await page.route("**/api/coach/fighters/*/weight", route => route.fulfill({ json: { ...weight,
      cut_assessment: { ...weight.cut_assessment, weight_cut_status: "too_aggressive", weight_cut_risk_band: "severe", days_until_fight: 16 },
    } }));
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/coach");
    const attention = page.getByRole("region", { name: "Needs attention" });
    await expect(attention.getByRole("listitem")).toHaveCount(2);
    await expect(attention.getByRole("listitem").first()).toContainText("Jay");
    await expect(attention.getByRole("listitem").first()).toContainText("Cut too aggressive");
    await expect(page.getByRole("region", { name: "Roster summary" }).locator("article").filter({ hasText: "Needs attention" }).locator("strong")).toHaveText("2");
    const roster = page.locator("section").filter({ has: page.getByRole("heading", { name: "Your fighters", exact: true }) });
    const jay = roster.getByRole("link", { name: /Jay/ });
    await expect(jay.getByText("Cut too aggressive", { exact: true })).toHaveAttribute("data-recovery-tone", "concern");
    await expect(jay.getByText("Recovery normal", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `/tmp/coach-team-risk-${width}.png`, fullPage: true });
    await jay.click();
    await expect(page.getByRole("region", { name: "Needs attention" })).toContainText("Cut too aggressive · 16 days to fight");
  });
}

test("team weight checks show pending and unavailable states, and never request unshared or minor assessments", async ({ page, baseURL }) => {
  const base = { ...fighter, share_recovery: false, share_injuries: false, age: 25 };
  await fixture(page, baseURL!, "coach", false, {}, [base,
    { ...base, id: "00000000-0000-4000-8000-000000000005", name: "Private", share_weight: false },
    { ...base, id: "00000000-0000-4000-8000-000000000006", name: "Junior", age: 17 },
  ]);
  const requests: string[] = [];
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/coach/fighters/*/weight", async route => {
    requests.push(new URL(route.request().url()).pathname);
    await gate;
    await route.fulfill({ status: 503, json: { detail: "Unavailable" } });
  });
  await page.goto("/coach");
  const metric = page.getByRole("region", { name: "Roster summary" }).locator("article").filter({ hasText: "Needs attention" }).locator("strong");
  await expect(metric).toHaveText("—");
  await expect(page.getByText("Checking weight…", { exact: true })).toBeVisible();
  release();
  await expect(metric).toHaveText("1");
  await expect(page.getByRole("region", { name: "Needs attention" }).getByText("Weight status unavailable", { exact: true })).toHaveAttribute("data-recovery-tone", "unknown");
  expect(requests).toEqual([`/api/coach/fighters/${fighterId}/weight`]);
});

test("one weigh-in shows weight summary without an invented trend", async ({ page, baseURL }) => {
  await fixture(page, baseURL!);
  await page.route("**/api/coach/fighters/*/weight", route => route.fulfill({ json: { ...weight, entries: [weight.entries[1]] } }));
  await page.goto(`/coach/fighters/${fighterId}`);
  const section = page.locator('#fight-weight');
  await expect(section.getByRole("listitem")).toHaveCount(1);
  await expect(section.getByRole("slider")).toHaveCount(0);
  await expect(section.getByText("Current", { exact: true })).toBeVisible();
  await expect(section.getByText("73.6 kg", { exact: true }).first()).toBeVisible();
});

for (const width of [390, 1440]) {
  test(`coach draft to fighter consent and shared progress at ${width}px`, async ({ page, baseURL }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", error => pageErrors.push(error.message));
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, baseURL!);
    const rows: import('../lib/api-schema.generated').CoachPlan[] = [];
    let activations = 0;
    await page.route('**/api/coach/fighters/*/plans**', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.pathname.endsWith('/assign')) {
        rows[0] = { ...rows[0], assignment_status: 'pending', assigned_at: '2026-11-15T12:00:00Z', coach_name: 'Test Coach' };
        return route.fulfill({ json: rows[0] });
      }
      if (request.method() === 'POST') {
        const body = request.postDataJSON();
        rows.push({ ...body, id: url.searchParams.get('request_id'), fighter_id: fighterId, kind: 'self_directed', incomplete: false,
          assignment_status: 'draft', assigned_at: null, coach_name: null, published_plan_id: null,
          created_at: '2026-11-15T12:00:00Z', updated_at: '2026-11-15T12:00:00Z' });
        return route.fulfill({ status: 201, json: rows[0] });
      }
      return route.fulfill({ json: rows });
    });
    await page.goto('/coach/plans');
    await page.getByRole('button', { name: 'Create New Plan', exact: true }).click();
    await page.getByRole("group", { name: "Plan view" }).getByRole("button", { name: "Week", exact: true }).click();
    await expect(page.getByRole('combobox', { name: 'Fighter', exact: true })).toBeDisabled();
    await page.getByLabel('Plan name', { exact: true }).fill('Coach technical week');
    await page.getByRole('button', { name: /^Monday,/ }).click();
    await page.getByRole('button', { name: 'Add session on Monday', exact: true }).click();
    const session = page.getByRole('dialog', { name: 'New session', exact: true });
    await session.getByRole('button', { name: 'Light technical / skill', exact: true }).click();
    await session.getByRole('button', { name: 'Add session', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Save & view plan' })).toHaveCount(0);
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await page.getByRole('button', { name: 'View saved plans', exact: true }).click();
    await expect(page.getByText('Draft', { exact: true })).toBeVisible();
    expect(activations).toBe(0);
    await page.getByRole('button', { name: 'Assign to fighter', exact: true }).click();
    await expect(page.getByText('Pending', { exact: true })).toBeVisible();
    expect(activations).toBe(0);
    await setup(page, baseURL!, 'athlete');
    await page.route('**/api/custom-plans/assignments**', async route => {
      if (route.request().method() === 'POST') {
        activations++;
        rows[0] = { ...rows[0], assignment_status: 'active', published_plan_id: 'test-plan' };
        return route.fulfill({ json: { plan_id: 'test-plan' } });
      }
      return route.fulfill({ json: rows });
    });
    await page.goto('/plans');
    const assigned = page.getByRole('region', { name: 'Coach assigned plans' });
    await expect(assigned).toContainText('Test Coach');
    await expect(assigned.getByRole('button', { name: 'Activate plan' })).toHaveCount(0);
    await assigned.getByRole('button', { name: 'Preview Plan' }).click();
    await expect(assigned).toContainText('Monday');
    await assigned.getByRole('button', { name: 'Activate plan' }).click();
    await expect(assigned.getByText('Active', { exact: true })).toBeVisible();
    expect(activations).toBe(1);
    const a11y = await new AxeBuilder({ page }).include('[aria-label="Coach assigned plans"]').analyze();
    expect(a11y.violations).toEqual([]);
    await fixture(page, baseURL!);
    await page.route('**/api/coach/fighters/*/plans**', route => route.fulfill({ json: rows }));
    await page.route('**/api/coach/fighters/*/training/plan**', route => route.fulfill({ json: [
      { id: 'strength', plan_id: 'test-plan', day: '2026-11-09', title: 'Strength', type: 'strength_power', blocks: [] },
      { id: 'spar', plan_id: 'test-plan', day: '2026-11-15', title: 'Technical', type: 'skill', blocks: [] },
    ] }));
    await page.goto('/coach/plans');
    await expect(page.getByRole('progressbar', { name: 'Weekly completion' })).toHaveAttribute('value', '100');
    await expect(page.getByText('All sessions complete this week', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: width >= 760 ? 'View Fighter Progress' : 'Progress', exact: true })).toHaveAttribute('href', `/coach/fighters/${fighterId}#training-history`);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const dashboard = page.getByRole('region', { name: 'Fighter plans dashboard' });
    if (width >= 760) {
      const main = await page.locator('main').boundingBox();
      const card = await page.getByRole('article', { name: 'Coach technical week', exact: true }).boundingBox();
      expect(card!.width / main!.width).toBeGreaterThan(0.9);
      const meter = await page.getByRole('progressbar').boundingBox();
      const stats = await dashboard.locator('dl').boundingBox();
      expect(stats!.x).toBeGreaterThan(meter!.x + meter!.width);
      await page.getByRole('button', { name: 'View Schedule', exact: true }).click();
      await expect(page.getByRole('region', { name: 'Coach technical week schedule' })).toContainText('Technical');
      await page.getByRole('button', { name: 'View Schedule', exact: true }).click();
    }
    await expect(dashboard.getByText('100%', { exact: true })).toBeVisible();
    await expect(dashboard.getByText('Latest: 15 Nov · Done · RPE 6', { exact: true })).toBeVisible();
    const dashboardA11y = await new AxeBuilder({ page }).include('[aria-label="Fighter plans dashboard"]').analyze();
    expect(dashboardA11y.violations).toEqual([]);
    expect(pageErrors).toEqual([]);
    await page.mouse.move(0, 0);
    await page.screenshot({ path: `test-results/coach-plan-dashboard-${width}.png`, fullPage: true });
    await dashboard.screenshot({ path: `test-results/coach-plan-dashboard-card-${width}.png` });

    let scheduleAvailable = false;
    await page.route('**/api/coach/fighters/*/training/plan**', route => scheduleAvailable
      ? route.fallback()
      : route.fulfill({ status: 503, json: { detail: "Training schedule unavailable. Please retry." } }));
    await page.getByRole('button', { name: 'Refresh plans' }).click();
    const alert = dashboard.getByRole('alert');
    await expect(alert).toContainText('Progress unavailable.');
    await expect(dashboard.getByRole('progressbar')).toHaveCount(0);
    await expect(dashboard.getByText('Loading progress…')).toHaveCount(0);
    scheduleAvailable = true;
    await alert.getByRole('button', { name: 'Retry' }).click();
    await expect(dashboard.getByRole('progressbar')).toHaveAttribute('value', '100');
    await expect(alert).toHaveCount(0);
  });
}

test('desktop plan filters keep pending assignments accessible and separate drafts from past plans', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page, baseURL!);
  const rows = ['active', 'pending', 'draft', 'declined'].map((status, index) => ({
    id: `filter-plan-${index}`, name: `${status} plan`, kind: 'self_directed', sessions: [], incomplete: false,
    fighter_id: fighterId, assignment_status: status, coach_name: 'Test Coach',
    assigned_at: status === 'draft' ? null : '2026-11-15T12:00:00Z',
    published_plan_id: status === 'active' ? 'test-plan' : null,
    created_at: '2026-11-15T12:00:00Z', updated_at: '2026-11-15T12:00:00Z',
  }));
  await page.route('**/api/coach/fighters/*/plans**', route => route.fulfill({ json: rows }));
  await page.goto('/coach/plans');
  const filters = page.getByRole('group', { name: 'Filter plans' });
  await expect(page.getByRole('article', { name: 'pending plan', exact: true })).toBeVisible();
  await filters.getByRole('button', { name: 'Active (1)', exact: true }).click();
  await expect(page.getByRole('article', { name: 'active plan', exact: true })).toBeVisible();
  await expect(page.getByRole('article')).toHaveCount(1);
  await filters.getByRole('button', { name: 'Drafts (1)', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Edit draft', exact: true })).toBeVisible();
  await expect(page.getByRole('article')).toHaveCount(1);
  await filters.getByRole('button', { name: 'Past (1)', exact: true }).click();
  await expect(page.getByRole('article', { name: 'declined plan', exact: true })).toBeVisible();
  await filters.getByRole('button', { name: 'All Plans', exact: true }).click();
  await expect(page.getByRole('article')).toHaveCount(4);
  await expect(filters.getByRole('button', { name: 'All Plans', exact: true })).toHaveAttribute('aria-pressed', 'true');
});

test('a coach sends an assigned plan to other fighters as independent copies', async ({ page, baseURL }) => {
  const jayId = "00000000-0000-4000-8000-000000000004", kaiId = "00000000-0000-4000-8000-000000000005";
  await page.setViewportSize({ width: 390, height: 1000 });
  await fixture(page, baseURL!, "coach", false, {}, [
    { ...fighter, name: "Michael" },
    { ...fighter, id: jayId, connection_id: "jay-connection", name: "Jay" },
    { ...fighter, id: kaiId, connection_id: "kai-connection", name: "Kai" },
  ]);
  const source = { id: 'source-plan', name: 'Fight camp', kind: 'self_directed', incomplete: false, fighter_id: fighterId, assignment_status: 'pending',
    coach_name: 'Test Coach', assigned_at: '2026-11-15T12:00:00Z', published_plan_id: null, created_at: '2026-11-15T12:00:00Z', updated_at: '2026-11-15T12:00:00Z',
    schedule_mode: 'weekly', start_date: null, end_date: null,
    sessions: [{ id: 'session-a', name: 'Strength', weekday: 'Monday', scheduled_date: null, session_type: 'strength_power', exercises: [] }] };
  const created: { fighter: string; body: { name: string; sessions: { id: string }[] } }[] = [];
  const assigned: string[] = [];
  await page.route('**/api/coach/fighters/*/plans**', route => {
    const url = new URL(route.request().url()), target = url.pathname.split('/')[4];
    if (url.pathname.endsWith('/assign')) { assigned.push(target); return route.fulfill({ json: { ...source, id: `copy-${target}`, fighter_id: target } }); }
    if (route.request().method() === 'POST') {
      created.push({ fighter: target, body: route.request().postDataJSON() });
      return route.fulfill({ status: 201, json: { ...source, id: `copy-${target}`, fighter_id: target, assignment_status: 'draft' } });
    }
    return route.fulfill({ json: target === fighterId ? [source] : [] });
  });
  await page.goto('/coach/plans');
  const card = page.getByRole('article', { name: 'Fight camp', exact: true });
  await card.getByRole('button', { name: 'Send to other fighters', exact: true }).click();
  const panel = card.getByRole('group', { name: 'Send a copy to' });
  await expect(panel.getByLabel('Michael')).toHaveCount(0);
  await panel.getByLabel('Jay', { exact: true }).check();
  await panel.getByLabel('Kai', { exact: true }).check();
  await panel.getByRole('button', { name: 'Send to 2', exact: true }).click();
  await expect(card.getByRole('status')).toHaveText('Assigned to Jay, Kai.');
  expect(created.map(row => row.fighter).sort()).toEqual([jayId, kaiId]);
  expect(assigned.sort()).toEqual([jayId, kaiId]);
  expect(created.every(row => row.body.name === 'Fight camp' && row.body.sessions[0].id !== 'session-a')).toBe(true);
  const a11y = await new AxeBuilder({ page }).include('[aria-label="Fight camp"]').analyze();
  expect(a11y.violations).toEqual([]);
});

test('a coach deletes an unsent draft after confirming', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 1000 });
  await fixture(page, baseURL!);
  const base = { kind: 'self_directed', incomplete: false, fighter_id: fighterId, coach_name: 'Test Coach', published_plan_id: null,
    created_at: '2026-11-15T12:00:00Z', updated_at: '2026-11-15T12:00:00Z', sessions: [] };
  const rows = [{ ...base, id: 'draft-plan', name: 'Old draft', assignment_status: 'draft', assigned_at: null },
    { ...base, id: 'sent-plan', name: 'Sent plan', assignment_status: 'pending', assigned_at: '2026-11-15T12:00:00Z' }];
  const deleted: string[] = [];
  await page.route('**/api/coach/fighters/*/plans**', route => {
    if (route.request().method() === 'DELETE') { deleted.push(new URL(route.request().url()).pathname.split('/').pop()!); return route.fulfill({ status: 204 }); }
    return route.fulfill({ json: rows });
  });
  await page.goto('/coach/plans');
  await expect(page.getByRole('article', { name: 'Sent plan' }).getByRole('button', { name: 'Delete draft' })).toHaveCount(0);
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('article', { name: 'Old draft' }).getByRole('button', { name: 'Delete draft', exact: true }).click();
  await expect(page.getByRole('article', { name: 'Old draft' })).toBeVisible();
  expect(deleted).toEqual([]);
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('article', { name: 'Old draft' }).getByRole('button', { name: 'Delete draft', exact: true }).click();
  await expect(page.getByRole('article', { name: 'Old draft' })).toHaveCount(0);
  expect(deleted).toEqual(['draft-plan']);
});

for (const width of [390, 1440]) {
  test(`assigned schedule uses the builder calendar without editing at ${width}px`, async ({ page, baseURL }) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setViewportSize({ width: 1440, height: 1000 });
    await fixture(page, baseURL!);
    await page.route('**/api/coach/fighters/*/plans**', route => route.fulfill({ json: [{
      id: 'calendar-plan', name: 'Fight camp', kind: 'self_directed', sessions: [
        { id: 'off-17', name: 'Off day', scheduled_date: '2026-11-17', session_type: 'rest', exercises: [] },
        { id: 'recovery-19', name: 'Recovery day', scheduled_date: '2026-11-19', session_type: 'recovery', exercises: [] },
      ], incomplete: false,
      schedule_mode: 'dated', start_date: '2026-11-16', end_date: '2026-12-06',
      assignment_status: 'active', published_plan_id: 'test-plan', fighter_id: fighterId,
      created_at: '2026-11-16T12:00:00Z', updated_at: '2026-11-16T12:00:00Z',
    }] }));
    await page.route('**/api/coach/fighters/*/training/plan**', route => route.fulfill({ json: [
      { id: 'spar', plan_id: 'test-plan', day: '2026-11-16', title: 'Hard Sparring', type: 'sparring', blocks: [] },
      { id: 'spar', plan_id: 'test-plan', day: '2026-11-23', title: 'Light Technical', type: 'skill', blocks: [] },
      { id: 'strength', plan_id: 'test-plan', day: '2026-12-01', title: 'Strength & Power', type: 'strength_power', blocks: [] },
      { id: 'other', plan_id: 'another-plan', day: '2026-11-16', title: 'Unrelated session', type: 'skill', blocks: [] },
    ] }));
    await page.goto('/coach/plans');
    await expect(page).toHaveURL(/\/coach\/plans$/);
    await page.getByRole('button', { name: 'View Schedule', exact: true }).click();
    await page.setViewportSize({ width, height: 1000 });
    const schedule = page.getByRole('region', { name: 'Fight camp schedule', exact: true });
    const calendar = schedule.getByRole('region', { name: 'Plan calendar', exact: true });
    await expect(calendar).toBeVisible();
    await expect(calendar.getByText('Fight day · Sat 12 Dec 2026')).toBeVisible();
    await expect(calendar.getByRole('button', { name: 'Select Tue 17 Nov 2026' })).toHaveCount(0);
    await expect(calendar.locator('[data-rest="true"]')).toContainText('Off');
    await expect(calendar).toContainText('Recovery');
    await expect(calendar).toContainText('No session planned');
    await expect(calendar.locator('[data-rest="true"]')).toHaveCount(1);
    await expect(calendar.getByRole('button', { name: 'Week', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(calendar.getByRole('button', { name: 'View Hard Sparring on Mon 16 Nov 2026', exact: true })).toBeVisible();
    await expect(calendar).not.toContainText('Unrelated session');
    await expect(calendar.getByRole('button', { name: /Add session|Copy|Move|Duplicate|Paste/ })).toHaveCount(0);
    await expect(calendar.locator('[draggable="true"]')).toHaveCount(0);
    await calendar.getByRole('button', { name: 'Next calendar period', exact: true }).click();
    await expect(calendar.getByRole('button', { name: 'View Light Technical on Mon 23 Nov 2026', exact: true })).toBeVisible();
    await calendar.getByRole('button', { name: 'View Light Technical on Mon 23 Nov 2026', exact: true }).click();
    await expect(calendar.getByRole('list', { name: 'Sessions on selected day' })).toContainText('Light Technical');
    await calendar.getByRole('button', { name: 'Month', exact: true }).click();
    await expect(calendar.getByRole('button', { name: 'Month', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await calendar.getByRole('button', { name: 'Next calendar period', exact: true }).click();
    await calendar.getByRole('button', { name: 'View Strength & Power on Tue 01 Dec 2026', exact: true }).click();
    await expect(calendar.locator('[data-fight-day="true"]')).toHaveCount(1);
    await expect(calendar.locator('[data-fight-day="true"]').getByText('Fight day')).toBeVisible();
    await expect(calendar.locator('[data-fight-day="true"]').getByRole('button')).toHaveCount(0);
    await expect(calendar.locator('[data-outside="true"][data-rest="true"]')).toHaveCount(0);
    await expect(calendar.getByRole('list', { name: 'Sessions on selected day' })).toContainText('Strength & Power');
    await expect(calendar.getByRole('button', { name: 'Next calendar period', exact: true })).toBeDisabled();
    await calendar.getByRole('button', { name: 'Week', exact: true }).click();
    await calendar.getByRole('button', { name: 'Next calendar period', exact: true }).click();
    await expect(calendar.locator('[data-fight-day="true"]')).toBeVisible();
    await expect(calendar.getByRole('button', { name: 'Next calendar period', exact: true })).toBeDisabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const a11y = await new AxeBuilder({ page }).include('[aria-label="Fight camp schedule"]').analyze();
    expect(a11y.violations).toEqual([]);
    expect(errors).toEqual([]);
    await calendar.screenshot({ path: `/tmp/assigned-schedule-${width}.png` });
  });
}

test('a coach retracts a pending plan and clears answered ones, but not the active plan', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 1400 });
  await fixture(page, baseURL!);
  const base = { kind: 'self_directed', incomplete: false, fighter_id: fighterId, coach_name: 'Test Coach', published_plan_id: null,
    created_at: '2026-11-15T12:00:00Z', updated_at: '2026-11-15T12:00:00Z', assigned_at: '2026-11-15T12:00:00Z', sessions: [] };
  let rows = [{ ...base, id: 'sent-plan', name: 'Sent plan', assignment_status: 'pending' },
    { ...base, id: 'declined-plan', name: 'Declined plan', assignment_status: 'declined' },
    { ...base, id: 'old-plan', name: 'Old plan', assignment_status: 'superseded', published_plan_id: 'old-published' },
    { ...base, id: 'live-plan', name: 'Live plan', assignment_status: 'active', published_plan_id: 'test-plan' }];
  const calls: string[] = [];
  await page.route('**/api/coach/fighters/*/plans**', route => {
    const url = new URL(route.request().url()), method = route.request().method();
    if (url.pathname.endsWith('/retract')) {
      calls.push('retract sent-plan');
      rows = rows.map(row => row.id === 'sent-plan' ? { ...row, assignment_status: 'draft', assigned_at: null as unknown as string } : row);
      return route.fulfill({ json: rows.find(row => row.id === 'sent-plan') });
    }
    if (method === 'DELETE') { calls.push(`delete ${url.pathname.split('/').pop()}`); return route.fulfill({ status: 204 }); }
    return route.fulfill({ json: rows });
  });
  await page.goto('/coach/plans');
  // The plan the fighter is training on offers neither.
  const live = page.getByRole('article', { name: 'Live plan' });
  await expect(live).toBeVisible();
  await expect(live.getByRole('button', { name: /Retract|Remove from list|Delete/ })).toHaveCount(0);
  // Dismissing the confirmation changes nothing.
  const sent = page.getByRole('article', { name: 'Sent plan' });
  page.once('dialog', dialog => dialog.dismiss());
  await sent.getByRole('button', { name: 'Retract plan', exact: true }).click();
  expect(calls).toEqual([]);
  page.once('dialog', dialog => { expect(dialog.message()).toContain('goes back to your drafts'); return dialog.accept(); });
  await sent.getByRole('button', { name: 'Retract plan', exact: true }).click();
  // Retracted: it is a draft again, editable and deletable.
  await expect(sent.getByRole('button', { name: 'Edit draft', exact: true })).toBeVisible();
  await expect(sent.getByRole('button', { name: 'Delete draft', exact: true })).toBeVisible();
  await expect(sent.getByRole('button', { name: 'Retract plan' })).toHaveCount(0);
  for (const name of ['Declined plan', 'Old plan']) {
    page.once('dialog', dialog => { expect(dialog.message()).toContain('keeps their own copy'); return dialog.accept(); });
    await page.getByRole('article', { name }).getByRole('button', { name: 'Remove from list', exact: true }).click();
    await expect(page.getByRole('article', { name })).toHaveCount(0);
  }
  expect(calls).toEqual(['retract sent-plan', 'delete declined-plan', 'delete old-plan']);
  await expect(live).toBeVisible();
});

for (const width of [390, 1440]) {
  test(`upcoming fights open the fighter's calendar in place (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 1200 });
    await fixture(page, baseURL!);
    await page.goto('/coach');
    const fights = page.locator('section', { has: page.getByRole('heading', { name: 'Upcoming fights' }) });
    await expect(fights).toBeVisible();
    await expect(fights.getByRole('group', { name: 'Training calendar' })).toHaveCount(0);
    const toggle = fights.getByRole('button', { name: "Show Test Fighter's calendar", exact: true });
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await toggle.click();
    await expect(fights.getByRole('group', { name: 'Training calendar' })).toBeVisible();
    // Still on the overview, and the row still links to the fighter.
    await expect(page).toHaveURL(/\/coach$/);
    await expect(fights.getByRole('link', { name: /Test Fighter/ })).toHaveAttribute('href', `/coach/fighters/${fighterId}`);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const a11y = await new AxeBuilder({ page }).include('section:has(h2:text-is("Upcoming fights"))').analyze().catch(() => null);
    if (a11y) expect(a11y.violations).toEqual([]);
    if (process.env.COACH_SCREENSHOTS) await page.screenshot({ path: `${process.env.COACH_SCREENSHOTS}/fight-calendar-${width}.png`, fullPage: true });
    await fights.getByRole('button', { name: "Hide Test Fighter's calendar", exact: true }).click();
    await expect(fights.getByRole('group', { name: 'Training calendar' })).toHaveCount(0);
  });
}

test("photos and voice notes are shared only when the athlete turns them on, and can be stopped", async ({ page, baseURL }) => {
  const state = await fixture(page, baseURL!, "athlete", false, {
    status: "accepted", share_recovery: true, share_injuries: true, share_weight: true,
    allow_session_logging: true, share_media: false, health_consent: true,
  });
  await page.goto("/settings/coach-access");
  await expect(page.getByText("Not sharing photos and voice notes")).toBeVisible();
  await page.getByRole("button", { name: "Share photos & voice notes" }).click();
  await expect(page.getByText("Sharing photos and voice notes", { exact: true })).toBeVisible();
  expect(state.actions[0]).toMatchObject({ p_action: "consent", p_media: true, p_recovery: true, p_injuries: true,
    p_weight: true, p_logging: true });
  await page.getByRole("button", { name: "Stop", exact: true }).click();
  await expect(page.getByRole("button", { name: "Share photos & voice notes" })).toBeVisible();
  expect(state.actions[1]).toMatchObject({ p_action: "consent", p_media: false, p_recovery: true, p_injuries: true });
});

// A fighter's shared media on their page: progress photos beside the latest
// sessions with files, where the coach can reply with a voice note or a PDF.
const mediaFile = (id: string, kind: "photo" | "voice" | "pdf", author: "athlete" | "coach" = "athlete") => ({
  id, created_at: "2026-11-15T10:00:00Z", kind, author, removable: author === "coach", media: null,
  file: { url: null, width: kind === "photo" ? 900 : null, height: kind === "photo" ? 1200 : null,
    duration_ms: kind === "voice" ? 14000 : null, name: kind === "pdf" ? "Fight week meal plan.pdf" : null,
    pages: kind === "pdf" ? 4 : null, size_bytes: kind === "pdf" ? 820_000 : null },
});
async function mediaRoutes(page: Page, share: boolean) {
  const lists: string[] = [];
  await page.route("**/api/attachments/settings", route => route.fulfill({ json: { photos_available: true, voice_available: true, pdfs_available: true } }));
  await page.route(`**/api/coach/fighters/${fighterId}/media`, route => route.fulfill({ json: share ? {
    share_media: true, progress_count: 3,
    recent: [{ source: "external", target_id: strengthSession.id, exercise_ref: "", last_at: "2026-11-15T10:00:00Z" },
      { source: "sparring", target_id: "spar-1", exercise_ref: "", last_at: "2026-11-12T19:00:00Z" }],
  } : { share_media: false, progress_count: 0, recent: [] } }));
  await page.route(`**/api/coach/fighters/${fighterId}/training/attachments?**`, route => {
    const params = new URL(route.request().url()).searchParams;
    lists.push(`${params.get("source")}:${params.get("target_id")}`);
    const source = params.get("source");
    return route.fulfill({ json: source === "progress" ? [mediaFile("p1", "photo"), mediaFile("p2", "photo"), mediaFile("p3", "photo")]
      : source === "sparring" ? [mediaFile("v1", "voice")] : [mediaFile("s1", "photo"), mediaFile("d1", "pdf", "coach")] });
  });
  return lists;
}

for (const width of [320, 390, 1280]) {
  test(`fighter media: progress photos and recent session files at ${width}px`, async ({ page, baseURL }) => {
    await fixture(page, baseURL!, "coach", false, {}, [{ ...fighter,
      sparring_reports: [{ id: "spar-1", day: "2026-11-12", rounds: 5, intensity: "hard", round_seconds: 180, head_contact: "none", rocked: false }] }]);
    const lists = await mediaRoutes(page, true);
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`/coach/fighters/${fighterId}`);
    const media = page.locator("#media");
    await expect(media.getByRole("heading", { name: "Photos and voice notes" })).toBeVisible();
    const progress = media.getByRole("region", { name: "Progress photos" });
    await expect(progress.getByRole("button", { name: /Open photo/ })).toHaveCount(3);
    await expect(progress.getByText("3 of 24")).toBeVisible();
    // Coaches never add progress photos.
    await expect(progress.getByRole("button", { name: /Attach/ })).toHaveCount(0);
    const recent = media.getByRole("region", { name: "Recent session files" });
    await expect(recent.getByRole("heading", { name: /Sparring · / })).toBeVisible();
    await expect(recent.getByText("Fight week meal plan.pdf")).toBeVisible();
    // Sessions are browse-only here. Sparring reports retain their reply control.
    await expect(recent.getByRole("button", { name: "Attach", exact: true })).toHaveCount(1);
    await recent.getByRole("button", { name: "Attach", exact: true }).first().click();
    await expect(recent.getByRole("menuitem")).toHaveText(["Voice note", "PDF"]);
    await page.keyboard.press("Escape");
    expect(lists.sort()).toEqual([`external:${strengthSession.id}`, `progress:${fighterId}`, "sparring:spar-1"]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    // Headings never wrap onto a second line or get cut short.
    for (const heading of await media.locator("h3").all()) {
      expect(await heading.evaluate(node => node.getBoundingClientRect().height < 30 && node.scrollWidth <= node.clientWidth)).toBe(true);
    }
    if (width >= 900) {
      const [left, right] = [await progress.boundingBox(), await recent.boundingBox()];
      expect(Math.abs(left!.y - right!.y)).toBeLessThan(4);
      expect(right!.x).toBeGreaterThan(left!.x + left!.width);
    }
    if (process.env.COACH_SCREENSHOTS) {
      await media.scrollIntoViewIfNeeded();
      await media.screenshot({ path: `${process.env.COACH_SCREENSHOTS}/fighter-media-${width}.png` });
    }
  });
}

test("fighter media stays closed when the fighter doesn't share it", async ({ page, baseURL }) => {
  await fixture(page, baseURL!);
  const lists = await mediaRoutes(page, false);
  await page.goto(`/coach/fighters/${fighterId}`);
  const media = page.locator("#media");
  await expect(media.getByText(/Photos, voice notes and PDFs: Not shared/)).toBeVisible();
  await expect(media.getByRole("button")).toHaveCount(0);
  expect(lists).toEqual([]);
});

test("check-in nudge: one switch turns it on, days and time save only when changed", async ({ page, baseURL }) => {
  await fixture(page, baseURL!, "coach", false, {}, [fighter]);
  const saves: Record<string, unknown>[] = [];
  await page.route("**/rest/v1/coach_checkin_nudges?**", route => route.fulfill({ json: [] }));
  await page.route("**/rest/v1/rpc/coach_set_checkin_nudge", route => {
    const body = route.request().postDataJSON();
    saves.push(body);
    return route.fulfill({ json: { days: body.p_days, local_time: `${body.p_time}:00`, enabled: body.p_enabled, last_sent_day: null } });
  });
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto(`/coach/fighters/${fighterId}`);
  const nudge = page.getByRole("region", { name: "Contact and check-ins" });
  const toggle = nudge.getByRole("switch", { name: /Remind .* to check in/ });
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  // No summary line repeating the setting under the title.
  await expect(nudge.getByText(/their time/)).toHaveCount(0);
  await expect(nudge.getByRole("group", { name: "Nudge days" })).toHaveCount(0);
  if (process.env.COACH_SCREENSHOTS) await nudge.screenshot({ path: `${process.env.COACH_SCREENSHOTS}/nudge-off.png` });

  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await expect(nudge.getByRole("group", { name: "Nudge days" })).toBeVisible();
  expect(saves).toEqual([expect.objectContaining({ p_enabled: true, p_days: 0b0011111, p_time: "08:00" })]);
  await expect(nudge.getByRole("button", { name: "Save" })).toHaveCount(0);

  await nudge.getByRole("button", { name: "Sat" }).click();
  await nudge.getByRole("button", { name: "Save" }).click();
  await expect(nudge.getByRole("status")).toHaveText("Saved");
  expect(saves[1]).toMatchObject({ p_enabled: true, p_days: 0b0111111 });
  if (process.env.COACH_SCREENSHOTS) await nudge.screenshot({ path: `${process.env.COACH_SCREENSHOTS}/nudge-on.png` });

  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  expect(saves[2]).toMatchObject({ p_enabled: false });
});
