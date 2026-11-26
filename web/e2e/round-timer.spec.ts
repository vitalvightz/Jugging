import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { isolateFromNetwork } from "./support";

test.beforeEach(async ({ page, baseURL }) => {
  await isolateFromNetwork(page, baseURL ?? "http://127.0.0.1:3100");
  await page.addInitScript(() => {
    // Inspect generated PCM without fetching a blob (connect-src stays strict).
    const blobs = new Map<string, Blob>();
    const createUrl = URL.createObjectURL.bind(URL);
    const revokeUrl = URL.revokeObjectURL.bind(URL);
    URL.createObjectURL = (blob) => {
      const url = createUrl(blob);
      if (blob instanceof Blob) blobs.set(url, blob);
      return url;
    };
    URL.revokeObjectURL = (url) => { blobs.delete(url); revokeUrl(url); };
    Object.assign(window, { __roundTimerAudioBlobs: blobs });
    // Seed once: reload must exercise the saved run, not overwrite it.
    if (localStorage.getItem("round-timer-e2e")) return;
    localStorage.setItem("round-timer-e2e", "true");
    localStorage.setItem("unlxck.session-timer.free-format", JSON.stringify({ rounds: 3, workSec: 5, restSec: 2 }));
    localStorage.setItem("unlxck.session-timer.settings", JSON.stringify({ prepSec: 0, voice: false }));
  });
});

