import test from "node:test";
import assert from "node:assert/strict";
import "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { PathnameContext } from "next/dist/shared/lib/hooks-client-context.shared-runtime";
import { InstallPrompt } from "./install-unlxck";
import { PwaRegister } from "./pwa-register";
import { INSTALL_PROMPT_EVENT, offerPushNotifications, PUSH_PROMPT_EVENT } from "@/lib/push-prompt";
import { PWA_INSTALL_GUIDE_DISMISSED_KEY, PWA_INSTALL_PROMPT_SEEN_KEY, PWA_INSTAGRAM_EXIT_SEEN_KEY } from "@/lib/pwa";
import { SHEET_EXIT_MS } from "@/lib/use-sheet-exit";

const SAFARI_26 = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1";
const CHROME_IOS = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/141.0.0.0 Mobile/15E148 Safari/604.1";
const INSTAGRAM_IOS = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 400.0.0";
const INSTAGRAM_ANDROID = "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Mobile Safari/537.36 Instagram 400.0.0";

const prototype = window.HTMLDialogElement.prototype;
prototype.showModal = function () { this.setAttribute("open", ""); };
prototype.close = function () { this.removeAttribute("open"); };

async function wait(ms = 600) {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, ms)); });
}

async function mount({ userAgent = SAFARI_26, standalone = false, pathname = "/" } = {}) {
  window.localStorage.clear();
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: standalone, addEventListener() {}, removeEventListener() {} }) });
  Object.defineProperty(navigator, "userAgent", { configurable: true, value: userAgent });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const render = (path: string) => root.render(
    <PwaRegister><PathnameContext.Provider value={path}><InstallPrompt /></PathnameContext.Provider></PwaRegister>,
  );
  await act(async () => render(pathname));
  await wait(30);
  return {
    container,
    navigate: async (path: string) => { await act(async () => render(path)); },
    cleanup: async () => { await act(async () => root.unmount()); container.remove(); },
  };
}

