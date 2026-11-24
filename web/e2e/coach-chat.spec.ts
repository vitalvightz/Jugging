import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { setup } from "./custom-plan-fixture";
import type { ChatMessage, InboxRow } from "../lib/coach-chat";

// The fixture signs in as this profile; the role decides which side of the chat it is.
const me = "00000000-0000-4000-8000-000000000001";
const leo = "00000000-0000-4000-8000-000000000003";
const sam = "00000000-0000-4000-8000-000000000004";
const chatId = "00000000-0000-4000-8000-0000000000c1";
const session = "00000000-0000-4000-8000-0000000000aa";
const PHOTO = "data:image/svg+xml;base64," + Buffer.from(
  "<svg xmlns='http://www.w3.org/2000/svg' width='320' height='200'><rect width='320' height='200' fill='#333'/></svg>",
).toString("base64");

// A short real WAV, so the voice note player draws its waveform from the audio.
const WAV = (() => {
  const rate = 8000, samples = rate;
  const data = Buffer.alloc(44 + samples * 2);
  data.write("RIFF", 0); data.writeUInt32LE(36 + samples * 2, 4); data.write("WAVE", 8); data.write("fmt ", 12);
  data.writeUInt32LE(16, 16); data.writeUInt16LE(1, 20); data.writeUInt16LE(1, 22); data.writeUInt32LE(rate, 24);
  data.writeUInt32LE(rate * 2, 28); data.writeUInt16LE(2, 32); data.writeUInt16LE(16, 34); data.write("data", 36);
  data.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++) data.writeInt16LE(Math.round(Math.sin(i / 6) * 12000 * Math.sin(i / samples * Math.PI)), 44 + i * 2);
  return "data:audio/wav;base64," + data.toString("base64");
})();

function inboxRows(role: "coach" | "athlete"): InboxRow[] {
  const base = { avatar_url: null, role, active: true, fight_date: null, attention: false, missed_checkin: false, other_read_at: null };
  return role === "coach" ? [
    { ...base, chat_id: chatId, other_id: leo, name: "Leo Martins", last_message_at: new Date().toISOString(),
      last_preview: "Round 3 felt slow. Thoughts?", last_from_me: false, unread: 2, fight_date: new Date(Date.now() + 19 * 864e5).toISOString().slice(0, 10) },
    { ...base, chat_id: null, other_id: sam, name: "Sam Ortiz", last_message_at: null, last_preview: "", last_from_me: false,
      unread: 0, attention: true, missed_checkin: true },
  ] : [
    { ...base, chat_id: chatId, other_id: leo, name: "Coach Rey", last_message_at: new Date().toISOString(),
      last_preview: "How did sparring feel?", last_from_me: false, unread: 1 },
  ];
}

function history(role: "coach" | "athlete"): ChatMessage[] {
  const coach = role === "coach" ? me : leo;
  const fighter = role === "coach" ? leo : me;
  const at = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
  const base = { chat_id: chatId, coach_id: coach, athlete_id: fighter, asset_id: null, quote: null, reply_to_id: null, reactions: {} };
  return [
    { ...base, id: 1, client_id: "a", sender_id: coach, kind: "text", body: "How did sparring feel?", created_at: at(30) },
    { ...base, id: 2, client_id: "b", sender_id: fighter, kind: "text", body: "Round 3 felt slow. Thoughts?", created_at: at(20),
      quote: { source: "external", target_id: session, day: new Date().toISOString().slice(0, 10), title: "Sparring", meta: "45 min · Hard" },
      reactions: { [coach]: "🔥" } },
    { ...base, id: 3, client_id: "c", sender_id: fighter, kind: "photo", body: "", asset_id: "asset-1", created_at: at(19) },
    { ...base, id: 4, client_id: "d", sender_id: fighter, kind: "voice", body: "", asset_id: "asset-2", created_at: at(18) },
  ];
}

