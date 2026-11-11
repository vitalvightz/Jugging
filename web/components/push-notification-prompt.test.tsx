import test from "node:test";
import assert from "node:assert/strict";
import "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { DevicePushPrompt } from "./push-notification-prompt";
import { INSTALL_PROMPT_EVENT, offerPushNotifications } from "@/lib/push-prompt";

async function mount(options: { permission?: string; enabled?: boolean; subscribed?: boolean; paused?: boolean; failSave?: boolean; beforeOpen?: () => void } = {}) {
  window.localStorage.clear();
  const originalFetch = globalThis.fetch;
  let requests = 0;
  const events: string[] = [];
  const notification = {
    permission: options.permission ?? "default",
    requestPermission: async () => { requests++; return "granted"; },
  };
  Object.defineProperty(window, "Notification", { configurable: true, value: notification });
  Object.defineProperty(globalThis, "Notification", { configurable: true, value: notification });
  Object.defineProperty(window, "PushManager", { configurable: true, value: function () {} });
  Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: {
    getRegistration: async () => ({ active: {}, pushManager: {
      getSubscription: async () => options.subscribed ? { endpoint: "existing" } : null,
      subscribe: async () => ({ toJSON: () => ({ endpoint: "https://push.test/device", keys: { auth: "auth", p256dh: "key" } }) }),
    } }),
  } });
  const prototype = window.HTMLDialogElement.prototype;
  prototype.showModal = function () { options.beforeOpen?.(); this.setAttribute("open", ""); };
  prototype.close = function () { this.removeAttribute("open"); };
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    if (String(input).endsWith("/api/analytics/events")) {
      events.push(JSON.parse(String(init?.body)).event_name);
      return new Response(JSON.stringify({ accepted: true }), { status: 202 });
    }
    if (String(input).endsWith("/api/push/settings")) return new Response(JSON.stringify({
      enabled: options.enabled !== false, public_key: "AQAB", preferences: { push_enabled: !options.paused },
    }));
    return new Response(null, { status: options.failSave ? 503 : 204 });
  }) as typeof fetch;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<DevicePushPrompt token="test-token" athleteId="athlete-1" />));
  return { container, events, requests: () => requests, cleanup: async () => {
    await act(async () => root.unmount()); container.remove(); globalThis.fetch = originalFetch;
  } };
}

async function saved(token = "test-token") {
  await act(async () => { offerPushNotifications(token); await new Promise(resolve => setTimeout(resolve, 600)); });
}

test("waits behind an existing modal; dismissing prevents another offer", async () => {
  const app = await mount();
  const safety = document.createElement("dialog"); safety.setAttribute("open", ""); document.body.append(safety);
  try {
    await saved();
    assert.equal(app.container.querySelector("dialog"), null);
    safety.remove();
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 600)); });
    assert.match(app.container.textContent!, /UNLXCK your next move/);
    assert.equal(app.requests(), 0, "permission requires the Enable click");
    const later = Array.from(app.container.querySelectorAll("button")).find(button => button.textContent === "Not now")!;
    await act(async () => later.click());
    await saved();
    assert.equal(app.container.querySelector("dialog"), null);
    assert.equal(window.localStorage.getItem("unlxck:push-prompt:v1:athlete-1"), "seen");
  } finally { safety.remove(); await app.cleanup(); }
});

test("suppresses blocked, granted, subscribed, disabled and paused devices", async () => {
  for (const options of [{ permission: "denied" }, { permission: "granted" }, { subscribed: true }, { enabled: false }, { paused: true }]) {
    const app = await mount(options);
    try { await saved(); assert.equal(app.container.querySelector("dialog"), null); assert.equal(app.requests(), 0); }
    finally { await app.cleanup(); }
  }
});

test("ignores another account's save and keeps subscription failures visible", async () => {
  const app = await mount({ failSave: true });
  try {
    await saved("other-token");
    assert.equal(app.container.querySelector("dialog"), null);
    await saved();
    const enable = app.container.querySelector("button")!;
    await act(async () => {
      enable.click();
      assert.equal(app.requests(), 1, "native permission starts synchronously in the click gesture");
    });
    assert.ok(app.container.querySelector('[role="alert"]'));
    assert.ok(app.container.querySelector("dialog[open]"));
  } finally { await app.cleanup(); }
});


test("records the offer only after the dialog successfully opens", async () => {
  let attempted = false;
  const app = await mount({ beforeOpen: () => {
    attempted = true;
    assert.equal(window.localStorage.getItem("unlxck:push-prompt:v1:athlete-1"), null,
      "preparing a dialog must not consume the offer");
  } });
  try {
    await saved();
    assert.equal(attempted, true);
    assert.ok(app.container.querySelector("dialog[open]"));
    assert.equal(window.localStorage.getItem("unlxck:push-prompt:v1:athlete-1"), "seen");
  } finally { await app.cleanup(); }
});