test("round media carries the bells while page JavaScript is paused, then the UI catches up", async ({ page, context }, testInfo) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const response = await page.goto("/timer");
  expect(response?.headers()["content-security-policy"]).toContain("media-src 'self' blob:");
  await expect(page).toHaveTitle(/UNLXCK/i);
  await page.getByRole("button", { name: "Start round 1", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Round audio playing" })).toBeVisible();
  const media = page.locator("audio[data-round-audio]");
  await expect(media).toHaveCount(1);
  const before = await media.evaluate((audio: HTMLAudioElement) => audio.currentTime);

  // Verify actual rendered PCM contains bell energy at the engine's deadlines.
  const waveform = await media.evaluate(async (audio: HTMLAudioElement) => {
    const blobs = (window as unknown as { __roundTimerAudioBlobs: Map<string, Blob> }).__roundTimerAudioBlobs;
    const pcm = new DataView(await blobs.get(audio.src)!.arrayBuffer());
    const energy = (seconds: number) => {
      let sum = 0;
      for (let i = 0; i < 1600; i += 1) sum += Math.abs(pcm.getInt16(44 + (Math.round(seconds * 16000) + i) * 2, true));
      return sum;
    };
    return { duration: audio.duration, silence: energy(1), endBell: energy(5.05), startBell: energy(7.05), finish: energy(19.05) };
  });
  expect(waveform.duration).toBeCloseTo(23, 1);
  expect(waveform.silence).toBe(0);
  expect(waveform.endBell).toBeGreaterThan(1000);
  expect(waveform.startBell).toBeGreaterThan(1000);
  expect(waveform.finish).toBeGreaterThan(1000);
  await page.screenshot({ path: testInfo.outputPath("round-timer-desktop.png") });

  const cdp = await context.newCDPSession(page);
  await cdp.send("Debugger.enable");
  await cdp.send("Debugger.pause");
  try {
    // Node's timer runs outside the browser. The page cannot trigger any bells.
    await new Promise((resolve) => setTimeout(resolve, 9000));
  } finally {
    await cdp.send("Debugger.resume");
    await cdp.send("Debugger.disable");
  }
  const after = await media.evaluate((audio: HTMLAudioElement) => ({ time: audio.currentTime, paused: audio.paused }));
  expect(after.paused).toBe(false);
  expect(after.time - before).toBeGreaterThan(8);
  await expect(page.locator(".st-clock-caption")).toHaveText("Round 2 / 3");
  await expect(page.locator(".st-done-title")).toHaveText("Session complete", { timeout: 15_000 });
  // The final bell's decay must survive the engine finishing the run.
  await expect(media).toHaveCount(1);
  await expect(media).toHaveCount(0, { timeout: 7000 });
  expect(errors).toEqual([]);
});

test("pause, resume, edits and interrupted playback keep one correctly aligned track", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/timer");
  await page.getByRole("button", { name: "Start round 1", exact: true }).click();
  await expect(page.locator(".st-audio-status")).toContainText("Round audio playing");
  const media = page.locator("audio[data-round-audio]");
  await page.getByRole("button", { name: "Pause timer", exact: true }).click();
  // Keep one silent paused element for Safari Now Playing's Resume control.
  await expect(media).toHaveCount(1);
  expect(await media.evaluate((audio: HTMLAudioElement) => audio.paused)).toBe(true);
  const paused = await page.locator(".st-clock-digits").textContent();
  await page.waitForTimeout(1100);
  await expect(page.locator(".st-clock-digits")).toHaveText(paused!);
  await page.getByRole("button", { name: "Resume timer", exact: true }).click();
  await expect(page.locator(".st-audio-status")).toContainText("Round audio playing");
  const duration = await media.evaluate((audio: HTMLAudioElement) => audio.duration);
  await page.getByRole("button", { name: "Add 10 seconds", exact: true }).click();
  await expect(page.locator(".st-audio-status")).toContainText("Round audio playing");
  await expect(media).toHaveCount(1);
  expect(await media.evaluate((audio: HTMLAudioElement) => audio.duration)).toBeGreaterThan(duration + 8);
  // Safari's native media Pause must stop the actual round, not just mute bells.
  await media.evaluate((audio: HTMLAudioElement) => audio.pause());
  await expect(page.getByRole("button", { name: "Resume timer", exact: true })).toBeVisible();
  const nativePausedClock = await page.locator(".st-clock-digits").textContent();
  await page.waitForTimeout(1100);
  await expect(page.locator(".st-clock-digits")).toHaveText(nativePausedClock!);
  await expect(media).toHaveCount(1, { timeout: 2000 });
  const pausedBytes = await media.evaluate((audio: HTMLAudioElement) => {
    const blobs = (window as unknown as { __roundTimerAudioBlobs: Map<string, Blob> }).__roundTimerAudioBlobs;
    return blobs.get(audio.src)!.size;
  });
  expect(pausedBytes).toBeLessThan(2_000_000);
  await page.getByRole("button", { name: "Resume timer", exact: true }).click();
  await expect(page.locator(".st-audio-status")).toContainText("Round audio playing");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("round-timer-mobile.png") });
  await page.getByRole("button", { name: "Minimise timer", exact: true }).click();
  await expect(page.locator(".st-mini")).toBeVisible();
  await expect(media).toHaveCount(1);
  await page.goto("/timer");
  await expect(page.locator(".st-root")).toBeVisible();
  // A reload either restores permitted playback or exposes a gesture to enable it.
  await expect(page.locator(".st-audio-status")).toHaveText(/Round audio playing|Round audio is unavailable/);
});


test("Safari Now Playing pause and play actions freeze and resume the real round", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("unlxck.session-timer.free-format", JSON.stringify({ rounds: 3, workSec: 30, restSec: 10 }));
    const session = navigator.mediaSession;
    const nativeHandler = session.setActionHandler.bind(session);
    const actions: Record<string, MediaSessionActionHandler | null> = {};
    Object.assign(window, { __unlxckMediaActions: actions });
    session.setActionHandler = (action, handler) => {
      if (action === "pause" || action === "play") actions[action] = handler;
      return nativeHandler(action, handler);
    };
  });
  await page.goto("/timer");
  await page.getByRole("button", { name: "Start round 1", exact: true }).click();
  await expect(page.locator(".st-audio-status")).toContainText("Round audio playing");
  const action = (name: "pause" | "play") => page.evaluate((actionName) => {
    const actions = (window as unknown as { __unlxckMediaActions: Record<string, MediaSessionActionHandler | null> }).__unlxckMediaActions;
    const callback = actions[actionName];
    if (!callback) throw new Error("Missing Safari media action " + actionName);
    callback({ action: actionName });
  }, name);
  await action("pause");
  await expect(page.getByRole("button", { name: "Resume timer", exact: true })).toBeVisible();
  const time = await page.locator(".st-clock-digits").textContent();
  await page.waitForTimeout(1100);
  await expect(page.locator(".st-clock-digits")).toHaveText(time!);
  await expect(page.locator("audio[data-round-audio]")).toHaveCount(1);
  await action("play");
  await expect(page.getByRole("button", { name: "Pause timer", exact: true })).toBeVisible();
  await expect(page.locator(".st-audio-status")).toContainText("Round audio playing");
});

