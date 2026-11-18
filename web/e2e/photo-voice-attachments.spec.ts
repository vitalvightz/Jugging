import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { isolateFromNetwork } from "./support";

// Photos and voice notes on a logged session: nothing loads from the list, a
// photo opens through a one-off link, voice notes are rows that play on press,
// and a recorded voice note goes Record → Stop → Preview → Send.
const day = "2026-10-19";
const athleteId = "00000000-0000-4000-8000-000000000001";
const storage = (process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").replace(/\/$/, "");
const session = { id: "manual", source: "external", title: "Boxing", activity: "boxing", duration_minutes: 40, effort: "hard", status: "done", training_day: day };
const row = (id: string, kind: "photo" | "voice", author: "athlete" | "coach" = "athlete") => ({
  id, created_at: `${day}T10:00:00Z`, kind, author, removable: true, media: null,
  file: { url: null, width: kind === "photo" ? 1200 : null, height: kind === "photo" ? 900 : null, duration_ms: kind === "voice" ? 14_000 : null },
});

// A small striped JPEG-like test image (an SVG served as the signed photo).
const PHOTO_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="900"><rect width="1200" height="900" fill="#2b2f36"/>
  <rect x="380" y="180" width="440" height="540" rx="40" fill="#c9a27a"/><rect x="380" y="430" width="440" height="80" fill="#e9e4dc"/></svg>`;

async function setup(page: Page, baseURL: string) {
  await isolateFromNetwork(page, baseURL);
  const now = new Date(`${day}T12:00:00Z`);
  await page.clock.setFixedTime(now);
  const key = `sb-${new URL(storage).hostname.split(".")[0]}-auth-token`;
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [key, JSON.stringify({
    access_token: "attachments-test", refresh_token: "test", token_type: "bearer", expires_at: now.getTime() / 1000 + 86400,
    user: { id: athleteId, email: "fighter@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  let rows = [row("p1", "photo"), row("p2", "photo"), row("v1", "voice", "coach")];
  const calls: string[] = [];
  await page.route(`${storage}/storage/v1/object/sign/**`, route => route.fulfill({ contentType: "image/svg+xml", body: PHOTO_SVG }));
  await page.route("**/api/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    calls.push(`${request.method()} ${path}`);
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/me") return json({ profile: { athlete_id: athleteId, email: "fighter@example.com", full_name: "Carlos", role: "athlete", access_status: "approved", technical_style: ["boxing"], tactical_style: [], athlete_timezone: "Europe/London", athlete_locale: "en", appearance_mode: "dark", nutrition_profile: {}, date_of_birth: "1995-01-01", meets_minimum_age: true, is_minor: false, terms_accepted: true, health_consent_granted: true, private_trial_ack_at: `${day}T12:00:00Z` }, plan_count: 0, latest_plan: null, latest_intake: null, username_rate_limit: {} });
    if (path === "/api/training/history") return json({ sessions: [session] });
    if (path.startsWith("/api/training/history/")) return json({ session, athlete_id: athleteId, prescription_source: "manual", blocks: [] });
    if (path === "/api/private-media/settings") return json({ uploads_available: false, youtube_available: false });
    if (path === "/api/attachments/settings") return json({ photos_available: true, voice_available: true });
    if (path.endsWith("/play")) {
      const id = path.split("/").at(-2);
      const found = rows.find(item => item.id === id)!;
      return json({ ...found, file: { ...found.file, url: `${storage}/storage/v1/object/sign/attachment-ready/${id}?token=t` } });
    }
    if (path === "/api/training/attachments" && request.method() === "GET") return json(rows);
    if (request.method() === "DELETE") { rows = rows.filter(item => !path.endsWith(item.id)); return route.fulfill({ status: 204 }); }
    if (path === "/api/plans/active") return json({ detail: "No active plan" }, 404);
    if (path === "/api/plans") return json([]);
    return json({ notifications: [], unread_count: 0, level: 1, total_xp: 0, logs: [] });
  });
  return { calls };
}