function offer() {
  const event = new CustomEvent(INSTALL_PROMPT_EVENT, { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}

// Compared as a boolean: a failing assert on a DOM node makes node:assert
// inspect the whole jsdom graph, which exhausts memory instead of failing.
const noDialog = (root: HTMLElement) => root.querySelector("dialog") === null;

const button = (root: HTMLElement, label: string) =>
  Array.from(root.querySelectorAll("button")).find(candidate => candidate.textContent?.startsWith(label));

test("installed, unsupported and already-offered devices leave the moment to notifications", async () => {
  for (const options of [{ standalone: true }, { userAgent: "Mozilla/5.0 (Linux; Android 15) Firefox/140.0" }]) {
    const app = await mount(options);
    try { assert.equal(offer(), false); await wait(); assert.equal(noDialog(app.container), true); }
    finally { await app.cleanup(); }
  }
  const app = await mount();
  try {
    window.localStorage.setItem(PWA_INSTALL_PROMPT_SEEN_KEY, "1");
    assert.equal(offer(), false);
  } finally { await app.cleanup(); }
});

test("Android uses the captured native prompt only from the Install click", async () => {
  const app = await mount({ userAgent: "Mozilla/5.0 (Linux; Android 15) Chrome/141.0 Mobile Safari/537.36" });
  let prompts = 0;
  try {
    const native = Object.assign(new window.Event("beforeinstallprompt", { cancelable: true }), {
      prompt: async () => { prompts++; },
      userChoice: Promise.resolve({ outcome: "dismissed" as const, platform: "web" }),
    });
    await act(async () => window.dispatchEvent(native));
    assert.equal(offer(), true);
    await wait();
    const dialog = app.container.querySelector("dialog")!;
    assert.match(dialog.textContent!, /GET UNLXCK ON YOUR PHONE/);
    assert.doesNotMatch(dialog.textContent!, /Share/);
    assert.equal(prompts, 0);
    await act(async () => button(dialog, "Install UNLXCK")!.click());
    await wait(SHEET_EXIT_MS + 50);
    assert.equal(prompts, 1);
    assert.equal(noDialog(app.container), true);
  } finally { await app.cleanup(); }
});

test("iPhone Chrome gets Chrome steps behind open modals, and claims priority over notifications once per visit", async () => {
  const app = await mount({ userAgent: CHROME_IOS });
  const review = document.createElement("dialog"); review.setAttribute("open", ""); document.body.append(review);
  let pushOffers = 0;
  const onPush = () => { pushOffers++; };
  window.addEventListener(PUSH_PROMPT_EVENT, onPush);
  try {
    offerPushNotifications("token");
    await wait();
    assert.equal(noDialog(app.container), true, "never stacks on another modal");
    review.remove();
    await wait();
    const dialog = app.container.querySelector("dialog")!;
    assert.equal(pushOffers, 0, "install takes the moment instead of notifications");
    assert.match(dialog.textContent!, /Chrome’s address bar/);
    assert.match(dialog.textContent!, /View More/);
    assert.doesNotMatch(dialog.textContent!, /Safari/);
    assert.ok(window.localStorage.getItem(PWA_INSTALL_PROMPT_SEEN_KEY));
    await act(async () => button(dialog, "Got it")!.click());
    await wait(SHEET_EXIT_MS + 50);
    assert.equal(noDialog(app.container), true);
    offerPushNotifications("token");
    await wait();
    assert.equal(pushOffers, 0, "notifications wait for a later visit");
    assert.equal(noDialog(app.container), true);
  } finally { review.remove(); window.removeEventListener(PUSH_PROMPT_EVENT, onPush); await app.cleanup(); }
});

test("iPhone Safari 26 shows the ••• → Share → Add to Home Screen route and closes with Maybe later", async () => {
  const app = await mount();
  try {
    // An existing user who once closed the Settings sheet still gets the offer.
    window.localStorage.setItem(PWA_INSTALL_GUIDE_DISMISSED_KEY, "1");
    assert.equal(offer(), true);
    await wait();
    const dialog = app.container.querySelector("dialog")!;
    for (const text of [/Tap ••• in Safari’s toolbar/, /Select Share/, /Tap Add to Home Screen/, /View More/, /Open as Web App/]) assert.match(dialog.textContent!, text);
    assert.doesNotMatch(dialog.textContent!, /installed/i);
    await act(async () => button(dialog, "Maybe later")!.click());
    await wait(SHEET_EXIT_MS + 50);
    assert.equal(noDialog(app.container), true);
    assert.equal(offer(), false, "a dismissed prompt is not offered again");
  } finally { await app.cleanup(); }
});

test("Instagram entry reuses the branded guide without consuming the later install offer", async () => {
  window.sessionStorage.clear();
  const app = await mount({ userAgent: INSTAGRAM_IOS });
  try {
    await wait();
    const dialog = app.container.querySelector("dialog")!;
    assert.ok(dialog);
    assert.match(dialog.textContent!, /GET UNLXCK ON YOUR PHONE/);
    assert.match(dialog.textContent!, /Open UNLXCK outside Instagram/);
    assert.match(dialog.textContent!, /Tap ••• in Instagram/);
    assert.match(dialog.textContent!, /Choose Open in external browser/);
    assert.match(dialog.textContent!, /Copy the link instead/);
    assert.doesNotMatch(dialog.textContent!, /Tap Add to Home Screen/);
    assert.equal(offer(), false, "the ordinary install prompt is not offered inside Instagram");
    assert.equal(window.localStorage.getItem(PWA_INSTALL_PROMPT_SEEN_KEY), null);
    assert.equal(window.sessionStorage.getItem(PWA_INSTAGRAM_EXIT_SEEN_KEY), "1");
    await act(async () => button(dialog, "Maybe later")!.click());
    await wait(SHEET_EXIT_MS + 50);
  } finally { await app.cleanup(); }
  const returnVisit = await mount({ userAgent: INSTAGRAM_IOS });
  try {
    await wait();
    assert.equal(noDialog(returnVisit.container), true, "no repeat in the same Instagram browser session");
  } finally { await returnVisit.cleanup(); window.sessionStorage.clear(); }
});

test("Android Instagram gets the exit instructions without a native install prompt", async () => {
  window.sessionStorage.clear();
  const app = await mount({ userAgent: INSTAGRAM_ANDROID });
  try {
    await wait();
    assert.match(app.container.querySelector("dialog")?.textContent ?? "", /Choose Open in external browser/);
    assert.equal(window.localStorage.getItem(PWA_INSTALL_PROMPT_SEEN_KEY), null);
  } finally { await app.cleanup(); window.sessionStorage.clear(); }
});

test("Instagram entry waits for existing modals and excludes workouts and installed mode", async () => {
  window.sessionStorage.clear();
  const safety = document.createElement("dialog");
  safety.setAttribute("open", "");
  document.body.append(safety);
  const app = await mount({ userAgent: INSTAGRAM_IOS });
  try {
    await wait();
    assert.equal(noDialog(app.container), true);
    safety.remove();
    await wait();
    assert.match(app.container.querySelector("dialog")?.textContent ?? "", /Open UNLXCK outside Instagram/);
  } finally { safety.remove(); await app.cleanup(); window.sessionStorage.clear(); }

  const working = await mount({ userAgent: INSTAGRAM_IOS, pathname: "/today" });
  try {
    await wait();
    assert.equal(noDialog(working.container), true);
    await working.navigate("/plans/plan-1");
    await wait();
    assert.match(working.container.querySelector("dialog")?.textContent ?? "", /Open UNLXCK outside Instagram/);
  } finally { await working.cleanup(); window.sessionStorage.clear(); }

  const installed = await mount({ userAgent: INSTAGRAM_IOS, standalone: true });
  try {
    await wait();
    assert.equal(noDialog(installed.container), true);
  } finally { await installed.cleanup(); window.sessionStorage.clear(); }
});

test("a deep-linked plan opens the guide and Copy link strips sensitive URL parameters", async () => {
  window.sessionStorage.clear();
  window.history.replaceState({}, "", "/plans/demo?code=secret#access_token=secret");
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  let copied = "";
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
    writeText: async (value: string) => { copied = value; },
  } });
  const app = await mount({ userAgent: INSTAGRAM_IOS, pathname: "/plans/demo" });
  try {
    await wait();
    const dialog = app.container.querySelector("dialog")!;
    assert.match(dialog.textContent!, /Choose Open in external browser/);
    await act(async () => button(dialog, "Copy link")!.click());
    assert.equal(copied, "http://localhost/plans/demo");
    assert.match(dialog.querySelector('[role="status"]')?.textContent ?? "", /Paste it into your phone/);
    assert.match(button(dialog, "Link copied")?.textContent ?? "", /Link copied/);
  } finally {
    await app.cleanup();
    window.history.replaceState({}, "", "/");
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
    window.sessionStorage.clear();
  }
});

test("clipboard failure explains the manual option without claiming success", async () => {
  window.sessionStorage.clear();
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
    writeText: async () => { throw new Error("blocked"); },
  } });
  const app = await mount({ userAgent: INSTAGRAM_IOS });
  try {
    await wait();
    const dialog = app.container.querySelector("dialog")!;
    await act(async () => button(dialog, "Copy link")!.click());
    assert.match(dialog.querySelector('[role="status"]')?.textContent ?? "", /Couldn’t copy automatically/);
    assert.ok(button(dialog, "Copy link"));
  } finally {
    await app.cleanup();
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
    window.sessionStorage.clear();
  }
});
