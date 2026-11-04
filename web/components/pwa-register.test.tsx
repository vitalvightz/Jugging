import assert from "node:assert/strict";
import test from "node:test";

import "./test-dom";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { InstallUnlxck } from "./install-unlxck";
import { PwaRegister } from "./pwa-register";
import { ToastProvider } from "./toast-provider";

function mount(): { container: HTMLElement; root: Root } {
  const container = document.createElement("div");
  document.body.appendChild(container);
  return { container, root: createRoot(container) };
}

function cleanup(container: HTMLElement, root: Root) {
  act(() => root.unmount());
  container.remove();
  document.body.innerHTML = "";
  window.history.replaceState({}, "", "/");
}

async function settle(durationMs = 20) {
  await act(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, durationMs));
    await Promise.resolve();
    await Promise.resolve();
  });
}

function setMatchMedia(matches: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: () => ({
      matches,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  });
}

function renderInstallSurface(
  root: Root,
  environment?: string,
  variant?: "panel" | "inline",
) {
  root.render(
    <ToastProvider>
      <PwaRegister environment={environment}>
        <InstallUnlxck variant={variant} />
      </PwaRegister>
    </ToastProvider>,
  );
}

test("unsupported browsers do not see a misleading Settings install panel", async () => {
  setMatchMedia(false);
  const { container, root } = mount();
  try {
    await act(async () => renderInstallSurface(root));
    assert.equal(container.querySelector('[data-testid="install-unlxck"]'), null);
    await settle();
    assert.equal(container.querySelector('[data-testid="install-unlxck"]'), null);
    assert.doesNotMatch(container.textContent ?? "", /browser menu|install app/i);
  } finally {
    cleanup(container, root);
  }
});

test("install state renders nothing while capability detection is pending", async () => {
  setMatchMedia(false);
  const { container, root } = mount();
  try {
    await act(async () => renderInstallSurface(root));
    assert.equal(container.querySelector('[data-testid="install-unlxck"]'), null);
  } finally {
    cleanup(container, root);
  }
});

test("installed standalone mode hides the Settings install action", async () => {
  setMatchMedia(true);
  const { container, root } = mount();
  try {
    await act(async () => renderInstallSurface(root));
    await settle();
    assert.equal(container.querySelector('[data-testid="install-unlxck"]'), null);
  } finally {
    cleanup(container, root);
  }
});

test("iPhone Settings action shows Safari's own Add to Home Screen flow", async () => {
  setMatchMedia(false);
  const userAgentDescriptor = Object.getOwnPropertyDescriptor(navigator, "userAgent");
  const touchDescriptor = Object.getOwnPropertyDescriptor(navigator, "maxTouchPoints");
  Object.defineProperty(navigator, "userAgent", {
    configurable: true,
    value: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1",
  });
  Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 5 });
  const { container, root } = mount();
  try {
    await act(async () => renderInstallSurface(root));
    await settle();
    const trigger = Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => button.textContent === "View iPhone steps",
    );
    assert.ok(trigger);
    await act(async () => trigger.click());
    // The sheet is portaled to <body> so fixed positioning cannot be captured
    // by transformed ancestors — it must exist but live outside the container.
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
    assert.ok(dialog);
    assert.equal(container.contains(dialog), false);
    const dialogText = dialog?.textContent ?? "";
    assert.match(dialogText, /Tap ••• in Safari’s toolbar/);
    assert.match(dialogText, /Select Share/);
    assert.match(dialogText, /Tap Add to Home Screen/);
    assert.match(dialogText, /Open as Web App/);
    assert.doesNotMatch(dialogText, /Chrome|browser menu|Install app/i);

    const close = dialog?.querySelector<HTMLButtonElement>(
      '[aria-label="Close install instructions"]',
    );
    const done = Array.from(dialog?.querySelectorAll<HTMLButtonElement>("button") ?? []).find(
      (button) => button.textContent === "Done",
    );
    assert.ok(close);
    assert.ok(done);
    assert.equal(document.activeElement, close);
    await act(async () => {
      window.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Tab", shiftKey: true }));
    });
    assert.equal(document.activeElement, done);

    const outsideButton = document.createElement("button");
    document.body.appendChild(outsideButton);
    outsideButton.focus();
    await act(async () => {
      window.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Tab" }));
    });
    assert.equal(document.activeElement, close);
    outsideButton.remove();
  } finally {
    cleanup(container, root);
    if (userAgentDescriptor) Object.defineProperty(navigator, "userAgent", userAgentDescriptor);
    else Reflect.deleteProperty(navigator, "userAgent");
    if (touchDescriptor) Object.defineProperty(navigator, "maxTouchPoints", touchDescriptor);
    else Reflect.deleteProperty(navigator, "maxTouchPoints");
  }
});

