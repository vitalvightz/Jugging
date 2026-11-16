import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { setup } from "./custom-plan-fixture";
import type { Connection, Fighter } from "../lib/coach";
const fighterId = "00000000-0000-4000-8000-000000000003";
// Isolated test responses only. Never written to production.
const connection: Connection = {
  id: "test-connection",
  coach_id: "coach-test",
  athlete_id: fighterId,
  invited_email: "fighter@example.invalid",
  coach_name: "Test Coach",
  status: "accepted",
  share_recovery: true,
  share_injuries: false,
  created_at: "2026-11-15T12:00:00Z",
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
  ],
  injuries: null,
};
async function fixture(
  page: Page,
  baseURL: string,
  role = "coach",
  empty = false,
) {
  await setup(page, baseURL, role);
  let rows = empty ? [] : [fighter];
  let connections: Connection[] = empty
    ? []
    : [
        {
          ...connection,
          status: role === "coach" ? "accepted" : "pending",
          share_recovery: role === "coach",
          share_injuries: false,
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
      if (["accept", "consent"].includes(body.p_action))
        connections = connections.map((c) => ({
          ...c,
          status: "accepted",
          share_recovery: body.p_recovery,
          share_injuries: body.p_injuries,
        }));
      return route.fulfill({ json: "test-connection" });
    }
    return route.fulfill({
      json:
        name === "coach_connections_list"
          ? connections
          : rows.map((f) => ({
              ...f,
              schedule: body.p_athlete_id ? f.schedule : [],
            })),
    });
  });
  return {
    actions,
    revoke: () => {
      rows = [];
      connections = [];
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
      page.getByText("2 sessions this week", { exact: false }),
    ).toBeVisible();
    await expect(
      page
        .getByRole("link", { name: /Test Fighter.*sessions this week/ })
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
    await page.getByRole("searchbox").fill("does not exist");
    await expect(
      page.getByText("No fighters match this search."),
    ).toBeVisible();
    await page.getByRole("searchbox").fill("Test Fighter");
    await page
      .getByRole("link", { name: /Test Fighter.*sessions this week/ })
      .click();
    await expect(
      page.getByRole("heading", { name: "Fighter overview" }),
    ).toBeVisible();
    await expect(
      page.getByText("1 of 1 scheduled sessions completed"),
    ).toBeVisible();
    await page.getByRole("button", { name: "Previous week" }).click();
    await expect(
      page.getByText("No sessions recorded this week."),
    ).toBeVisible();
    await page.getByRole("button", { name: "Next week" }).click();
    await expect(
      page.getByText("1 of 1 scheduled sessions completed"),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Sparring & S&C" }),
    ).toBeVisible();
    await page.screenshot({
      path: info.outputPath(`coach-fighter-compact-${width}.png`),
      fullPage: true,
    });
    await page.getByText("Check-ins & sharing", { exact: true }).click();
    await expect(
      page.getByText("Injury information is not shared."),
    ).toBeVisible();
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
  await page.getByText("Manage connections", { exact: true }).click();
  await page
    .getByRole("textbox", { name: "Athlete email" })
    .fill("fighter@example.invalid");
  await page.getByRole("button", { name: "Invite athlete" }).click();
  await expect(page.getByText(/Invitation saved/)).toBeVisible();
  expect(state.actions[0].p_action).toBe("invite");
});
test("athlete accepts, changes consent, then revokes", async ({
  page,
  baseURL,
}, info) => {
  const state = await fixture(page, baseURL!, "athlete");
  await page.setViewportSize({ width: 390, height: 1000 });
  await page.goto("/settings/coach-access");
  await expect(
    page.getByRole("button", { name: "Accept & share training" }),
  ).toBeVisible();
  await expect(page.getByRole("checkbox").first()).not.toBeChecked();
  await expect(page.getByRole("checkbox").last()).not.toBeChecked();
  await page.screenshot({
    path: info.outputPath("coach-consent-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("checkbox").first().check();
  await page.getByRole("button", { name: "Accept & share training" }).click();
  await expect(
    page.getByRole("button", { name: "Revoke all access" }),
  ).toBeVisible();
  expect(state.actions[0]).toMatchObject({
    p_action: "accept",
    p_recovery: true,
    p_injuries: false,
  });
  await page.getByRole("checkbox").first().uncheck();
  await page.getByRole("button", { name: "Save sharing" }).click();
  await expect(
    page.getByRole("button", { name: "Save sharing" }),
  ).toBeDisabled();
  expect(state.actions[1]).toMatchObject({
    p_action: "consent",
    p_recovery: false,
    p_injuries: false,
  });
  await page.getByRole("button", { name: "Revoke all access" }).click();
  await expect(page.getByText("No coach requests yet.")).toBeVisible();
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
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
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