async function chatFixture(page: Page, baseURL: string, role: "coach" | "athlete") {
  await setup(page, baseURL, role);
  const sent: Record<string, unknown>[] = [];
  const notified: string[] = [];
  const messages = history(role);
  await page.route("**/rest/v1/coach_messages?**", route => route.fulfill({ json: [...messages].reverse() }));
  await page.route("**/rest/v1/coach_connections?**", route => route.fulfill({ json: [{ id: "connection" }] }));
  await page.route("**/rest/v1/rpc/**", async route => {
    const name = new URL(route.request().url()).pathname.split("/").at(-1);
    const body = route.request().postDataJSON() ?? {};
    if (name === "coach_chat_inbox") return route.fulfill({ json: inboxRows(role) });
    if (name === "coach_chat_read") return route.fulfill({ status: 204, body: "" });
    if (name === "coach_chat_send") {
      sent.push(body);
      const message: ChatMessage = {
        id: 10 + sent.length, chat_id: chatId, coach_id: role === "coach" ? me : leo, athlete_id: role === "coach" ? leo : me,
        sender_id: me, client_id: body.p_client_id, kind: body.p_kind, body: body.p_body, asset_id: null, reply_to_id: body.p_reply_to,
        quote: body.p_quote_source ? { source: body.p_quote_source, target_id: body.p_quote_target, day: "2026-11-23", title: "Pads", meta: "Done · RPE 7" } : null,
        reactions: {}, created_at: new Date().toISOString(),
      };
      return route.fulfill({ json: { ...message, asset_id: body.p_asset_id ?? null, created: true } });
    }
    return route.fulfill({ json: null });
  });
  await page.route("**/api/chats/**", route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/notify")) { notified.push(url.pathname); return route.fulfill({ status: 202, body: "" }); }
    return route.fulfill({ json: [
      { message_id: 3, kind: "photo", url: PHOTO, thumb_url: PHOTO, width: 320, height: 200, duration_ms: null },
      { message_id: 4, kind: "voice", url: WAV, thumb_url: null, width: null, height: null, duration_ms: 1000 },
    ] });
  });
  return { sent, notified };
}