for (const replacementOnly of [false, true]) {
test(`denied ${replacementOnly ? "replacement" : "all"} media playback exposes the foreground fallback and releases its track`, async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(({ replacementOnly }) => {
    const play = HTMLMediaElement.prototype.play;
    const calls = new WeakMap<HTMLMediaElement, number>();
    HTMLMediaElement.prototype.play = function () {
      const count = (calls.get(this) ?? 0) + 1;
      calls.set(this, count);
      return !replacementOnly || count > 1
        ? Promise.reject(new DOMException("Playback denied for test", "NotAllowedError"))
        : play.call(this);
    };
  }, { replacementOnly });
  await page.goto("/timer");
  await page.getByRole("button", { name: "Start round 1", exact: true }).click();
  await expect(page.locator(".st-audio-status")).toContainText("Round audio is unavailable. Keep this screen open.");
  await expect(page.getByRole("button", { name: "Enable round audio", exact: true })).toBeVisible();
  await expect(page.locator("audio[data-round-audio]")).toHaveCount(0);
  await expect(page.locator(".st-clock-digits")).not.toHaveText("0:05");
  expect(errors).toEqual([]);
});
}

test("waking within a round does not synthesise an old warning over the media track", async ({ page, context }) => {
  await page.addInitScript(() => {
    localStorage.setItem("unlxck.session-timer.free-format", JSON.stringify({ rounds: 1, workSec: 30, restSec: 0 }));
    const createOscillator = AudioContext.prototype.createOscillator;
    let oscillators = 0;
    AudioContext.prototype.createOscillator = function () { oscillators += 1; return createOscillator.call(this); };
    Object.defineProperty(window, "__liveTimerOscillators", { get: () => oscillators });
  });
  await page.goto("/timer");
  await page.getByRole("button", { name: "Start", exact: true }).click();
  await expect(page.locator(".st-audio-status")).toContainText("Round audio playing");
  // Restore 18 seconds into the same round: its warning is due two seconds later.
  await page.evaluate(() => {
    const key = "unlxck.session-timer.free";
    const run = JSON.parse(localStorage.getItem(key)!);
    run.state.phaseStartedAt = Date.now() - 18_000;
    localStorage.setItem(key, JSON.stringify(run));
  });
  await page.reload();
  await expect(page.locator(".st-root")).toBeVisible();
  await expect(page.locator(".st-audio-status")).toHaveText(/Round audio playing|Round audio is unavailable/);
  if (await page.getByRole("button", { name: "Enable round audio", exact: true }).isVisible()) {
    await page.getByRole("button", { name: "Enable round audio", exact: true }).click();
  }
  await expect(page.locator(".st-audio-status")).toContainText("Round audio playing");
  const before = await page.evaluate(() => (window as unknown as { __liveTimerOscillators: number }).__liveTimerOscillators);
  const cdp = await context.newCDPSession(page);
  await cdp.send("Debugger.enable");
  await cdp.send("Debugger.pause");
  try {
    await new Promise((resolve) => setTimeout(resolve, 6000));
  } finally {
    await cdp.send("Debugger.resume");
    await cdp.send("Debugger.disable");
  }
  await expect(page.locator(".st-phase")).toContainText("Work");
  expect(await page.evaluate(() => (window as unknown as { __liveTimerOscillators: number }).__liveTimerOscillators)).toBe(before);
});

