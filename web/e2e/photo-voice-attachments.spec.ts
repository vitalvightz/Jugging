import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { isolateFromNetwork } from "./support";

// Photos and voice notes on a logged session: nothing loads from the list, a
// photo opens full screen through a one-off link, voice notes are chat-style
// cards with a waveform that load and play only on press,
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

// A 6-second 8 kHz mono WAV whose loudness rises and falls, so the waveform has a shape.
function testWav() {
  const rate = 8_000, samples = rate * 6;
  const data = Buffer.alloc(44 + samples * 2);
  data.write("RIFF", 0); data.writeUInt32LE(36 + samples * 2, 4); data.write("WAVEfmt ", 8);
  data.writeUInt32LE(16, 16); data.writeUInt16LE(1, 20); data.writeUInt16LE(1, 22); data.writeUInt32LE(rate, 24);
  data.writeUInt32LE(rate * 2, 28); data.writeUInt16LE(2, 32); data.writeUInt16LE(16, 34); data.write("data", 36); data.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++) {
    const loud = Math.abs(Math.sin((i / samples) * Math.PI * 3));
    data.writeInt16LE(Math.round(Math.sin(i / 8) * 20_000 * loud), 44 + i * 2);
  }
  return data;
}

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
  await page.route(`${storage}/storage/v1/object/sign/**`, route => /\/v\d\?/.test(route.request().url())
    ? route.fulfill({ contentType: "audio/wav", body: testWav() })
    : route.fulfill({ contentType: "image/svg+xml", body: PHOTO_SVG }));
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
    const play = dialog.getByRole("button", { name: "Play voice note 1 from coach for Boxing" });
    await expect(play).toBeVisible();
    const note = dialog.getByRole("listitem").filter({ has: page.getByRole("button", { name: "Play voice note 1 from coach for Boxing" }) });
    await expect(note).toContainText("Coach");
    await expect(note).toContainText("0:14");
    // A chat-style card, not the browser's audio bar.
    await expect(dialog.locator("audio[controls]")).toHaveCount(0);
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

    // The voice note loads and plays only now, and its waveform is drawn from the recording.
    await play.click();
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await dialog.getByRole("button", { name: "Pause voice note 1 from coach for Boxing" }).click();
    await expect(play).toBeVisible();
    await expect(note.getByRole("alert")).toHaveCount(0);
    await expect(note.locator("[role=slider][data-real]")).toHaveCount(1);
    await expect(note.getByRole("button", { name: /Playback speed/ })).toBeVisible();
    expect(calls.filter(call => call.endsWith("/play"))).toEqual(["GET /api/training/attachments/v1/play"]);
    await note.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath(`${width}-3-voice.png`) });

    await dialog.getByRole("button", { name: "Open photo 1 for Boxing" }).click();
    const viewer = page.getByRole("dialog", { name: "Photo 1 of 2" });
    const photo = viewer.getByRole("img", { name: "Photo 1 for Boxing" });
    await expect(photo).toBeVisible();
    await expect.poll(() => photo.evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
    expect(calls.filter(call => call.endsWith("/play"))).toEqual(["GET /api/training/attachments/v1/play", "GET /api/training/attachments/p1/play"]);
    // Fills the screen width on phones, never taller than the screen, never wider than the photo.
    const box = await photo.boundingBox();
    expect(box!.height).toBeLessThanOrEqual(900 - 150 + 1);
    expect(box!.width).toBeLessThanOrEqual(Math.min(width, 1200) + 1);
    if (width < 600) expect(box!.width).toBeGreaterThan(width - 40);
    await page.screenshot({ path: info.outputPath(`${width}-4-photo.png`) });
    expect((await new AxeBuilder({ page }).include("dialog[aria-label='Photo 1 of 2']").analyze()).violations
      .filter(v => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);

    // Next photo by arrow key (buttons on desktop, swipe on phones); Escape closes only the viewer.
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("dialog", { name: "Photo 2 of 2" }).getByRole("img", { name: "Photo 2 for Boxing" })).toBeVisible();
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog", { name: /^Photo / })).toHaveCount(0);
    await expect(dialog).toBeVisible();

    await dialog.getByRole("button", { name: "Open photo 1 for Boxing" }).click();
    await page.getByRole("dialog", { name: "Photo 1 of 2" }).getByRole("button", { name: "Remove photo" }).click();
    await expect(page.getByRole("dialog", { name: /^Photo / })).toHaveCount(0);
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

test("navigation during microphone permission closes a late stream", async ({ page, baseURL }, info) => {
  const { calls } = await setup(page, baseURL!);
  await page.setViewportSize({ width: 390, height: 900 });
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => {
    const state = window as unknown as { grantMicrophone: () => void; microphonePending: boolean; lateTracksStopped: number };
    state.lateTracksStopped = 0;
    navigator.mediaDevices.getUserMedia = () => new Promise<MediaStream>(resolve => {
      state.microphonePending = true;
      state.grantMicrophone = () => {
        const context = new AudioContext();
        const stream = context.createMediaStreamDestination().stream;
        for (const track of stream.getTracks()) {
          const stop = track.stop.bind(track);
          track.stop = () => { state.lateTracksStopped++; stop(); void context.close(); };
        }
        resolve(stream);
      };
    });
  });
  await page.goto("/history");
  await page.getByRole("button", { name: /View Boxing details/ }).click();
  const dialog = page.getByRole("dialog", { name: "Boxing" });
  await dialog.getByRole("button", { name: "Attach", exact: true }).click();
  await dialog.getByRole("menuitem", { name: "Voice note" }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { microphonePending: boolean }).microphonePending)).toBe(true);
  await dialog.getByRole("button", { name: "Close session details" }).click();
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Home", exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  await page.evaluate(() => (window as unknown as { grantMicrophone: () => void }).grantMicrophone());
  await expect.poll(() => page.evaluate(() => (window as unknown as { lateTracksStopped: number }).lateTracksStopped)).toBe(1);
  expect(calls.filter(call => call.includes("/api/attachments/uploads"))).toEqual([]);
  expect(errors).toEqual([]);
  await page.screenshot({ path: info.outputPath("late-microphone-navigation.png") });
});