for (const width of [390, 1440]) {
  test(`coach chats at ${width}px: inbox, thread, training card and an optimistic send`, async ({ page, baseURL }) => {
    const { sent, notified } = await chatFixture(page, baseURL!, "coach");
    // The headless browser has no audio output: playback reports success without sound.
    await page.addInitScript(() => {
      HTMLMediaElement.prototype.play = function play(this: HTMLMediaElement) { this.dispatchEvent(new Event("play")); return Promise.resolve(); };
      HTMLMediaElement.prototype.pause = function pause(this: HTMLMediaElement) { this.dispatchEvent(new Event("pause")); };
    });
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/coach/chats");
    const list = page.getByRole("region", { name: "Chats" });
    await expect(list.getByText("Leo Martins")).toBeVisible();
    await expect(list.getByLabel("2 unread")).toBeVisible();
    if (width < 900) {
      const tab = page.getByRole("navigation", { name: "Coach navigation" }).getByRole("link", { name: /Chats/ });
      await expect(tab).toContainText("2 unread");
    }
    await expect(list.getByText("Missed check-in ·")).toBeVisible();
    await expect(list.getByText("Start the conversation")).toBeVisible();
    await page.screenshot({ path: `test-results/coach-chat-inbox-${width}.png` });

    await list.getByRole("button", { name: /Leo Martins/ }).click();
    await expect(page).toHaveURL(new RegExp(`with=${leo}`));
    const thread = page.getByRole("dialog", { name: "Chat with Leo Martins" });
    await expect(thread.getByText("Round 3 felt slow. Thoughts?").first()).toBeVisible();
    // Opening the chat focuses the sheet, not Back, so no focus ring shows on Back.
    await expect(thread.getByRole("button", { name: "Back to chats" })).not.toBeFocused();
    await thread.getByRole("banner").or(thread.locator("header")).first().screenshot({ path: `test-results/coach-chat-header-${width}.png` });
    await expect(thread.getByText("Sparring", { exact: true })).toBeVisible();
    await expect(thread.getByText("Fight in 19 days")).toBeVisible();
    await expect(thread.getByRole("img", { name: "Photo" })).toBeVisible();
    // Voice notes use the training attachments' waveform player, not the browser's audio bar.
    await expect(thread.locator("audio")).toHaveCount(0);
    const note = thread.getByRole("slider", { name: "Voice note position" }).locator("..");
    const unplayed = (await note.boundingBox())!;
    await thread.getByRole("button", { name: /^Play voice note from Leo,/ }).click();
    await expect(thread.getByRole("slider", { name: "Voice note position" })).toBeVisible();
    // The speed chip's slot is reserved: playing, and every speed, keep the note's size and the chip's place.
    const speed = thread.getByRole("button", { name: /^Playback speed/ });
    await expect(speed).toBeVisible();
    const chip = (await speed.boundingBox())!;
    for (const label of ["1.5×", "2×", "1×"]) {
      await speed.click();
      await expect(speed).toHaveText(label);
      const now = (await speed.boundingBox())!;
      expect([now.x, now.y, now.width]).toEqual([chip.x, chip.y, chip.width]);
      const box = (await note.boundingBox())!;
      expect([box.width, box.height]).toEqual([unplayed.width, unplayed.height]);
    }

    await thread.getByRole("textbox", { name: "Message" }).fill("Keep the last round technical.");
    await thread.getByRole("button", { name: "Send", exact: true }).click();
    await expect(thread.getByText("Keep the last round technical.")).toBeVisible();
    await expect(thread.getByText("Delivered")).toBeVisible();
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ p_other_id: leo, p_kind: "text", p_body: "Keep the last round technical." });
    await expect.poll(() => notified.length).toBe(1);

    await thread.getByRole("button", { name: "Message options" }).first().click();
    await expect(thread.getByRole("toolbar", { name: "Message actions" })).toBeVisible();
    await page.screenshot({ path: `test-results/coach-chat-thread-${width}.png` });
    const results = await new AxeBuilder({ page }).include("dialog").analyze();
    expect(results.violations.filter(v => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);

    // The chat is its own sheet over the inbox; Back closes it and the list is there again.
    await thread.getByRole("button", { name: "Back to chats" }).click();
    await expect(thread).toBeHidden();
    await expect(list.getByText("Leo Martins")).toBeVisible();
    await expect(page).not.toHaveURL(/with=/);
  });
}

