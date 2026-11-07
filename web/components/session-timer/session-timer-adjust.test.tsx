import test, { afterEach } from "node:test";
import assert from "node:assert/strict";

import "../test-dom";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { SessionTimer } from "./session-timer";
import type { TimerItem } from "@/lib/session-timer/plan";

const ROUNDS: TimerItem[] = [
  {
    kind: "interval",
    id: "free-rounds",
    title: "Rounds",
    detail: null,
    blockType: null,
    rounds: 3,
    workSec: 180,
    restSec: 60,
    sparring: false,
    needsSetup: false,
  },
];

let root: Root | null = null;
afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.replaceChildren();
  window.localStorage.clear();
});

async function openAdjustSheet(onMinimize: () => void = () => undefined) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () =>
    root?.render(
      <SessionTimer
        items={ROUNDS}
        storageKey="adjust-test"
        sessionTitle="Round timer"
        visible
        onMinimize={onMinimize}
        onExpand={() => undefined}
        onFinish={() => undefined}
        onClose={() => undefined}
      />,
    ),
  );
  const summary = document.querySelector<HTMLButtonElement>(".st-plan-summary");
  assert.ok(summary);
  await act(async () => summary.click());
  assert.ok(document.querySelector('[aria-label="Adjust timer"]'));
}

function stepperValue(label: string): string {
  const row = [...document.querySelectorAll(".st-stepper")].find(
    (el) => el.querySelector(".st-stepper-label")?.textContent === label,
  );
  return row?.querySelector(".st-stepper-value")?.textContent ?? "";
}

function stepperButton(label: string, name: string): HTMLButtonElement {
  const button = document.querySelector<HTMLButtonElement>(`[aria-label="${name} ${label.toLowerCase()}"]`);
  assert.ok(button, `${name} ${label}`);
  return button;
}

/** React only sees typing through the native value setter plus an input event. */
function type(input: HTMLInputElement, text: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, text);
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
}

async function tapValue(label: string) {
  const value = [...document.querySelectorAll<HTMLButtonElement>("button.st-stepper-value")].find((el) =>
    el.getAttribute("aria-label")?.startsWith(`${label} `),
  );
  assert.ok(value, `${label} value`);
  await act(async () => value.click());
}

test("the first round timer points to settings and retires the hint when opened", async () => {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  const timer = <SessionTimer items={ROUNDS} storageKey="settings-hint-test" sessionTitle="Round timer" visible
    onMinimize={() => undefined} onExpand={() => undefined} onFinish={() => undefined} onClose={() => undefined} />;
  await act(async () => root?.render(timer));
  assert.match(document.querySelector(".st-hint")?.textContent ?? "", /change timer settings/);
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Timer settings"]')?.click());
  assert.ok(document.querySelector('[aria-label="Timer settings"][aria-expanded="true"]'));
  assert.equal(document.querySelector(".st-hint"), null);
  assert.equal(window.localStorage.getItem("st-settings-hint-seen"), "1");
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Close settings"]')?.click());
  const start = [...document.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === "Start round 1");
  assert.ok(start);
  await act(async () => start.click());
  assert.match(document.querySelector(".st-hint")?.textContent ?? "", /leave and return later/);
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Minimise timer"]')?.click());
  assert.equal(window.localStorage.getItem("st-minimize-hint-seen"), "1");
});

test("typing a round length and rest sets them exactly", async () => {
  await openAdjustSheet();

  await tapValue("Round length");
  const minutes = document.querySelector<HTMLInputElement>('input[aria-label="round length minutes"]');
  const seconds = document.querySelector<HTMLInputElement>('input[aria-label="round length seconds"]');
  assert.ok(minutes && seconds);
  assert.equal(minutes.value, "3");
  assert.equal(seconds.value, "00");
  await act(async () => {
    type(minutes, "2");
    type(seconds, "30");
  });
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Set round length"]')?.click());
  assert.equal(stepperValue("Round length"), "2:30");

  await tapValue("Rest");
  await act(async () => {
    type(document.querySelector<HTMLInputElement>('input[aria-label="rest minutes"]')!, "1");
    type(document.querySelector<HTMLInputElement>('input[aria-label="rest seconds"]')!, "05");
  });
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Set rest"]')?.click());
  assert.equal(stepperValue("Rest"), "1:05");

  // The steppers snap an odd value back onto 15 s marks.
  await act(async () => stepperButton("Rest", "Increase").click());
  assert.equal(stepperValue("Rest"), "1:15");
  await act(async () => stepperButton("Rest", "Decrease").click());
  assert.equal(stepperValue("Rest"), "1:00");
});

test("typing a round count sets it, and nonsense changes nothing", async () => {
  await openAdjustSheet();

  await tapValue("Rounds");
  const count = document.querySelector<HTMLInputElement>('input[aria-label="rounds"]');
  assert.ok(count);
  await act(async () => type(count, "12"));
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Set rounds"]')?.click());
  assert.equal(stepperValue("Rounds"), "12");

  await tapValue("Rounds");
  await act(async () => type(document.querySelector<HTMLInputElement>('input[aria-label="rounds"]')!, "abc"));
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Set rounds"]')?.click());
  assert.equal(stepperValue("Rounds"), "12");
});

test("Escape leaves the typed value unchanged and does not minimise the timer", async () => {
  let minimised = 0;
  await openAdjustSheet(() => {
    minimised += 1;
  });

  await tapValue("Round length");
  const minutes = document.querySelector<HTMLInputElement>('input[aria-label="round length minutes"]');
  assert.ok(minutes);
  await act(async () => type(minutes, "9"));
  await act(async () => {
    minutes.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  assert.equal(stepperValue("Round length"), "3:00");
  assert.equal(minimised, 0);
});