test("inline install variant gives iPhone Chrome its own steps", async () => {
  setMatchMedia(false);
  const userAgentDescriptor = Object.getOwnPropertyDescriptor(navigator, "userAgent");
  Object.defineProperty(navigator, "userAgent", {
    configurable: true,
    value: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/141.0.0.0 Mobile/15E148 Safari/604.1",
  });
  const { container, root } = mount();
  try {
    await act(async () => renderInstallSurface(root, undefined, "inline"));
    await settle();
    const strip = container.querySelector('[data-testid="install-unlxck-inline"]');
    assert.ok(strip);
    const trigger = Array.from(strip.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => button.textContent === "View iPhone steps",
    );
    assert.ok(trigger);
    await act(async () => trigger.click());
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
    assert.ok(dialog);
    assert.equal(container.contains(dialog), false);
    assert.match(dialog.textContent ?? "", /Chrome’s address bar/);
    assert.match(dialog.textContent ?? "", /Add to Home Screen/);
    assert.doesNotMatch(dialog.textContent ?? "", /Safari/);
  } finally {
    cleanup(container, root);
    if (userAgentDescriptor) Object.defineProperty(navigator, "userAgent", userAgentDescriptor);
    else Reflect.deleteProperty(navigator, "userAgent");
  }
});

test("an in-app iPhone browser shows Instagram exit steps instead of Safari install steps", async () => {
  setMatchMedia(false);
  const userAgentDescriptor = Object.getOwnPropertyDescriptor(navigator, "userAgent");
  Object.defineProperty(navigator, "userAgent", {
    configurable: true,
    value: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 400.0.0",
  });
  const { container, root } = mount();
  try {
    await act(async () => renderInstallSurface(root));
    await settle();
    const trigger = Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => button.textContent === "View browser steps",
    );
    assert.ok(trigger);
    await act(async () => trigger.click());
    assert.match(document.querySelector('[role="dialog"]')?.textContent ?? "", /Open UNLXCK in your phone’s browser/);
    assert.match(document.querySelector('[role="dialog"]')?.textContent ?? "", /Choose Open in external browser/);
  } finally {
    cleanup(container, root);
    if (userAgentDescriptor) Object.defineProperty(navigator, "userAgent", userAgentDescriptor);
    else Reflect.deleteProperty(navigator, "userAgent");
  }
});

test("captured Chromium install prompt is used only after an explicit click", async () => {
  setMatchMedia(false);
  let promptCalls = 0;
  const { container, root } = mount();
  try {
    await act(async () => renderInstallSurface(root));
    await settle();
    assert.equal(container.querySelector('[data-testid="install-unlxck"]'), null);

    const event = Object.assign(new window.Event("beforeinstallprompt", { cancelable: true }), {
      prompt: async () => {
        promptCalls += 1;
      },
      userChoice: Promise.resolve({ outcome: "accepted" as const, platform: "web" }),
    });
    await act(async () => window.dispatchEvent(event));

    const trigger = Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => button.textContent === "Install UNLXCK",
    );
    assert.ok(trigger);
    assert.equal(promptCalls, 0);
    await act(async () => trigger.click());
    await settle();
    assert.equal(promptCalls, 1);
    await act(async () => window.dispatchEvent(new window.Event("appinstalled")));
    await settle();
    assert.equal(container.querySelector('[data-testid="install-unlxck"]'), null);
  } finally {
    cleanup(container, root);
  }
});

test("a rejected native prompt hides the panel instead of inventing manual steps", async () => {
  setMatchMedia(false);
  const { container, root } = mount();
  try {
    await act(async () => renderInstallSurface(root));
    await settle();
    const event = Object.assign(new window.Event("beforeinstallprompt", { cancelable: true }), {
      prompt: async () => {
        throw new Error("stale install prompt");
      },
      userChoice: Promise.resolve({ outcome: "dismissed" as const, platform: "web" }),
    });
    await act(async () => window.dispatchEvent(event));
    const trigger = Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => button.textContent === "Install UNLXCK",
    );
    assert.ok(trigger);
    await act(async () => trigger.click());
    await settle();
    assert.equal(container.querySelector('[data-testid="install-unlxck"]'), null);
    assert.equal(document.querySelector('[role="dialog"]'), null);
  } finally {
    cleanup(container, root);
  }
});

test("Android Instagram Settings keeps a manual exit route and working Copy link", async () => {
  setMatchMedia(false);
  const userAgent = Object.getOwnPropertyDescriptor(navigator, "userAgent");
  const clipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "userAgent", { configurable: true,
    value: "Mozilla/5.0 (Linux; Android 15) Chrome/141.0 Mobile Safari/537.36 Instagram 400.0.0" });
  let copied = "";
  Object.defineProperty(navigator, "clipboard", { configurable: true,
    value: { writeText: async (value: string) => { copied = value; } } });
  const { container, root } = mount();
  try {
    await act(async () => renderInstallSurface(root));
    await settle();
    const trigger = Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => button.textContent === "View browser steps",
    );
    assert.ok(trigger, "Android Instagram must offer Settings guidance without native install support");
    await act(async () => trigger.click());
    const sheet = document.querySelector<HTMLElement>('[role="dialog"]');
    assert.ok(sheet);
    assert.match(sheet.textContent ?? "", /Choose Open in external browser/);
    assert.doesNotMatch(sheet.textContent ?? "", /Tap Add to Home Screen/);
    const copy = Array.from(sheet.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => button.textContent === "Copy link",
    );
    assert.ok(copy);
    await act(async () => copy.click());
    assert.equal(copied, "http://localhost/");
    assert.match(sheet.querySelector('[role="status"]')?.textContent ?? "", /Paste it into your phone/);
  } finally {
    cleanup(container, root);
    if (userAgent) Object.defineProperty(navigator, "userAgent", userAgent);
    else Reflect.deleteProperty(navigator, "userAgent");
    if (clipboard) Object.defineProperty(navigator, "clipboard", clipboard);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});