test.describe("voice notes are recorded by holding the mic", () => {
  test("hold to record, slide left to cancel; a tap only explains", async ({ page, baseURL }) => {
    // A tone stands in for the microphone; each request for it is counted.
    await page.addInitScript(() => {
      (window as unknown as { micRequests: number }).micRequests = 0;
      navigator.mediaDevices.getUserMedia = async () => {
        (window as unknown as { micRequests: number }).micRequests++;
        const context = new AudioContext();
        const tone = context.createOscillator();
        const out = context.createMediaStreamDestination();
        tone.connect(out);
        tone.start();
        return out.stream;
      };
    });
    const { sent } = await chatFixture(page, baseURL!, "coach");
    await page.setViewportSize({ width: 390, height: 900 });
    await page.goto(`/coach/chats?with=${leo}`);
    const thread = page.getByRole("dialog", { name: "Chat with Leo Martins" });
    const mic = thread.getByRole("button", { name: "Hold to record a voice note" });
    await expect(mic).toBeVisible();
    // Measure once the sheet has finished sliding in, not part-way through.
    await thread.evaluate(sheet => Promise.all(sheet.getAnimations({ subtree: true }).map(a => a.finished)));

    // Idle, the composer is one row: the field and mic share a line, mic on the right.
    const field = thread.getByRole("textbox", { name: "Message" });
    const centre = async () => {
      const b = (await mic.boundingBox())!;
      return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
    };
    const idle = await centre();
    const fieldBox = (await field.boundingBox())!;
    expect(Math.abs(idle.y - (fieldBox.y + fieldBox.height / 2))).toBeLessThan(4);
    expect(idle.x).toBeGreaterThan(fieldBox.x + fieldBox.width);
    // Coaches send photos in chat too.
    await expect(thread.getByRole("button", { name: "Send a photo" })).toBeVisible();

    // Held, the mic stays exactly where the finger is, through recording and cancelling.
    await page.mouse.move(idle.x, idle.y);
    await page.mouse.down();
    await expect(thread.getByText("‹ Slide left to cancel")).toBeVisible();
    await page.screenshot({ path: "test-results/coach-chat-hold-390.png" });
    const held = await centre();
    expect(Math.abs(held.x - idle.x)).toBeLessThan(1);
    expect(Math.abs(held.y - idle.y)).toBeLessThan(1);
    await page.mouse.move(idle.x - 150, idle.y, { steps: 5 });
    await expect(thread.getByText("Release to cancel")).toBeVisible();
    await page.screenshot({ path: "test-results/coach-chat-cancelling-390.png" });
    const cancelling = await centre();
    expect(Math.abs(cancelling.x - idle.x)).toBeLessThan(1);
    expect(Math.abs(cancelling.y - idle.y)).toBeLessThan(1);
    await page.mouse.up();
    await expect(field).toBeVisible();
    expect(sent).toHaveLength(0);
    const after = await centre();
    expect(Math.abs(after.x - idle.x)).toBeLessThan(1);
    expect(Math.abs(after.y - idle.y)).toBeLessThan(1);

    // Hold, talk, let go: the note uploads and is sent once, with no extra step.
    const asset = "00000000-0000-4000-8000-0000000000f1";
    await page.route("**/api/attachments/uploads", route => route.fulfill({ status: 201, json: {
      asset_id: asset, bucket: "attachment-intake", path: `${me}/${asset}.wav`, content_type: "audio/wav",
      expires_at: new Date(Date.now() + 600_000).toISOString() } }));
    await page.route("**/storage/v1/object/attachment-intake/**", route => route.fulfill({ json: { Key: "ok" } }));
    await page.route(`**/api/attachments/uploads/${asset}/process`, route => route.fulfill({ json: { asset_id: asset, status: "ready" } }));
    await page.mouse.move(idle.x, idle.y);
    await page.mouse.down();
    await expect(thread.getByText("‹ Slide left to cancel")).toBeVisible();
    await page.waitForTimeout(1200);
    await page.mouse.up();
    await expect.poll(() => sent.length).toBe(1);
    expect(sent[0]).toMatchObject({ p_kind: "voice", p_asset_id: asset });
    sent.length = 0;

    await mic.click();
    await expect(thread.getByRole("alert")).toContainText(/Hold the mic|Too short/);
    expect(sent).toHaveLength(0);

    // Holding Enter repeats the key: it starts one recording, never restarts it.
    await page.getByRole("button", { name: "Dismiss" }).click();
    const before = await page.evaluate(() => (window as unknown as { micRequests: number }).micRequests);
    await mic.focus();
    await page.keyboard.down("Enter");
    await page.keyboard.down("Enter");
    await page.keyboard.down("Enter");
    await page.keyboard.up("Enter");
    await expect(thread.getByRole("button", { name: "Send voice note" })).toBeVisible();
    expect(await page.evaluate(() => (window as unknown as { micRequests: number }).micRequests)).toBe(before + 1);
    await thread.getByRole("button", { name: "Cancel" }).click();
    expect(sent).toHaveLength(0);
  });
});