test("a maximum-length track loads, seeks near its final bell, and releases replaced blobs", async ({ page, browser }, testInfo) => {
  test.setTimeout(90_000);
  await page.addInitScript(() => {
    localStorage.setItem("unlxck.session-timer.free-format", JSON.stringify({ rounds: 20, workSec: 150, restSec: 0 }));
  });
  const nativeMemory = async () => {
    if (process.env.ROUND_TIMER_PROFILE_MEMORY !== "1" || process.platform !== "win32") return null;
    const cdp = await browser.newBrowserCDPSession();
    try {
      const { processInfo } = await cdp.send("SystemInfo.getProcessInfo");
      const ids = processInfo.map(({ id }) => id).filter((id) => Number.isInteger(id) && id > 0).join(",");
      // Optional Windows profiling covers native/media allocations, not only JS heap.
      const raw = execFileSync("powershell.exe", ["-NoProfile", "-Command",
        `$timerProcesses = Get-Process -Id ${ids} -ErrorAction SilentlyContinue; [pscustomobject]@{privateBytes=($timerProcesses | Measure-Object PrivateMemorySize64 -Sum).Sum;workingSetBytes=($timerProcesses | Measure-Object WorkingSet64 -Sum).Sum} | ConvertTo-Json -Compress`], { encoding: "utf8" });
      return JSON.parse(raw) as { privateBytes: number; workingSetBytes: number };
    } finally {
      await cdp.detach();
    }
  };
  await page.goto("/timer");
  const baseline = await nativeMemory();
  const started = Date.now();
  await page.getByRole("button", { name: "Start round 1", exact: true }).click();
  await expect(page.locator(".st-audio-status")).toContainText("Round audio playing", { timeout: 30_000 });
  const buildToPlayingMs = Date.now() - started;
  const loaded = await nativeMemory();
  const facts = await page.locator("audio[data-round-audio]").evaluate(async (audio: HTMLAudioElement) => {
    const blobs = (window as unknown as { __roundTimerAudioBlobs: Map<string, Blob> }).__roundTimerAudioBlobs;
    const blob = blobs.get(audio.src)!;
    // Read a small slice: the audit itself must not copy the full 96 MB WAV.
    const pcm = new DataView(await blob.slice(44 + 3000 * 16000 * 2, 44 + 3001 * 16000 * 2).arrayBuffer());
    let finishEnergy = 0;
    for (let i = 0; i < pcm.byteLength; i += 2) finishEnergy += Math.abs(pcm.getInt16(i, true));
    const probe = document.createElement("audio");
    probe.muted = true;
    probe.src = audio.src;
    await new Promise<void>((resolve, reject) => {
      probe.onloadedmetadata = () => resolve();
      probe.onerror = () => reject(new Error("Long-track metadata failed"));
    });
    probe.currentTime = 2999.5;
    await probe.play();
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const decodedNearEnd = !probe.paused && probe.currentTime > 3000;
    probe.pause();
    probe.removeAttribute("src");
    probe.load();
    return { blobBytes: blob.size, retainedBlobs: blobs.size, duration: audio.duration, finishEnergy, decodedNearEnd };
  });
  expect(facts.duration).toBeCloseTo(3004, 1);
  expect(facts.blobBytes).toBe(96_128_044);
  expect(facts.retainedBlobs).toBe(1);
  expect(facts.finishEnergy).toBeGreaterThan(1000);
  expect(facts.decodedNearEnd).toBe(true);
  const afterSeek = await nativeMemory();
  await page.getByRole("button", { name: "Adjust timer", exact: true }).click();
  for (let i = 0; i < 3; i += 1) {
    await page.getByRole("button", { name: "Increase round length", exact: true }).click();
    // Crossing the cap must discard the old source and use fallback.
    await expect(page.locator(".st-audio-status")).toContainText("unavailable");
    await expect(page.locator("audio[data-round-audio]")).toHaveCount(0);
    await page.getByRole("button", { name: "Decrease round length", exact: true }).click();
    await expect(page.locator(".st-audio-status")).toContainText("Round audio playing", { timeout: 30_000 });
  }
  await page.getByRole("button", { name: "Adjust timer", exact: true }).click();
  expect(await page.evaluate(() => (window as unknown as { __roundTimerAudioBlobs: Map<string, Blob> }).__roundTimerAudioBlobs.size)).toBe(1);
  await page.getByRole("button", { name: "Pause timer", exact: true }).click();
  await expect(page.locator("audio[data-round-audio]")).toHaveCount(1);
  const pausedTrack = await page.locator("audio[data-round-audio]").evaluate((audio: HTMLAudioElement) => {
    const blobs = (window as unknown as { __roundTimerAudioBlobs: Map<string, Blob> }).__roundTimerAudioBlobs;
    return { paused: audio.paused, count: blobs.size, size: blobs.get(audio.src)!.size };
  });
  expect(pausedTrack).toEqual({ paused: true, count: 1, size: 1_920_044 });
  await page.getByRole("button", { name: "End session", exact: true }).click();
  await page.getByRole("alertdialog", { name: "End session" }).getByRole("button", { name: "End", exact: true }).click();
  await expect(page.locator("audio[data-round-audio]")).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __roundTimerAudioBlobs: Map<string, Blob> }).__roundTimerAudioBlobs.size)).toBe(0);
  const released = await nativeMemory();
  const profile = { ...facts, buildToPlayingMs, baseline, loaded, afterSeek, released };
  await testInfo.attach("long-track-profile", { body: JSON.stringify(profile, null, 2), contentType: "application/json" });
  console.log("Round timer long-track profile:", JSON.stringify(profile));
});

