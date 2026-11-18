import assert from "node:assert/strict";
import test from "node:test";
import "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { TrainingVideos, videoProblem } from "./training-videos";

const video = (id: string) => ({ id, created_at: "2026-11-18T10:00:00Z", media: {
  id: `media-${id}`, provider: "uploaded", video_id: null, asset_id: `asset-${id}`, start_s: 0, end_s: 12,
  channel_title: null, orientation: "portrait", url: `https://signed.example/${id}.mp4`,
} });

async function mount(element: (token: string) => React.ReactElement, { uploads = true, rows = [video("a"), video("b")] } = {}) {
  const host = document.createElement("div"); document.body.append(host);
  const root = createRoot(host);
  const before = globalThis.fetch;
  const calls: { url: string; method: string }[] = [];
  let current = rows;
  globalThis.fetch = async (input, init) => {
    const url = String(input instanceof Request ? input.url : input);
    const method = init?.method ?? "GET";
    calls.push({ url, method });
    if (url.includes("/api/private-media/settings")) return Response.json({ uploads_available: uploads, youtube_available: false });
    if (method === "DELETE") { current = current.filter(row => !url.endsWith(row.id)); return new Response(null, { status: 204 }); }
    return Response.json(current);
  };
  // Settings are cached per token, so each test signs in fresh.
  const token = `token-${Math.random()}`;
  await act(async () => root.render(element(token)));
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
  const button = (label: string) => [...host.querySelectorAll("button")].find(item => item.textContent === label || item.getAttribute("aria-label") === label);
  return { host, calls, button, root, close() { act(() => root.unmount()); host.remove(); globalThis.fetch = before; } };
}

test("an athlete sees their videos on an exercise, a single-line Attach, and the count", async () => {
  const ui = await mount(token => <TrainingVideos token={token} label="Back squat"
    target={{ source: "external", target_id: "session", exercise_ref: "squat" }} />);
  try {
    const list = ui.calls.find(call => call.url.includes("/api/training/attachments?"));
    assert.ok(list?.url.includes("source=external") && list.url.includes("target_id=session") && list.url.includes("exercise_ref=squat"));
    assert.equal(ui.host.querySelectorAll("ul li").length, 2);
    assert.ok(ui.button("Attach video"), "attach button");
    assert.match(ui.host.textContent ?? "", /2 of 6/);
    assert.ok(ui.host.querySelector('input[type="file"][accept*="video/mp4"]'));
  } finally { ui.close(); }
});

test("opening a video re-reads fresh links, and the athlete can remove it", async () => {
  const ui = await mount(token => <TrainingVideos token={token} label="Back squat" target={{ source: "external", target_id: "session" }} />);
  try {
    const reads = () => ui.calls.filter(call => call.method === "GET" && call.url.includes("/api/training/attachments?")).length;
    const before = reads();
    await act(async () => ui.button("Play video 1 for Back squat")!.click());
    assert.equal(reads(), before + 1);
    assert.ok(ui.host.querySelector("video.ex-demo-player"));
    await act(async () => ui.button("Remove video")!.click());
    assert.ok(ui.calls.some(call => call.method === "DELETE" && call.url.endsWith("/api/training/attachments/a")));
    assert.equal(ui.host.querySelectorAll("ul li").length, 1);
    assert.equal(ui.host.querySelector("video.ex-demo-player"), null);
  } finally { ui.close(); }
});

test("a coach watches through the consented coach route and gets no attach or remove controls", async () => {
  const ui = await mount(token => <TrainingVideos token={token} viewer="coach" athleteId="fighter" label="Sparring"
    target={{ source: "sparring", target_id: "report" }} />);
  try {
    assert.ok(ui.calls.some(call => call.url.includes("/api/coach/fighters/fighter/training/attachments?")));
    assert.ok(!ui.calls.some(call => call.url.includes("/api/private-media/settings")));
    assert.equal(ui.button("Attach video"), undefined);
    await act(async () => ui.button("Play video 1 for Sparring")!.click());
    assert.equal(ui.button("Remove video"), undefined);
  } finally { ui.close(); }
});

test("nothing renders when there are no videos and uploads are off", async () => {
  const ui = await mount(token => <TrainingVideos token={token} label="Back squat" target={{ source: "external", target_id: "session" }} />,
    { uploads: false, rows: [] });
  try { assert.equal(ui.host.innerHTML, ""); } finally { ui.close(); }
});

test("six videos fill a record, so Attach goes away", async () => {
  const ui = await mount(token => <TrainingVideos token={token} label="Back squat" target={{ source: "external", target_id: "session" }} />,
    { rows: ["a", "b", "c", "d", "e", "f"].map(video) });
  try {
    assert.equal(ui.button("Attach video"), undefined);
    assert.match(ui.host.textContent ?? "", /6 of 6/);
  } finally { ui.close(); }
});

test("only short phone videos are accepted before uploading", () => {
  const file = (type: string, size: number) => ({ type, size }) as File;
  assert.equal(videoProblem(file("video/quicktime", 5_000_000)), null);
  assert.match(videoProblem(file("image/png", 10))!, /MP4, MOV or WebM/);
  assert.match(videoProblem(file("video/mp4", 21 * 1024 * 1024))!, /over 20 MB/);
});
