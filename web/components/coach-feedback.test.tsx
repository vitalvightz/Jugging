import assert from "node:assert/strict";
import test from "node:test";
import { window } from "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { CoachFeedback } from "./coach-feedback";

const original = { id: "note", coach_name: "Sam", note: "Control the descent.", exercise_ref: "squat", updated_at: "2026-11-16T12:00:00Z" };
async function mount(editable: boolean, denySave = false) {
  const host = document.createElement("div"); document.body.append(host);
  const root = createRoot(host);
  const before = globalThis.fetch;
  let notes = [original];
  const writes: Record<string, unknown>[] = [];
  globalThis.fetch = async (_input, init) => {
    if (init?.method === "POST") {
      writes.push(JSON.parse(String(init.body)));
      if (denySave) return new Response(JSON.stringify({ message: "Access ended" }), { status: 403 });
      notes = [{ ...original, note: String(writes.at(-1)!.p_note) }];
      return new Response(JSON.stringify("note"));
    }
    return new Response(JSON.stringify(notes));
  };
  await act(async () => root.render(<CoachFeedback token="token" athleteId="fighter" source="unlxck" targetId="session" exerciseRef="squat" editable={editable} />));
  const click = async (label: string) => {
    const button = [...host.querySelectorAll("button")].find(button => button.textContent === label);
    assert.ok(button, label); await act(async () => button.click());
  };
  return { host, writes, click, close() { act(() => root.unmount()); host.remove(); globalThis.fetch = before; } };
}

test("athletes see received coach feedback and attribution without editing controls", async () => {
  const ui = await mount(false);
  try {
    assert.match(ui.host.textContent!, /Coach Sam/);
    assert.match(ui.host.textContent!, /Control the descent/);
    assert.equal(ui.host.querySelectorAll("button,textarea").length, 0);
  } finally { ui.close(); }
});

for (const deny of [false, true]) {
  test(`coach edits only the targeted exercise feedback; save denied=${deny}`, async () => {
    const ui = await mount(true, deny);
    try {
      await ui.click("Edit feedback");
      const input = ui.host.querySelector("textarea")!;
      await act(async () => {
        Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")!.set!.call(input, "Good depth today.");
        input.dispatchEvent(new window.Event("input", { bubbles: true }));
      });
      await act(async () => ui.host.querySelector("form")!.requestSubmit());
      assert.deepEqual(ui.writes[0], { p_athlete_id: "fighter", p_source: "unlxck", p_target_id: "session", p_exercise_ref: "squat", p_note: "Good depth today." });
      if (deny) {
        assert.equal(ui.host.querySelector("textarea")!.value, "Good depth today.");
        assert.match(ui.host.textContent!, /Control the descent/);
        assert.match(ui.host.textContent!, /feedback unavailable/);
      } else {
        assert.equal(ui.host.querySelector("textarea"), null);
        assert.match(ui.host.textContent!, /Good depth today/);
      }
    } finally { ui.close(); }
  });
}