test("a fighter with one coach lands in the chat with the linked session attached", async ({ page, baseURL }) => {
  const { sent } = await chatFixture(page, baseURL!, "athlete");
  await page.setViewportSize({ width: 390, height: 860 });
  await page.goto(`/chat?quote=unlxck:${session}`);
  const thread = page.getByRole("dialog", { name: "Chat with Coach Rey" });
  await expect(thread.getByText("Training card")).toBeVisible();
  await thread.getByRole("textbox", { name: "Message" }).fill("Knee felt off on pads");
  await thread.getByRole("button", { name: "Send", exact: true }).click();
  await expect(thread.getByText("Pads", { exact: true })).toBeVisible();
  expect(sent[0]).toMatchObject({ p_other_id: leo, p_quote_source: "unlxck", p_quote_target: session });
  await expect(thread.getByText("Training card")).toHaveCount(0);
  await page.screenshot({ path: "test-results/coach-chat-fighter-390.png" });
});

// Sentry JAVASCRIPT-NEXTJS-2: iOS Safari refused the Realtime socket
// ("The operation is insecure.") and the throw from subscribe() crashed the page.
test("with WebSockets refused, chats still load, send, and refresh when the screen returns", async ({ page, baseURL }) => {
  await page.addInitScript(() => {
    (window as unknown as { WebSocket: unknown }).WebSocket = function RefusedWebSocket() {
      throw new DOMException("The operation is insecure.", "SecurityError");
    };
  });
  const pageErrors: string[] = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  const { sent } = await chatFixture(page, baseURL!, "coach");
  await page.setViewportSize({ width: 390, height: 860 });
  await page.goto(`/coach/chats?with=${leo}`);

  const thread = page.getByRole("dialog", { name: "Chat with Leo Martins" });
  await expect(thread.getByText("Round 3 felt slow. Thoughts?").first()).toBeVisible();
  await thread.getByRole("textbox", { name: "Message" }).fill("Still works without Realtime.");
  await thread.getByRole("button", { name: "Send", exact: true }).click();
  await expect(thread.getByText("Delivered")).toBeVisible();
  expect(sent).toHaveLength(1);

  // A reply lands while Realtime is down; coming back to the tab shows it.
  const reply = { ...history("coach")[1], id: 50, client_id: "late", body: "Got it, coach.", quote: null, reactions: {},
    created_at: new Date().toISOString() };
  await page.route("**/rest/v1/coach_messages?**", route => route.fulfill({ json: [reply, ...[...history("coach")].reverse()] }));
  await page.evaluate(() => {
    for (const state of ["hidden", "visible"]) {
      Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
    }
  });
  await expect(thread.getByText("Got it, coach.")).toBeVisible();
  expect(pageErrors.filter(message => /WebSocket|insecure/i.test(message))).toEqual([]);
});

test("on a phone, a fighter reaches coach chat from the top bar, with the unread count", async ({ page, baseURL }) => {
  await chatFixture(page, baseURL!, "athlete");
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto("/history");
  const button = page.getByRole("link", { name: "Coach chat, 1 unread" });
  await expect(button).toBeVisible();
  await page.screenshot({ path: "test-results/coach-chat-phone-entry-390.png" });
  await button.click();
  await expect(page).toHaveURL(/\/chat/);
  await expect(page.getByRole("dialog", { name: "Chat with Coach Rey" })).toBeVisible();
});

