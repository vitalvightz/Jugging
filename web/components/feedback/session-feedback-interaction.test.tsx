import test from "node:test";
import assert from "node:assert/strict";

import "../test-dom";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { SessionFeedbackPrompt } from "./session-feedback-prompt";

function mount(): { container: HTMLElement; root: Root } {
  const container = document.createElement("div");
  document.body.appendChild(container);
  return { container, root: createRoot(container) };
}

function cleanup(container: HTMLElement, root: Root) {
  act(() => root.unmount());
  container.remove();
}

async function settle() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function savedRecord() {
  return {
    id: "feedback-1",
    surface: "session",
    category: "session_review",
    response: null,
    reason: null,
    comment: "",
    structured_response: { difficulty: "too_hard" },
    priority: "normal",
    has_screenshot: false,
    created_at: "2026-08-06T09:00:00Z",
    updated_at: "2026-08-06T09:00:00Z",
  };
}

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const match = Array.from(container.querySelectorAll("button")).find(
    (button) => button.textContent?.trim() === text,
  );
  assert.ok(match, `expected a "${text}" button`);
  return match as HTMLButtonElement;
}

function renderPrompt(root: Root, onDismiss: () => void) {
  act(() => {
    root.render(
      <SessionFeedbackPrompt
        token="test-token"
        planId="plan-1"
        sessionId="session-1"
        onDismiss={onDismiss}
      />,
    );
  });
}

test("the first stage is one question and two buttons, never a form", async () => {
  const { container, root } = mount();
  renderPrompt(root, () => {});

  assert.match(container.textContent ?? "", /How did this session feel\?/);
  assert.equal(container.querySelectorAll("textarea").length, 0);
  assert.equal(container.querySelectorAll("input").length, 0);
  assert.ok(buttonByText(container, "Give feedback"));
  assert.ok(buttonByText(container, "Not now"));

  cleanup(container, root);
});

test("Not now dismisses without sending anything", async () => {
  const originalFetch = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = (async () => {
    requests += 1;
    return new Response("{}", { status: 200 });
  }) as typeof fetch;

  const { container, root } = mount();
  let dismissed = false;
  renderPrompt(root, () => {
    dismissed = true;
  });

  act(() => {
    buttonByText(container, "Not now").click();
  });
  await settle();

  assert.equal(dismissed, true);
  assert.equal(requests, 0);

  globalThis.fetch = originalFetch;
  cleanup(container, root);
});

test("opting in shows the three quick questions with the text box still tucked away", async () => {
  const { container, root } = mount();
  renderPrompt(root, () => {});

  act(() => {
    buttonByText(container, "Give feedback").click();
  });
  await settle();

  const text = container.textContent ?? "";
  assert.match(text, /HOW DID THAT SESSION GO\?/);
  assert.match(text, /Difficulty/);
  assert.match(text, /Instructions/);
  assert.match(text, /Plan accuracy/);
  // The optional comment and screenshot stay behind a disclosure so the three
  // taps above are the whole ask.
  assert.equal(container.querySelectorAll("textarea").length, 0);
  assert.ok(buttonByText(container, "Add a comment or screenshot"));

  cleanup(container, root);
});

test("submit stays disabled until at least one answer is given", async () => {
  const { container, root } = mount();
  renderPrompt(root, () => {});

  act(() => {
    buttonByText(container, "Give feedback").click();
  });
  await settle();

  assert.equal(buttonByText(container, "SUBMIT FEEDBACK").disabled, true);

  act(() => {
    buttonByText(container, "Too hard").click();
  });
  await settle();

  assert.equal(buttonByText(container, "SUBMIT FEEDBACK").disabled, false);

  cleanup(container, root);
});

test("answers post to the session surface as multipart form data", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; body: FormData }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    // Usage analytics fire beside the save; only feedback requests count here.
    if (String(input).includes("/api/analytics/")) return new Response(JSON.stringify({ accepted: true }), { status: 202 });
    calls.push({ url: String(input), body: init?.body as FormData });
    return new Response(JSON.stringify(savedRecord()), {
      status: 201,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;

  const { container, root } = mount();
  renderPrompt(root, () => {});

  act(() => {
    buttonByText(container, "Give feedback").click();
  });
  await settle();
  act(() => {
    buttonByText(container, "Too hard").click();
    buttonByText(container, "Unclear").click();
  });
  await settle();
  act(() => {
    buttonByText(container, "SUBMIT FEEDBACK").click();
  });
  await settle();

  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/api\/feedback\/session$/);
  assert.equal(calls[0].body.get("plan_id"), "plan-1");
  assert.equal(calls[0].body.get("session_id"), "session-1");
  assert.equal(calls[0].body.get("difficulty"), "too_hard");
  assert.equal(calls[0].body.get("instructions"), "unclear");
  // An unanswered question is sent empty rather than as a default the athlete
  // never chose.
  assert.equal(calls[0].body.get("plan_accuracy"), "");
  assert.match(container.textContent ?? "", /Feedback sent/);

  globalThis.fetch = originalFetch;
  cleanup(container, root);
});

test("tapping the selected chip again clears that answer", async () => {
  const { container, root } = mount();
  renderPrompt(root, () => {});

  act(() => {
    buttonByText(container, "Give feedback").click();
  });
  await settle();
  act(() => {
    buttonByText(container, "Appropriate").click();
  });
  await settle();
  assert.equal(buttonByText(container, "Appropriate").getAttribute("aria-pressed"), "true");

  act(() => {
    buttonByText(container, "Appropriate").click();
  });
  await settle();

  assert.equal(buttonByText(container, "Appropriate").getAttribute("aria-pressed"), "false");
  assert.equal(buttonByText(container, "SUBMIT FEEDBACK").disabled, true);

  cleanup(container, root);
});

test("a failed submit surfaces the error and keeps the answers for a retry", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ detail: "feedback could not be saved" }), {
      status: 500,
      headers: { "content-type": "application/json" },
    })) as typeof fetch;

  const { container, root } = mount();
  renderPrompt(root, () => {});

  act(() => {
    buttonByText(container, "Give feedback").click();
  });
  await settle();
  act(() => {
    buttonByText(container, "Too hard").click();
  });
  await settle();
  act(() => {
    buttonByText(container, "SUBMIT FEEDBACK").click();
  });
  await settle();

  assert.ok(container.querySelector(".feedback-error"));
  assert.equal(buttonByText(container, "Too hard").getAttribute("aria-pressed"), "true");
  assert.equal(buttonByText(container, "SUBMIT FEEDBACK").disabled, false);

  globalThis.fetch = originalFetch;
  cleanup(container, root);
});