test("a 90-minute session keeps its foreground clock without allocating an audio track", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("unlxck.session-timer.free-format", JSON.stringify({ rounds: 30, workSec: 180, restSec: 0 }));
  });
  await page.goto("/timer");
  await page.getByRole("button", { name: "Start round 1", exact: true }).click();
  await expect(page.locator(".st-audio-status")).toContainText("Round audio is unavailable. Keep this screen open.");
  await expect(page.locator("audio[data-round-audio]")).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __roundTimerAudioBlobs: Map<string, Blob> }).__roundTimerAudioBlobs.size)).toBe(0);
  await expect(page.locator(".st-clock-digits")).not.toHaveText("3:00");
});
for (const viewport of [{ width: 320, height: 640 }, { width: 844, height: 390 }]) {
  test(`round clock fits ${viewport.width}px and pauses in place`, async ({ page }, info) => {
    await page.setViewportSize(viewport);
    await page.emulateMedia({ reducedMotion: viewport.width === 320 ? "no-preference" : "reduce" });
    await page.goto("/timer");
    await page.evaluate(() => {
      localStorage.setItem("unlxck.session-timer.free-format", JSON.stringify({ rounds: 3, workSec: 21, restSec: 10 }));
      localStorage.setItem("unlxck.session-timer.settings", JSON.stringify({ prepSec: 30, voice: false }));
    });
    await page.reload();
    const nativePlay = await page.evaluateHandle(() => {
      const play = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = () => Promise.reject(new DOMException("Playback denied for test", "NotAllowedError"));
      return play;
    });
    await page.getByRole("button", { name: "Start round 1", exact: true }).click();
    const clock = page.getByRole("timer");
    await expect(page.locator(".st-phase")).toHaveText("Get ready");
    await expect(page.getByRole("button", { name: "Enable round audio" })).toBeVisible();
    await expect(page.locator(".st-board-track")).toBeVisible();
    await expect(page.locator(".st-glow, .st-phase-dot, .st-ring")).toHaveCount(0);
    const box = await clock.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width);
    expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height);
    const geometry = () => page.locator(".st-root").evaluate(root => ({
      height: root.scrollHeight,
      boxes: [".st-board-header", ".st-clock", ".st-board-track", ".st-tools", ".st-primary"].map(selector => {
        const { x, y, width, height } = root.querySelector(selector)!.getBoundingClientRect();
        return [x, y, width, height];
      }),
    }));
    const initial = await geometry();
    await page.evaluate(play => { HTMLMediaElement.prototype.play = play; }, nativePlay);
    await nativePlay.dispose();
    await page.getByRole("button", { name: "Enable round audio" }).click();
    await expect(page.locator(".st-audio-status")).toContainText("Round audio playing");
    expect(await geometry()).toEqual(initial);
    await page.getByRole("button", { name: "Start now", exact: true }).click();
    await expect(page.locator(".st-board-header")).toContainText("Work");
    expect(await geometry()).toEqual(initial);
    const slice = await page.locator(".st-work-slice").elementHandle();
    await page.screenshot({ path: info.outputPath("ringside.png") });
    await page.getByRole("button", { name: "Pause timer", exact: true }).click();
    await expect(page.locator(".st-phase")).toHaveText("Paused");
    expect(await geometry()).toEqual(initial);
    await page.getByRole("button", { name: "Resume timer", exact: true }).click();
    expect(await geometry()).toEqual(initial);
    expect(await slice!.evaluate(element => element.isConnected)).toBe(true);
    await slice!.dispose();
    await expect(page.locator(".st-root")).toHaveAttribute("data-tone", "warn", { timeout: 15_000 });
    await expect(page.locator(".st-board-track")).toHaveAttribute("data-warning", "true");
    expect(await geometry()).toEqual(initial);
    await page.getByRole("button", { name: "End round", exact: true }).click();
    await expect(page.locator(".st-phase")).toHaveText("Rest");
    const completion = page.locator(".st-round-complete");
    await expect(completion).toHaveCount(1);
    await expect(completion).toHaveCSS("animation-name", viewport.width === 320 ? "st-round-land" : "none");
    expect(await geometry()).toEqual(initial);
  });
}

