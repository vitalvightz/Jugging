import assert from "node:assert/strict";
import test from "node:test";
import { window } from "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { AppRouterContext } from "next/dist/shared/lib/app-router-context.shared-runtime";
import { PathnameContext, SearchParamsContext } from "next/dist/shared/lib/hooks-client-context.shared-runtime";
import { AppSessionContext } from "./auth-provider";
import { QuickBuildForm } from "./quick-build-form";
import messages from "../messages/en.json";
import { emptyPlanRequest } from "@/lib/onboarding";
import type { MeResponse } from "@/lib/types";

async function mountSchedule() {
  const originalScrollTo = window.scrollTo;
  const originalFrame = globalThis.requestAnimationFrame;
  const originalScrollIntoView = window.HTMLElement.prototype.scrollIntoView;
  window.scrollTo = () => {};
  globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
  window.HTMLElement.prototype.scrollIntoView = () => {};
  const draft = { ...emptyPlanRequest(), weekly_training_frequency: 6, training_availability: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"] };
  const me = { profile: { id: "fixture-athlete", full_name: "Fixture athlete", role: "athlete",
    health_consent_granted: true, onboarding_draft: draft }, latest_intake: null } as unknown as MeResponse;
  const router = { push() {}, replace() {}, refresh() {}, back() {}, forward() {}, prefetch() {}, bfcacheId: "fixture" };
  const session = { isReady: true, isMeHydrated: true, hasTransientMeError: false,
    session: { access_token: "fixture-token" }, me, previewAppearanceMode() {}, refreshMe: async () => {},
    replaceMe() {}, signOut: async () => {} };
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => root.render(
    <NextIntlClientProvider locale="en" timeZone="Europe/London" messages={messages}>
      <AppRouterContext.Provider value={router}>
        <PathnameContext.Provider value="/onboarding"><SearchParamsContext.Provider value={new URLSearchParams()}>
          <AppSessionContext.Provider value={session}><QuickBuildForm /></AppSessionContext.Provider>
        </SearchParamsContext.Provider></PathnameContext.Provider>
      </AppRouterContext.Provider>
    </NextIntlClientProvider>,
  ));
  const press = async (text: string) => {
    const button = Array.from(host.querySelectorAll("button")).find((entry) => entry.textContent?.trim() === text);
    assert.ok(button, text);
    await act(async () => button.click());
  };
  return { host, press, cleanup: async () => {
    await act(async () => root.unmount()); host.remove();
    window.scrollTo = originalScrollTo;
    if (originalFrame) globalThis.requestAnimationFrame = originalFrame;
    else Reflect.deleteProperty(globalThis, "requestAnimationFrame");
    if (originalScrollIntoView) window.HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
    else Reflect.deleteProperty(window.HTMLElement.prototype, "scrollIntoView");
  } };
}

test("quick build never offers more than selected training days", async () => {
  const ui = await mountSchedule();
  try {
    const frequency = ui.host.querySelector<HTMLButtonElement>("#qb-weekly-frequency");
    assert.ok(frequency);
    await act(async () => frequency.click());
    const choices = Array.from(document.querySelectorAll<HTMLButtonElement>('[role="option"]'));
    assert.ok(choices.some(option => option.textContent?.trim() === "6"));
    assert.equal(choices.some(option => option.textContent?.trim() === "7"), false);
    assert.equal(choices.some(option => option.textContent?.trim() === "12"), false);
    const six = choices.find(option => option.textContent?.trim() === "6");
    assert.ok(six);
    await act(async () => six.click());
    assert.equal(frequency.textContent?.trim(), "6");
    const label = Array.from(ui.host.querySelectorAll(".checkbox-group-label")).find(entry => entry.textContent === "Days you can train");
    assert.ok(label?.parentElement);
    const saturday = Array.from(label.parentElement.querySelectorAll("label")).find(entry => entry.textContent?.trim() === "Saturday")?.querySelector<HTMLInputElement>("input");
    assert.ok(saturday);
    await act(async () => saturday.click());
    assert.equal(saturday.checked, false);
    await act(async () => frequency.click());
    assert.equal(Array.from(document.querySelectorAll<HTMLButtonElement>('[role="option"]')).some(option => option.textContent?.trim() === "6"), false);
  } finally { await ui.cleanup(); }
});


test("quick build disables the shared weight cut goal", async () => {
  const ui = await mountSchedule();
  try {
    const card = Array.from(ui.host.querySelectorAll<HTMLLabelElement>("label.checkbox-card"))
      .find(label => label.textContent?.includes("Weight Cut"));
    assert.ok(card);
    assert.equal(card.querySelector("input")?.disabled, true);
  } finally { await ui.cleanup(); }
});