for (const width of [320, 390, 1280]) {
  test(`photos and voice notes on a session at ${width}px`, async ({ page, baseURL }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const { calls } = await setup(page, baseURL!);
    const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
    const media: string[] = [];
    page.on("request", request => { if (request.url().includes("/storage/v1/")) media.push(request.url()); });
    await page.goto("/history");
    await page.getByRole("button", { name: /View Boxing details/ }).click();
    const dialog = page.getByRole("dialog", { name: "Boxing" });
    await expect(dialog.getByRole("button", { name: "Open photo 1 for Boxing" })).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Play voice note 1 from coach for Boxing" })).toContainText("0:14");
    // Nothing is fetched from storage until someone asks.
    expect(media).toEqual([]);
    expect(calls.filter(call => call.endsWith("/play"))).toEqual([]);
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`${width}-1-list.png`) });

    await dialog.getByRole("button", { name: "Attach", exact: true }).click();
    await expect(dialog.getByRole("menuitem")).toHaveText(["Photo", "Voice note"]);
    await page.screenshot({ path: info.outputPath(`${width}-2-menu.png`) });
    await dialog.getByRole("button", { name: "Attach", exact: true }).click();
    await expect(dialog.getByRole("menuitem")).toHaveCount(0);

    await dialog.getByRole("button", { name: "Open photo 1 for Boxing" }).click();
    const photo = dialog.getByRole("img", { name: "Photo 1 for Boxing" });
    await expect(photo).toBeVisible();
    await expect.poll(() => photo.evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
    expect(calls.filter(call => call.endsWith("/play"))).toEqual(["GET /api/training/attachments/p1/play"]);
    const box = await photo.boundingBox();
    expect(box!.height).toBeLessThanOrEqual(900 * 0.7 + 1);
    await photo.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath(`${width}-3-photo.png`) });
    expect((await new AxeBuilder({ page }).include("dialog").analyze()).violations
      .filter(v => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);

    await dialog.getByRole("button", { name: "Remove photo" }).click();
    await expect(dialog.getByRole("button", { name: /Open photo/ })).toHaveCount(1);
    await expect(dialog).toContainText("2 of 6");
    expect(errors).toEqual([]);
  });
}

test("a voice note is recorded, previewed and sent only when the athlete presses Send", async ({ page, baseURL, browserName }, info) => {
  test.skip(browserName !== "chromium", "uses Chromium's fake microphone");
  await page.setViewportSize({ width: 390, height: 900 });
  const { calls } = await setup(page, baseURL!);
  // A fake microphone: an oscillator stream in place of getUserMedia.
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext();
      const tone = context.createOscillator();
      const out = context.createMediaStreamDestination();
      tone.connect(out); tone.start();
      return out.stream;
    };
  });
  await page.goto("/history");
  await page.getByRole("button", { name: /View Boxing details/ }).click();
  const dialog = page.getByRole("dialog", { name: "Boxing" });
  await dialog.getByRole("button", { name: "Attach", exact: true }).click();
  await dialog.getByRole("menuitem", { name: "Voice note" }).click();
  await expect(dialog.getByRole("timer")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Stop" })).toBeVisible();
  await page.screenshot({ path: info.outputPath("390-4-recording.png") });
  await page.clock.runFor(1500);
  await page.waitForTimeout(1200);
  await dialog.getByRole("button", { name: "Stop" }).click();
  await expect(dialog.getByLabel("Preview your voice note")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Send" })).toBeVisible();
  await page.screenshot({ path: info.outputPath("390-5-preview.png") });
  // Nothing is uploaded while previewing.
  expect(calls.filter(call => call.includes("/api/attachments/uploads"))).toEqual([]);
  await dialog.getByRole("button", { name: "Discard" }).click();
  await expect(dialog.getByRole("button", { name: "Attach", exact: true })).toBeVisible();
  expect(calls.filter(call => call.includes("/api/attachments/uploads"))).toEqual([]);
});