test("the timer scoreboard follows the light and dark app themes", async ({ page }) => {
  await page.goto("/timer");
  const root = page.locator(".st-root");
  await expect(root).toBeVisible();
  // Signed out, the app settles on dark once auth resolves; hold the chosen
  // theme so that late reset cannot race the colour checks.
  const setTheme = (theme: "light" | "dark") => page.evaluate(value => {
    const html = document.documentElement;
    const held = window as unknown as { __heldTheme?: string; __themeHold?: MutationObserver };
    held.__heldTheme = value;
    held.__themeHold ??= new MutationObserver(() => {
      if (html.dataset.theme !== held.__heldTheme) html.dataset.theme = held.__heldTheme;
    });
    held.__themeHold.observe(html, { attributes: true, attributeFilter: ["data-theme"] });
    html.dataset.theme = value;
  }, theme);
  const lit = root.locator(".st-score-digit i[data-lit]").first();
  const unlit = root.locator(".st-score-digit i:not([data-lit])").first();

  await setTheme("light");
  await expect(root).toHaveCSS("background-color", "rgb(246, 246, 248)");
  await expect(lit).toHaveCSS("background-color", "rgb(17, 18, 22)");
  await expect(unlit).toHaveCSS("background-color", "rgb(226, 226, 230)");
  await page.getByRole("button", { name: "Start round 1", exact: true }).click();
  await expect(root.locator(".st-work-mark")).toHaveCSS("color", "rgb(255, 255, 255)");
  await expect(root.locator(".st-board-track")).toHaveCSS("background-color", "rgb(222, 222, 226)");
  await page.getByRole("button", { name: "Pause timer", exact: true }).click();
  await expect(root.locator(".st-phase")).toHaveText("Paused");

  await setTheme("dark");
  await expect(root).toHaveCSS("background-color", "rgb(11, 11, 12)");
  await expect(lit).toHaveCSS("background-color", "rgb(246, 247, 249)");
  await expect(unlit).toHaveCSS("background-color", "rgb(23, 23, 25)");
  await expect(root.locator(".st-board-track")).toHaveCSS("background-color", "rgb(36, 36, 39)");
});
