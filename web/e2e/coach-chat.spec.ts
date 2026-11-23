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
      return route.fulfill({ json: { ...message, created: true } });
    }
    return route.fulfill({ json: null });
  });
  await page.route("**/api/chats/**", route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/notify")) { notified.push(url.pathname); return route.fulfill({ status: 202, body: "" }); }
    return route.fulfill({ json: [{ message_id: 3, kind: "photo", url: PHOTO, thumb_url: PHOTO, width: 320, height: 200, duration_ms: null }] });
  });
  return { sent, notified };
}

for (const width of [390, 1440]) {
  test(`coach chats at ${width}px: inbox, thread, training card and an optimistic send`, async ({ page, baseURL }) => {
    const { sent, notified } = await chatFixture(page, baseURL!, "coach");
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/coach/chats");
    const list = page.getByRole("region", { name: "Chats" });
    await expect(list.getByText("Leo Martins")).toBeVisible();
    await expect(list.getByLabel("2 unread")).toBeVisible();
    await expect(list.getByText("Missed check-in ·")).toBeVisible();
    await expect(list.getByText("Start the conversation")).toBeVisible();
    await page.screenshot({ path: `test-results/coach-chat-inbox-${width}.png` });

    await list.getByRole("button", { name: /Leo Martins/ }).click();
    await expect(page).toHaveURL(new RegExp(`with=${leo}`));
    const thread = page.getByRole("region", { name: "Chat with Leo Martins" });
    await expect(thread.getByText("Round 3 felt slow. Thoughts?").first()).toBeVisible();
    await expect(thread.getByText("Sparring", { exact: true })).toBeVisible();
    await expect(thread.getByText("Fight in 19 days")).toBeVisible();
    await expect(thread.getByRole("img", { name: "Photo" })).toBeVisible();
    if (width < 900) await expect(list).toBeHidden();

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
    const results = await new AxeBuilder({ page }).include("main").analyze();
    expect(results.violations.filter(v => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);
  });
}

test("a fighter with one coach lands in the chat with the linked session attached", async ({ page, baseURL }) => {
  const { sent } = await chatFixture(page, baseURL!, "athlete");
  await page.setViewportSize({ width: 390, height: 860 });
  await page.goto(`/chat?quote=unlxck:${session}`);
  const thread = page.getByRole("region", { name: "Chat with Coach Rey" });
  await expect(thread.getByText("Training card")).toBeVisible();
  await thread.getByRole("textbox", { name: "Message" }).fill("Knee felt off on pads");
  await thread.getByRole("button", { name: "Send", exact: true }).click();
  await expect(thread.getByText("Pads", { exact: true })).toBeVisible();
  expect(sent[0]).toMatchObject({ p_other_id: leo, p_quote_source: "unlxck", p_quote_target: session });
  await expect(thread.getByText("Training card")).toHaveCount(0);
  await page.screenshot({ path: "test-results/coach-chat-fighter-390.png" });
});