test("an interrupted dialog open releases the overlay and preserves the next offer", async () => {
  let interrupted = true;
  const previousPosition = document.body.style.position;
  const app = await mount({ beforeOpen: () => {
    if (interrupted) throw new DOMException("Navigation interrupted the dialog", "InvalidStateError");
  } });
  try {
    await saved();
    assert.equal(window.localStorage.getItem("unlxck:push-prompt:v1:athlete-1"), null);
    assert.equal(app.container.querySelector("dialog[open]"), null);
    assert.equal(document.body.style.position, previousPosition);
    interrupted = false;
    await saved();
    assert.ok(app.container.querySelector("dialog[open]"), "the same device gets another opportunity");
    assert.equal(window.localStorage.getItem("unlxck:push-prompt:v1:athlete-1"), "seen");
  } finally { await app.cleanup(); }
});


test("a save arriving during dialog opening cannot queue a second offer", async () => {
  let repeated = false;
  const app = await mount({ beforeOpen: () => {
    if (!repeated) {
      repeated = true;
      offerPushNotifications("test-token");
    }
  } });
  try {
    await saved();
    assert.ok(app.container.querySelector("dialog[open]"));
    const later = Array.from(app.container.querySelectorAll("button")).find(button => button.textContent === "Not now")!;
    await act(async () => later.click());
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 650)); });
    assert.equal(app.container.querySelector("dialog[open]"), null);
    assert.equal(window.localStorage.getItem("unlxck:push-prompt:v1:athlete-1"), "seen");
  } finally { await app.cleanup(); }
});

test("does not offer push permissions inside Instagram's browser", async () => {
  const original = Object.getOwnPropertyDescriptor(navigator, "userAgent");
  Object.defineProperty(navigator, "userAgent", { configurable: true, value: "Mozilla/5.0 (Linux; Android 15) Instagram 400.0.0" });
  const app = await mount();
  try {
    await saved();
    assert.equal(app.container.querySelector("dialog"), null);
    assert.equal(app.requests(), 0);
  } finally {
    await app.cleanup();
    if (original) Object.defineProperty(navigator, "userAgent", original);
    else Reflect.deleteProperty(navigator, "userAgent");
  }
});

test("takes the saved moment ahead of install guidance, and hands it on when there is nothing to offer", async () => {
  let installOffers = 0;
  const onInstall = () => { installOffers++; };
  window.addEventListener(INSTALL_PROMPT_EVENT, onInstall);
  try {
    const app = await mount();
    try {
      await saved();
      assert.ok(app.container.querySelector("dialog[open]"));
      assert.equal(installOffers, 0, "install guidance must not pre-empt an available notification offer");
    } finally { await app.cleanup(); }
    const paused = await mount({ paused: true });
    try {
      await saved();
      assert.equal(paused.container.querySelector("dialog"), null);
      assert.equal(installOffers, 1, "a paused device passes the moment to install guidance");
    } finally { await paused.cleanup(); }
  } finally { window.removeEventListener(INSTALL_PROMPT_EVENT, onInstall); }
});

test("an iPhone browser tab is not asked: install guidance gets the moment first", async () => {
  const original = Object.getOwnPropertyDescriptor(navigator, "userAgent");
  Object.defineProperty(navigator, "userAgent", { configurable: true, value: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1" });
  let installOffers = 0;
  const onInstall = () => { installOffers++; };
  window.addEventListener(INSTALL_PROMPT_EVENT, onInstall);
  const app = await mount();
  try {
    await saved();
    assert.equal(app.container.querySelector("dialog"), null);
    assert.equal(app.requests(), 0);
    assert.equal(installOffers, 1);
  } finally {
    window.removeEventListener(INSTALL_PROMPT_EVENT, onInstall);
    await app.cleanup();
    if (original) Object.defineProperty(navigator, "userAgent", original);
    else Reflect.deleteProperty(navigator, "userAgent");
  }
});

test("records shown, accepted and subscribed so activation drop-off is measurable", async () => {
  const app = await mount();
  try {
    await saved();
    assert.deepEqual(app.events, ["push_prompt_shown"]);
    const enable = Array.from(app.container.querySelectorAll("button")).find(button => button.textContent === "Enable reminders")!;
    await act(async () => { enable.click(); await new Promise(resolve => setTimeout(resolve, 50)); });
    assert.deepEqual(app.events, ["push_prompt_shown", "push_prompt_accepted", "push_subscribed"]);
    assert.equal(app.container.querySelector("dialog"), null);
  } finally { await app.cleanup(); }
});