test.describe("mobile composer and voice notes", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = async () => {
        const context = new AudioContext();
        const tone = context.createOscillator();
        const out = context.createMediaStreamDestination();
        tone.connect(out);
        tone.start();
        return out.stream;
      };
    });
  });

  test("the field never triggers iOS zoom, grows to a cap, and keeps the latest message in view", async ({ page, baseURL }) => {
    await chatFixture(page, baseURL!, "coach");
    await page.setViewportSize({ width: 390, height: 700 });
    await page.goto(`/coach/chats?with=${leo}`);
    const thread = page.getByRole("dialog", { name: "Chat with Leo Martins" });
    const field = thread.getByRole("textbox", { name: "Message" });
    await expect(field).toBeVisible();
    // iOS Safari zooms into any field under 16px.
    expect(await field.evaluate(node => parseFloat(getComputedStyle(node).fontSize))).toBeGreaterThanOrEqual(16);
    const scroller = thread.locator("[class*='messages']").first();
    const atBottom = () => scroller.evaluate(node => node.scrollHeight - node.scrollTop - node.clientHeight < 2);
    await expect.poll(atBottom).toBe(true);
    const oneLine = (await field.boundingBox())!.height;
    await field.fill("Line one\nLine two\nLine three");
    const threeLines = (await field.boundingBox())!.height;
    expect(threeLines).toBeGreaterThan(oneLine + 20);
    // The list shrank as the field grew; the newest message stays in view.
    await expect.poll(atBottom).toBe(true);
    await field.fill(Array.from({ length: 20 }, (_, i) => `Line ${i}`).join("\n"));
    expect((await field.boundingBox())!.height).toBeLessThanOrEqual(142);
    // Nothing spills sideways, and the send button stays on screen.
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const send = (await thread.getByRole("button", { name: "Send", exact: true }).boundingBox())!;
    expect(send.x + send.width).toBeLessThanOrEqual(390);
  });

  test("a voice note is playable at once and keeps one size when its link arrives", async ({ page, baseURL }) => {
    await chatFixture(page, baseURL!, "coach");
    let release: () => void = () => undefined;
    const held = new Promise<void>(resolve => { release = resolve; });
    await page.route("**/api/chats/files**", async route => {
      await held;
      return route.fallback();
    });
    await page.setViewportSize({ width: 390, height: 900 });
    await page.goto(`/coach/chats?with=${leo}`);
    const thread = page.getByRole("dialog", { name: "Chat with Leo Martins" });
    // No spinner while the file link is on its way: the play button is there already.
    const player = thread.getByRole("button", { name: /^Play voice note from Leo,/ });
    await expect(player).toBeVisible();
    await expect(thread.getByRole("group", { name: "Voice note loading" })).toHaveCount(0);
    const box = thread.getByRole("slider", { name: "Voice note position" }).locator("..");
    const before = (await box.boundingBox())!;
    release();
    await expect(box.getByText("0:01")).toBeVisible();
    const after = (await box.boundingBox())!;
    expect(Math.abs(after.height - before.height)).toBeLessThan(2);
    expect(Math.abs(after.width - before.width)).toBeLessThan(2);
  });

  test("a reopened chat shows its messages at once, before the server answers", async ({ page, baseURL }) => {
    await chatFixture(page, baseURL!, "coach");
    await page.setViewportSize({ width: 390, height: 900 });
    await page.goto(`/coach/chats?with=${leo}`);
    const thread = page.getByRole("dialog", { name: "Chat with Leo Martins" });
    await expect(thread.getByRole("button", { name: /^Play voice note from Leo,/ })).toBeVisible();
    // Give the device copy time to save, then make the server hang.
    await page.waitForTimeout(500);
    await page.route("**/rest/v1/coach_messages**", () => new Promise(() => undefined));
    await page.route("**/rest/v1/rpc/coach_chat_inbox**", () => new Promise(() => undefined));
    await page.reload();
    await expect(page.getByRole("dialog", { name: "Chat with Leo Martins" })
      .getByRole("button", { name: /^Play voice note from Leo,/ })).toBeVisible();
  });

  test("a link to a fighter missing from the remembered inbox still opens their chat", async ({ page, baseURL }) => {
    await chatFixture(page, baseURL!, "coach");
    await page.setViewportSize({ width: 390, height: 900 });
    await page.goto("/coach/chats");
    await expect(page.getByRole("button", { name: /Leo Martins/ })).toBeVisible();
    await page.waitForTimeout(500);
    // The device copy predates the connection: Leo isn't in it yet.
    await page.evaluate(id => {
      for (const key of Object.keys(localStorage).filter(k => k.startsWith("unlxck.chat.v1."))) {
        const data = JSON.parse(localStorage.getItem(key)!);
        data.inbox = data.inbox.filter((row: { other_id: string }) => row.other_id !== id);
        localStorage.setItem(key, JSON.stringify(data));
      }
    }, leo);
    await page.goto(`/coach/chats?with=${leo}`);
    await expect(page.getByRole("dialog", { name: "Chat with Leo Martins" })).toBeVisible();
  });

  test("the sheet fills the screen without a keyboard and sits on the keyboard with one", async ({ page, baseURL }) => {
    // iOS's visible area, under the test's control: a little short of the screen
    // (as iOS reports it with no keyboard), then a keyboard's worth shorter.
    await page.addInitScript(() => {
      const fake = new EventTarget() as EventTarget & { height: number; offsetTop: number; scale: number };
      fake.height = innerHeight - 60;
      fake.offsetTop = 0;
      fake.scale = 1;
      Object.defineProperty(window, "visualViewport", { value: fake });
      (window as unknown as { keyboard: (open: boolean) => void }).keyboard = open => {
        fake.height = open ? innerHeight - 340 : innerHeight - 60;
        fake.dispatchEvent(new Event("resize"));
      };
    });
    await chatFixture(page, baseURL!, "coach");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/coach/chats?with=${leo}`);
    const thread = page.getByRole("dialog", { name: "Chat with Leo Martins" });
    await expect(thread).toBeVisible();
    await expect(thread).not.toHaveAttribute("data-settling");
    const height = () => thread.evaluate(sheet => sheet.getBoundingClientRect().height);
    // No keyboard: the whole screen, so the composer sits on the home indicator.
    expect(await height()).toBe(844);
    await page.evaluate(() => (window as unknown as { keyboard: (open: boolean) => void }).keyboard(true));
    await expect.poll(height).toBe(844 - 340);
    await expect(thread).toHaveAttribute("data-keyboard", "true");
    await page.evaluate(() => (window as unknown as { keyboard: (open: boolean) => void }).keyboard(false));
    await expect.poll(height).toBe(844);
    await expect(thread).not.toHaveAttribute("data-keyboard");
  });

  test("a failed voice upload stays one message and sends exactly once on retry", async ({ page, baseURL }) => {
    const { sent } = await chatFixture(page, baseURL!, "coach");
    const asset = "00000000-0000-4000-8000-0000000000f2";
    let reserves = 0;
    let ready = false;
    await page.route("**/api/attachments/uploads", route => {
      reserves++;
      return route.fulfill({ status: 201, json: { asset_id: asset, bucket: "attachment-intake", path: `${me}/${asset}.wav`,
        content_type: "audio/wav", expires_at: new Date(Date.now() + 600_000).toISOString() } });
    });
    await page.route("**/storage/v1/object/attachment-intake/**", route => route.fulfill({ json: { Key: "ok" } }));
    await page.route(`**/api/attachments/uploads/${asset}/process`, route => route.fulfill({ json: ready
      ? { asset_id: asset, status: "ready" } : { asset_id: asset, status: "rejected", reason: "Upload interrupted." } }));
    await page.setViewportSize({ width: 390, height: 900 });
    await page.goto(`/coach/chats?with=${leo}`);
    const thread = page.getByRole("dialog", { name: "Chat with Leo Martins" });
    const mic = thread.getByRole("button", { name: "Hold to record a voice note" });
    const box = (await mic.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await expect(thread.getByText("‹ Slide left to cancel")).toBeVisible();
    await page.waitForTimeout(1200);
    await page.mouse.up();
    // The note is in the conversation, playable from the device; the composer is free.
    const retry = thread.getByRole("button", { name: "Not sent · Retry" });
    await expect(retry).toBeVisible();
    await expect(thread.getByRole("textbox", { name: "Message" })).toBeEnabled();
    expect(sent).toHaveLength(0);
    ready = true;
    // A double tap on Retry still sends once.
    await retry.dblclick();
    await expect.poll(() => sent.length).toBe(1);
    expect(sent[0]).toMatchObject({ p_kind: "voice", p_asset_id: asset });
    await expect(thread.getByRole("button", { name: "Not sent · Retry" })).toHaveCount(0);
    expect(reserves).toBe(2);
    await page.waitForTimeout(300);
    expect(sent).toHaveLength(1);
  });
});
