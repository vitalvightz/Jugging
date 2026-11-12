import { expect, test, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import path from "node:path";
import fixtures from "./fixtures/rehab-today.json";
import bankFixtures from "./fixtures/rehab-bank-publication.json";
import progressionFixtures from "./fixtures/rehab-progression.json";
import expansionFixtures from "./fixtures/rehab-restore-expansion.json";
import { isolateFromNetwork } from "./support";

// Synthetic payloads exported from GET /api/today, not hand-authored eligibility.
const cases = { chest: fixtures.chest, achilles: fixtures.achilles, ...bankFixtures, ...progressionFixtures, ...expansionFixtures };
async function setup(page: Page, baseURL: string, name: keyof typeof cases, videoId?: string, readinessHold = false) {
  await isolateFromNetwork(page, baseURL);
  const { view, profile: sourceProfile } = structuredClone(cases[name]);
  // Presentation regression: change today's schedule only, preserving the
  // API-exported LOAD eligibility and entry decision.
  if (readinessHold) Object.assign(view.open_injuries[0].rehab_decision.schedule, {
    state: "held", reason: "Complete today's check-in and follow its training guidance.", next_due_day: null,
  });
  const profile = { ...sourceProfile, profile: { ...sourceProfile.profile, private_trial_ack_at: "2026-10-05T12:00:00Z" } };
  const now = new Date(`${view.today.training_day}T12:05:00Z`);
  await page.clock.setFixedTime(now);
  const key = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [key, JSON.stringify({
    access_token: "athlete-token", refresh_token: "fixture", token_type: "bearer", expires_at: now.getTime() / 1000 + 86400,
    user: { id: profile.profile.athlete_id, email: "fixture@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  const block = view.live_prescription.session.blocks[0];
  if (videoId) Object.assign(view.exercise_media, { [`exercise:${block.exercise_key}`]: {
    provider: "youtube", video_id: videoId, start_s: 0, source: "curated",
  } });
  const writes: Record<string, unknown>[] = [];
  await page.route("**/api/**", async route => {
    const request = route.request(); const url = new URL(request.url());
    const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    if (url.pathname === "/api/me") return json(profile);
    if (url.pathname === "/api/today") return json(view);
    if (url.pathname === `/api/plans/${view.active_plan.id}`) return json({
      plan_id: view.active_plan.id, status: "ready", created_at: "2026-10-05T12:00:00Z", outputs: {
        structured_plan: { weeks: [{ week_index: 1, phase_label: "GPP", days: [{ date: view.today.training_day, weekday: "Mon", sessions: [view.live_prescription.session] }] }] },
      },
    });
    if (url.pathname === "/api/today/session-completion") {
      const payload = request.postDataJSON(); writes.push(payload);
      view.today.completion_status = payload.status;
      Object.assign(view.live_prescription.session, { completion_status: payload.status });
      if (payload.status === "done") view.open_injuries[0].rehab_decision.schedule.state = "already_completed";
      return json({ completion: { ...payload, training_day: view.today.training_day }, completion_status: payload.status,
        landing_session_state: "session_done", rehab_response_prompts: [] }, 201);
    }
    if (url.pathname === "/api/training/history") return json({ sessions: [] });
    if (/session-completions|sparring-logs|exercise-logs|pending/.test(url.pathname)) return json({ completions: [], logs: [], response_sets: [] });
    if (url.pathname === "/api/plans") return json([]);
    return json({});
  });
  const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
  await page.goto("/today");
  await expect(page.getByRole("button", { name: block.display_name, exact: false }).first()).toBeVisible();
  return { block, view, writes, errors };
}

for (const [name, message] of [
  ["earned_load", "You meet the checks for this LOAD exercise."],
  ["missing_permission", "Do today’s rehab, then tap how it feels"],
  ["pending_response", "Do today’s rehab, then tap how it feels"],
  ["worsening", "Your injury got worse"],
  ["missing_equipment", "This needs equipment you haven’t listed"],
] as const) {
  test(`actual Today decision explains ${name} at 375px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width: 375, height: 844 });
    const { errors } = await setup(page, baseURL!, name);
    const summary = page.locator(".today-injury-summary-row");
    await expect(summary).toContainText(message);
    if (name === "earned_load") {
      await expect(summary).toContainText("DYNAMIC exercises aren’t available");
      await expect(summary).not.toContainText(/Complete your|Confirm what/);
    } else {
      await expect(summary).not.toContainText(/LOAD follows|meet the checks/);
    }
    if (name === "worsening") {
      await expect(summary).toContainText("Stage: Calm");
      await expect(summary).not.toContainText(/Future availability varies|Complete your|Confirm what/);
    }
    await summary.click();
    if (name === "worsening") {
      await expect(page.locator(".injury-progress")).toHaveCount(0);
    } else {
      await expect(page.locator(".injury-progress")).not.toContainText(message);
      await page.locator("button.injury-progress").click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await expect(page.getByRole("dialog").locator("form, input, select, textarea")).toHaveCount(0);
      if (name === "earned_load") {
        await expect(page.getByRole("dialog")).toContainText("DYNAMIC exercises aren’t available");
        await expect(page.getByRole("dialog").locator(".injury-requirements")).toHaveCount(0);
      }
      await page.keyboard.press("Escape");
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await shot(page, `progression-${name}-375.png`);
    await expect(page).toHaveURL(/\/today$/);
    await expect(page).not.toHaveTitle("");
    expect(errors).toEqual([]);
  });
}

test("a readiness schedule hold keeps LOAD eligibility at 375px", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 375, height: 844 });
  const { errors } = await setup(page, baseURL!, "earned_load", undefined, true);
  const summary = page.locator(".today-injury-summary-row");
  await expect(summary).toContainText("Stage: Load");
  await expect(summary).not.toContainText("Progression is on hold");
  await summary.click();
  await expect(page.locator(".injury-stage-entry")).toContainText("You meet the checks for this LOAD exercise.");
  await expect(page.locator(".today-injury-care")).toContainText("Today’s rehab on hold");
  await expect(page.locator(".today-injury-care")).toContainText("Complete today's check-in");
  await expect(page.locator(".injury-progress")).toHaveAttribute("aria-label", /DYNAMIC exercises aren’t available/);
  await expect(page.locator(".today-injury-care")).not.toContainText("Progression · On hold");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await shot(page, "readiness-schedule-hold-375.png");
  expect(errors).toEqual([]);
});

async function shot(page: Page, filename: string) {
  const dir = process.env.REHAB_QA_DIR;
  if (dir) { mkdirSync(dir, { recursive: true }); await page.screenshot({ path: path.join(dir, filename), fullPage: false, animations: "disabled" }); }
}

for (const width of [375, 1280]) {
  test(`chest RESTORE explains unpublished LOAD without impossible unlock instructions at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const { block, errors } = await setup(page, baseURL!, "chest");
    await expect(page.locator(".today-injury-summary-row")).toBeVisible();
    await expect(page.locator(".today-injury-summary-row")).toContainText("LOAD exercises aren’t available for this injury in UNLXCK yet.");
    await page.getByRole("button", { name: block.display_name, exact: false }).first().click();
    const detail = page.getByRole("dialog");
    await expect(detail).toContainText("Go at your own pace");
    await expect(detail).toContainText("No equipment needed");
    await expect(detail).toContainText("How to perform");
    await expect(detail).not.toContainText("Demo not assigned");
    await expect(detail.locator("iframe, .sp-block-stats")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await shot(page, `after-chest-exercise-${width}.png`);
    await page.keyboard.press("Escape");
    await page.locator(".today-injury-summary-row").click();
    await page.locator("button.injury-progress").click();
    await expect(page.getByRole("dialog")).toContainText("LOAD exercises aren’t available");
    await expect(page.getByRole("dialog")).not.toContainText(/Complete your|Confirm what|unlock|Closed/);
    await shot(page, `after-chest-progression-${width}.png`);
    expect(errors).toEqual([]);
    await expect(page).toHaveURL(/\/today$/);
    await expect(page).not.toHaveTitle("");
  });
}

test("published Achilles LOAD uses the shared embed and completes with the existing payload on mobile", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 375, height: 844 });
  const { block, view, writes, errors } = await setup(page, baseURL!, "achilles", "AAAAAAAAAAA");
  // Deterministic IFrame API stand-in: no unrelated real video is fetched.
  await page.evaluate(() => {
    (window as unknown as { YT: unknown }).YT = { PlayerState: { PLAYING: 1, PAUSED: 2, ENDED: 0 },
      Player: class {
        constructor(target: HTMLElement, options: { host: string; videoId: string; events: { onReady: (e: unknown) => void; onStateChange: (e: unknown) => void; onError: () => void } }) {
          const frame = document.createElement("iframe"); frame.title = "Rehab demo fixture";
          frame.dataset.videoId = options.videoId; frame.dataset.host = options.host; target.replaceWith(frame);
          Object.assign(window, { rehabDemoError: options.events.onError });
          queueMicrotask(() => { options.events.onReady({ target: this }); options.events.onStateChange({ target: this, data: 1 }); });
        }
        playVideo() {} destroy() {} getCurrentTime() { return 0; } seekTo() {} setPlaybackRate() {} mute() {} unMute() {}
      },
    };
  });
  await page.getByRole("button", { name: block.display_name, exact: false }).first().click();
  const detail = page.getByRole("dialog");
  // The plan's shared demo leads the same guide used without a video.
  await expect(detail.locator(".rehab-guide-dose")).toContainText("Reps10");
  await expect(detail.locator(".rehab-guide-dose")).toContainText("Sets1");
  await expect(detail.locator(".rehab-guide-how ol li").first()).toBeVisible();
  expect(await detail.evaluate(element => Boolean(element.querySelector(".rehab-guide-demo")!
    .compareDocumentPosition(element.querySelector(".rehab-guide-dose")!) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
  await expect(detail).toContainText("Bodyweight only");
  await expect(detail).toContainText("Floor level only");
  await expect(detail).toContainText("does not provide training or contact clearance");
  await detail.evaluate(element => { element.scrollTop = 0; });
  await shot(page, "after-achilles-prescription-375.png");
  await detail.getByRole("button", { name: `Play ${block.display_name} demo` }).click();
  await expect(detail.locator("iframe")).toHaveAttribute("data-video-id", "AAAAAAAAAAA");
  await expect(detail.locator("iframe")).toHaveAttribute("data-host", "https://www.youtube-nocookie.com");
  await detail.evaluate(element => { element.scrollTop = 0; });
  await shot(page, "after-achilles-demo-375.png");
  await page.evaluate(() => (window as unknown as { rehabDemoError: () => void }).rehabDemoError());
  await expect(detail).toContainText("Video unavailable here. Use the cues below.");
  await expect(detail.getByRole("link", { name: "Watch on YouTube" })).toHaveAttribute("href", "https://www.youtube.com/watch?v=AAAAAAAAAAA");
  await expect(detail).toContainText("Floor level only");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.keyboard.press("Escape");
  await page.getByRole("group", { name: "Did you complete your rehab?" }).getByRole("button", { name: "Completed", exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toMatchObject({ status: "done", rehab_tracking: "injury_checkin", rehab_performance: "done_as_shown",
    prescription_revision: view.live_prescription.revision, session_id: view.live_prescription.session.session_id });
  await expect(page.getByRole("group", { name: "Did you complete your rehab?" })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("an invalid demo leaves the actual Today exercise usable", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  const { block, errors } = await setup(page, baseURL!, "achilles", "invalid");
  await page.getByRole("button", { name: block.display_name, exact: false }).first().click();
  await expect(page.getByRole("dialog")).not.toContainText("Demo not assigned");
  await expect(page.getByRole("dialog")).toContainText("How to perform");
  await expect(page.getByRole("dialog").locator("iframe, .ex-demo")).toHaveCount(0);
  await expect(page.getByRole("dialog").locator(".rehab-guide-dose")).toContainText("Reps10");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

// Source-dosed RESTORE options exported from GET /api/today: dose shown, not self-paced.
for (const name of Object.keys(expansionFixtures) as (keyof typeof expansionFixtures)[]) {
  test(`${name}: dosed RESTORE option shows its approved amount and completes once at 320px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width: 320, height: 844 });
    const { block, view, writes, errors } = await setup(page, baseURL!, name);
    await page.getByRole("button", { name: block.display_name, exact: false }).first().click();
    const detail = page.getByRole("dialog");
    await expect(detail).toContainText(block.display_name);
    await expect(detail.getByRole("region", { name: "How to perform" }).locator("ol li").first()).toBeVisible();
    await expect(detail).not.toContainText("Self-paced");
    const dose = block.dose as { reps?: number; duration_seconds?: number };
    if (dose.reps) await expect(detail).toContainText(String(dose.reps));
    if (dose.duration_seconds) await expect(detail).toContainText(`${dose.duration_seconds}s`);
    await expect(detail).toContainText(/Stop if|Seek/);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await detail.evaluate(element => { element.scrollTop = 0; });
    await shot(page, `expansion-${name}-320.png`);
    await page.keyboard.press("Escape");
    await page.getByRole("group", { name: "Did you complete your rehab?" }).getByRole("button", { name: "Completed", exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0]).toMatchObject({ status: "done", rehab_tracking: "injury_checkin", rehab_performance: "done_as_shown",
      prescription_revision: view.live_prescription.revision, session_id: view.live_prescription.session.session_id });
    expect(errors).toEqual([]);
  });
}

for (const name of Object.keys(bankFixtures) as (keyof typeof bankFixtures)[]) {
  test(`${name}: bank option uses existing exercise UI and one completion at 320px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width: 320, height: 844 });
    const { block, view, writes, errors } = await setup(page, baseURL!, name);
    await page.getByRole("button", { name: block.display_name, exact: false }).first().click();
    const detail = page.getByRole("dialog");
    await expect(detail).toContainText(block.display_name);
    await expect(detail).not.toContainText("Demo not assigned");
    await expect(detail.getByRole("region", { name: "How to perform" }).locator("ol li").first()).toBeVisible();
    if (name === "achilles_tendonitis") {
      await expect(detail).toContainText("10");
      await expect(detail).toContainText("Bodyweight only");
      await expect(detail).toContainText("10 seconds");
    } else {
      await expect(detail).toContainText("Self-paced");
      await expect(detail.locator(".sp-block-stats")).toHaveCount(0);
    }
    await expect(detail).toContainText(/Stop if|Stop for/);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await detail.evaluate(element => { element.scrollTop = 0; });
    await shot(page, `bank-${name}-320.png`);
    await page.keyboard.press("Escape");
    await page.getByRole("group", { name: "Did you complete your rehab?" }).getByRole("button", { name: "Completed", exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0]).toMatchObject({ status: "done", rehab_tracking: "injury_checkin", rehab_performance: "done_as_shown",
      prescription_revision: view.live_prescription.revision, session_id: view.live_prescription.session.session_id });
    expect(errors).toEqual([]);
  });
}

for (const viewport of [{ width: 375, height: 844 }, { width: 668, height: 488 }, { width: 1280, height: 900 }]) {
  test(`text guide keeps prescription before steps and all limits accessible at ${viewport.width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize(viewport);
    const { block, errors } = await setup(page, baseURL!, "achilles_tendonitis");
    await page.getByRole("button", { name: block.display_name, exact: false }).first().click();
    const detail = page.getByRole("dialog");
    await expect(detail.locator(".rehab-guide-dose")).toHaveText(["Hold10sReps10FrequencyDaily"]);
    await expect(detail.locator(".rehab-guide-how ol li")).toHaveCount(3);
    expect(await detail.evaluate(element => {
      const dose = element.querySelector(".rehab-guide-dose")!;
      const steps = element.querySelector(".rehab-guide-how")!;
      const safety = element.querySelector(".rehab-guide-safety")!;
      return Boolean(dose.compareDocumentPosition(steps) & Node.DOCUMENT_POSITION_FOLLOWING)
        && Boolean(steps.compareDocumentPosition(safety) & Node.DOCUMENT_POSITION_FOLLOWING);
    })).toBe(true);
    await expect(detail).not.toContainText(/Demo not assigned|Full prescription|Cue/);
    await shot(page, `guide-achilles-${viewport.width}.png`);
    const lastLimit = detail.locator(".rehab-guide-safety li").last();
    await lastLimit.scrollIntoViewIfNeeded();
    await expect(lastLimit).toBeVisible();
    await expect(detail).toContainText("No added resistance, step, heel drop, forced stretch or automatic increase.");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.keyboard.press("Escape");
    await expect(detail).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}


for (const width of [375, 1280]) {
  test(`rehab access links straight to the prescribed exercise at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const { block, errors } = await setup(page, baseURL!, "chest");
    await page.evaluate(() => window.scrollTo(0, 0));
    await shot(page, `${process.env.REHAB_QA_LABEL ?? "after"}-rehab-access-${width}.png`);
    const access = page.getByRole("navigation", { name: "Your rehab", exact: true });
    await expect(access).toContainText(block.display_name);
    await access.getByRole("link", { name: /View rehab/ }).click();
    const row = page.locator(`[id="rehab-exercise-${block.block_id}"]`);
    await expect(row).toBeInViewport();
    await row.getByRole("button", { name: block.display_name, exact: false }).click();
    for (const sentence of block.instructions.split(/(?<=\.)\s+/)) {
      await expect(page.getByRole("dialog")).toContainText(sentence);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}

test("rehab access never offers held exercises", async ({ page, baseURL }) => {
  await setup(page, baseURL!, "achilles", undefined, true);
  await expect(page.getByRole("navigation", { name: "Your rehab", exact: true })).toHaveCount(0);
});

for (const width of [320, 390, 1280]) {
  test(`injury discovery keeps clearance visible and explains stages at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const { errors } = await setup(page, baseURL!, "chest");
    const injury = page.locator(".today-injury-item").first();
    await injury.scrollIntoViewIfNeeded();
    await expect(injury.locator(".today-injury-summary-row")).toHaveAttribute("aria-expanded", "false");
    const clearance = injury.getByRole("button", { name: /^(Change|Report) clearance$/ });
    await expect(clearance).toBeVisible();
    await expect(page.locator(".injury-discovery-hint")).toContainText("what your clinician says you can do");
    await shot(page, `injury-clearance-visible-${width}.png`);
    await clearance.click();
    const sheet = page.getByRole("dialog", { name: "Clearance & restrictions", exact: true });
    await expect(sheet).toBeVisible();
    await expect(sheet).toContainText("The rehab exercises your clinician allows.");
    await expect(sheet).toContainText("Whether your clinician allows training and contact.");
    await expect(sheet).toContainText("Restrictions and safety holds still apply.");
    await expect(page.locator(".injury-discovery-hint")).toHaveCount(0);
    await sheet.getByRole("button", { name: "Close", exact: true }).click();
    await expect(sheet).toHaveCount(0);
    await injury.locator(".today-injury-summary-row").click();
    const guide = injury.locator(".injury-stage-guide");
    await guide.locator("summary").click();
    await expect(guide).toContainText("Comfortable movement.");
    await expect(guide).toContainText("Stages are not clearance to train.");
    expect(await injury.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    await shot(page, `injury-stage-guide-${width}.png`);
    await page.getByRole("button", { name: "+ Add another injury", exact: true }).click();
    const add = page.getByRole("dialog", { name: "Add another injury", exact: true });
    await expect(add).toContainText("Your answers help set today’s rehab and training limits.");
    await add.getByRole("button", { name: "Close", exact: true }).click();
    await expect(add).toHaveCount(0);
    await page.reload();
    await expect(page.locator(".injury-discovery-hint")).toHaveCount(0);
    await expect(page.locator(".today-injury-item").first().getByRole("button", { name: /^(Change|Report) clearance$/ })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}