// Thumbnails: real previews in the tiles, and an opened photo that appears at
// once on its preview, keeps its shape, and fades to the full photo when it lands.
const THUMB_SVG = PHOTO_SVG.replace('width="1200" height="900"', 'width="360" height="270" viewBox="0 0 1200 900"');
for (const width of [320, 390, 1280]) {
  test(`photo tiles show previews and open instantly at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 900 });
    await setup(page, baseURL!);
    let releaseFull: () => void = () => undefined;
    const fullHeld = new Promise<void>(resolve => { releaseFull = resolve; });
    await page.route(`${storage}/storage/v1/object/sign/**`, async route => {
      if (route.request().url().includes("thumb")) return route.fulfill({ contentType: "image/svg+xml", body: THUMB_SVG });
      await fullHeld;
      return route.fulfill({ contentType: "image/svg+xml", body: PHOTO_SVG });
    });
    await page.route("**/api/training/attachments?**", route => route.fulfill({ json: ["p1", "p2"].map(id => {
      const item = row(id, "photo");
      return { ...item, file: { ...item.file, thumb_url: `${storage}/storage/v1/object/sign/attachment-ready/${id}/thumb.jpg?token=t` } };
    }) }));
    await page.goto("/history");
    await page.getByRole("button", { name: /View Boxing details/ }).click();
    const dialog = page.getByRole("dialog", { name: "Boxing" });
    const tile = dialog.getByRole("button", { name: "Open photo 1 for Boxing" });
    const preview = tile.locator("img");
    await expect.poll(() => preview.evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
    // The tile is the preview itself: no grey box, no "Photo 1" label.
    await expect(tile.getByText("Photo 1")).toHaveCount(0);

    await tile.click();
    const viewer = page.getByRole("dialog", { name: "Photo 1 of 2" });
    const frame = viewer.locator("[data-loaded], div:has(> img[aria-hidden='true'])").first();
    await expect(viewer.getByRole("status").filter({ hasText: "Loading photo…" })).toBeVisible();
    const before = await frame.boundingBox();
    // Space is reserved at the photo's shape (1200 x 900) before the full photo arrives, and it fits the screen.
    expect(Math.abs(before!.width / before!.height - 4 / 3)).toBeLessThan(0.02);
    expect(before!.height).toBeLessThanOrEqual(900 - 150 + 1);
    releaseFull();
    const full = viewer.getByRole("img", { name: "Photo 1 for Boxing" });
    await expect.poll(() => full.evaluate(image => getComputedStyle(image).opacity)).toBe("1");
    await expect(viewer.getByText("Loading photo…")).toHaveCount(0);
    const after = await frame.boundingBox();
    expect(Math.abs(after!.height - before!.height)).toBeLessThan(1);
    expect(await viewer.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    if (process.env.PHOTO_SCREENSHOTS) {
      await page.screenshot({ path: `${process.env.PHOTO_SCREENSHOTS}/photo-open-${width}.png` });
      await page.keyboard.press("Escape");
      await tile.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${process.env.PHOTO_SCREENSHOTS}/photo-tiles-${width}.png` });
    }
  });
}
