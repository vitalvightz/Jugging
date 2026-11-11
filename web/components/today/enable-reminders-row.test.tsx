import test from "node:test";
import assert from "node:assert/strict";
import "../test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { ToastProvider } from "@/components/toast-provider";
import { pushPromptSeenKey } from "@/lib/push-prompt";
import { EnableRemindersRow } from "./enable-reminders-row";

async function mount({ seen = true, subscribed = false }: { seen?: boolean; subscribed?: boolean } = {}) {
  window.localStorage.clear();
  if (seen) window.localStorage.setItem(pushPromptSeenKey("athlete-1"), "seen");
  const originalFetch = globalThis.fetch;
  const saves: string[] = [];
  const notification = { permission: "default", requestPermission: async () => "granted" };
  Object.defineProperty(window, "Notification", { configurable: true, value: notification });
  Object.defineProperty(globalThis, "Notification", { configurable: true, value: notification });
  Object.defineProperty(window, "PushManager", { configurable: true, value: function () {} });
  Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: {
    getRegistration: async () => ({ active: {}, pushManager: {
      getSubscription: async () => subscribed ? { endpoint: "existing" } : null,
      subscribe: async () => ({ toJSON: () => ({ endpoint: "https://push.test/device", keys: { auth: "auth", p256dh: "key" } }) }),
    } }),
  } });
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/push/settings")) return new Response(JSON.stringify({
      enabled: true, public_key: "AQAB", preferences: { push_enabled: true },
    }));
    if (url.endsWith("/api/push/subscriptions")) saves.push(String(init?.body));
    return new Response(JSON.stringify({ ok: true, accepted: true }));
  }) as typeof fetch;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const render = () => act(async () => {
    root.render(<ToastProvider><EnableRemindersRow token="test-token" athleteId="athlete-1" /></ToastProvider>);
    await new Promise(resolve => setTimeout(resolve, 20));
  });
  await render();
  const button = (name: string) => Array.from(container.querySelectorAll("button"))
    .find(node => node.textContent === name || node.getAttribute("aria-label") === name);
  return { container, saves, button, cleanup: async () => {
    await act(async () => root.unmount()); container.remove(); globalThis.fetch = originalFetch;
  } };
}

test("an athlete who passed on the automatic offer can enable reminders from Today", async () => {
  const app = await mount();
  try {
    assert.match(app.container.textContent!, /Reminders are off/);
    await act(async () => { app.button("Enable reminders")!.click(); await new Promise(resolve => setTimeout(resolve, 50)); });
    assert.equal(app.saves.length, 1, "the tap subscribes this device without a trip to Settings");
    assert.equal(app.button("Enable reminders"), undefined);
  } finally { await app.cleanup(); }
});

test("stays out of the way before the automatic offer and once the device is subscribed", async () => {
  for (const options of [{ seen: false }, { subscribed: true }]) {
    const app = await mount(options);
    try { assert.equal(app.button("Enable reminders"), undefined); }
    finally { await app.cleanup(); }
  }
});

test("hiding the row snoozes it instead of removing the route for good", async () => {
  const app = await mount();
  const realNow = Date.now;
  try {
    await act(async () => app.button("Hide reminders offer")!.click());
    assert.equal(app.button("Enable reminders"), undefined);
    const until = Number(window.localStorage.getItem("unlxck:enable-reminders-row:snoozed-until"));
    assert.ok(until > realNow() + 13 * 24 * 60 * 60 * 1000);
  } finally { Date.now = realNow; await app.cleanup(); }
});
