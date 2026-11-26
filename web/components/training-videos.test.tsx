import assert from "node:assert/strict";
import test from "node:test";
import "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { TrainingVideos, videoProblem } from "./training-videos";

const video = (id: string, removable = true) => ({ id, created_at: "2026-11-18T10:00:00Z", kind: "video" as const,
  author: "athlete" as const, removable, media: {
  id: `media-${id}`, provider: "uploaded", video_id: null, asset_id: `asset-${id}`, start_s: 0, end_s: 12,
  channel_title: null, orientation: "portrait", url: `https://signed.example/${id}.mp4`,
} });

type Row = ReturnType<typeof video> | ReturnType<typeof file>;
const file = (id: string, kind: "photo" | "voice", author: "athlete" | "coach" = "athlete", removable = true) => ({
  id, created_at: "2026-11-18T10:00:00Z", kind, author, removable, media: null,
  file: { url: null as string | null, width: kind === "photo" ? 800 : null, height: kind === "photo" ? 600 : null,
    duration_ms: kind === "voice" ? 12_000 : null },
});

async function mount(element: (token: string) => React.ReactElement,
  { uploads = true, photos = false, voice = false, rows = [video("a"), video("b")] as Row[] } = {}) {
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
    if (url.includes("/api/attachments/settings")) return Response.json({ photos_available: photos, voice_available: voice });
    if (url.includes("/play")) {
      const id = url.split("/").at(-2)!;
      const row = current.find(item => item.id === id);
      if (!row) return Response.json({ detail: "Video not available." }, { status: 404 });
      return row.media ? Response.json({ ...row, media: { ...row.media, url: `https://signed.example/${id}.mp4` } })
        : Response.json({ ...row, file: { ...row.file, url: `https://signed.example/${id}` } });
    }
    if (method === "DELETE") { current = current.filter(row => !url.endsWith(row.id)); return new Response(null, { status: 204 }); }
    return Response.json(current);
  };
  // Settings are cached per token, so each test signs in fresh.
  const token = `token-${Math.random()}`;
  await act(async () => root.render(element(token)));
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
  // Photo and PDF viewers open in a portal on <body>, so look there too.
  const button = (label: string) => [...document.body.querySelectorAll("button")].find(item => item.textContent === label || item.getAttribute("aria-label") === label);
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

test("nothing loads until a video is asked for; then one short link for that video only", async () => {
  const ui = await mount(token => <TrainingVideos token={token} label="Back squat" target={{ source: "external", target_id: "session" }} />);
  try {
    assert.equal(ui.host.querySelector("video"), null, "no preview frames load from the list");
    assert.ok(!ui.calls.some(call => call.url.includes("/play")));
    await act(async () => ui.button("Play video 1 for Back squat")!.click());
    assert.deepEqual(ui.calls.filter(call => call.url.includes("/play")).map(call => call.url.split("/api/")[1]),
      ["training/attachments/a/play"]);
    assert.equal(ui.host.querySelector("video.ex-demo-player")?.getAttribute("src"), "https://signed.example/a.mp4");
    await act(async () => ui.button("Remove video")!.click());
    assert.ok(ui.calls.some(call => call.method === "DELETE" && call.url.endsWith("/api/training/attachments/a")));
    assert.equal(ui.host.querySelectorAll("ul li").length, 1);
    assert.equal(ui.host.querySelector("video.ex-demo-player"), null);
  } finally { ui.close(); }
});

test("fullscreen failures keep the inline player available and a successful retry clears the message", async () => {
  const ui = await mount(token => <TrainingVideos token={token} label="Back squat" target={{ source: "external", target_id: "session" }} />);
  try {
    await act(async () => ui.button("Play video 1 for Back squat")!.click());
    const player = ui.host.querySelector("video")!;
    player.requestFullscreen = async () => { throw new Error("Denied"); };
    const expand = async (label: string) => act(async () => ui.button(label)!.click());
    await expand("Open Back squat video full screen");
    assert.match(ui.host.textContent ?? "", /Full screen is unavailable/);

    Object.assign(player, { webkitEnterFullscreen() { throw new Error("Unavailable"); } });
    await expand("Full screen");
    assert.match(ui.host.textContent ?? "", /Full screen is unavailable/);
    assert.equal(ui.host.querySelector("video"), player);
    assert.ok(ui.button("Play"));

    Object.assign(player, { webkitEnterFullscreen() {} });
    await expand("Full screen");
    assert.doesNotMatch(ui.host.textContent ?? "", /Full screen is unavailable/);
  } finally { ui.close(); }
});

test("a coach watches through the consented coach route and gets no attach or remove controls", async () => {
  const ui = await mount(token => <TrainingVideos token={token} viewer="coach" athleteId="fighter" label="Sparring"
    target={{ source: "sparring", target_id: "report" }} />, { rows: [video("a", false), video("b", false)] });
  try {
    assert.ok(ui.calls.some(call => call.url.includes("/api/coach/fighters/fighter/training/attachments?")));
    assert.equal(ui.host.querySelector("video"), null);
    assert.ok(!ui.calls.some(call => call.url.includes("/api/private-media/settings")));
    assert.equal(ui.button("Attach video"), undefined);
    assert.equal(ui.button("Record voice note"), undefined, "voice notes are off");
    await act(async () => ui.button("Play video 1 for Sparring")!.click());
    assert.ok(ui.calls.some(call => call.url.includes("/api/coach/fighters/fighter/training/attachments/a/play")));
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
    { rows: ["a", "b", "c", "d", "e", "f"].map(id => video(id)) });
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


test("Attach stays hidden when the server can't confirm uploads are on", async () => {
  const host = document.createElement("div"); document.body.append(host);
  const root = createRoot(host);
  const before = globalThis.fetch;
  globalThis.fetch = async input => {
    const url = String(input instanceof Request ? input.url : input);
    if (url.includes("/api/private-media/settings")) return Response.json({ detail: "down" }, { status: 503 });
    return Response.json([video("a")]);
  };
  try {
    await act(async () => root.render(<TrainingVideos token={`token-${Math.random()}`} label="Back squat" target={{ source: "external", target_id: "session" }} />));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.ok(host.querySelector("ul li"), "existing videos still listed");
    assert.equal(host.querySelector('input[type="file"]'), null);
    assert.ok(![...host.querySelectorAll("button")].some(button => button.textContent === "Attach video"));
  } finally { act(() => root.unmount()); host.remove(); globalThis.fetch = before; }
});

test("with photos and voice notes on, Attach opens a menu of the types that are on", async () => {
  const ui = await mount(token => <TrainingVideos token={token} label="Back squat" target={{ source: "external", target_id: "session" }} />,
    { photos: true, voice: true, uploads: false, rows: [] });
  try {
    const attach = ui.button("Attach")!;
    assert.equal(attach.getAttribute("aria-haspopup"), "menu");
    await act(async () => attach.click());
    assert.deepEqual([...ui.host.querySelectorAll('[role="menuitem"]')].map(item => item.textContent), ["Photo", "Voice note"]);
    assert.ok(ui.host.querySelector('input[type="file"][accept*="image/jpeg"]'), "photo picker (camera or library on phones)");
    assert.equal(ui.host.querySelector('input[type="file"][accept*="video/"]'), null, "video stays off");
  } finally { ui.close(); }
});

test("a photo opens full screen only when tapped, through a one-off link", async () => {
  const ui = await mount(token => <TrainingVideos token={token} label="Hand wraps" target={{ source: "external", target_id: "session" }} />,
    { rows: [file("p", "photo")] });
  try {
    assert.equal(ui.host.querySelector("img"), null);
    assert.ok(!ui.calls.some(call => call.url.includes("/play")));
    await act(async () => ui.button("Open photo 1 for Hand wraps")!.click());
    const viewer = document.body.querySelector('dialog[aria-label="Photo 1 of 1"]')!;
    assert.ok(viewer, "a full-screen viewer opens");
    const image = viewer.querySelector('img[alt="Photo 1 for Hand wraps"]')!;
    assert.equal(image.getAttribute("src"), "https://signed.example/p");
    assert.equal(image.getAttribute("referrerpolicy"), "no-referrer");
    await act(async () => ui.button("Remove photo")!.click());
    assert.ok(ui.calls.some(call => call.method === "DELETE" && call.url.endsWith("/api/training/attachments/p")));
    assert.equal(document.body.querySelector("dialog"), null, "the viewer closes once its only photo is gone");
  } finally { ui.close(); }
});

test("voice notes are chat-style players that load only when pressed, labelled with who left them", async () => {
  const ui = await mount(token => <TrainingVideos token={token} label="Sparring" target={{ source: "sparring", target_id: "report" }} />,
    { rows: [file("v", "voice", "coach")] });
  try {
    // No browser audio bar, and nothing fetched until Play.
    assert.equal(ui.host.querySelector("audio[controls]"), null);
    assert.match(ui.host.textContent ?? "", /Coach.*18 Nov.*0:12/);
    assert.equal(ui.host.querySelectorAll('[role="slider"] span').length, 36, "a waveform of bars");
    assert.ok(!ui.calls.some(call => call.url.includes("/play") || call.url.includes("signed.example")));
    await act(async () => ui.button("Play voice note 1 from coach for Sparring")!.click());
    assert.ok(ui.calls.some(call => call.url.endsWith("/api/training/attachments/v/play")));
    assert.ok(ui.calls.some(call => call.url === "https://signed.example/v"), "the one short-lived link is fetched");
    assert.ok(ui.button("Remove voice note 1"), "the athlete can remove anything on their own record");
  } finally { ui.close(); }
});

test("a coach can record voice feedback when voice notes are on, and removes only their own", async () => {
  const ui = await mount(token => <TrainingVideos token={token} viewer="coach" athleteId="fighter" label="Sparring"
    target={{ source: "sparring", target_id: "report" }} />,
    { voice: true, photos: true, rows: [file("mine", "voice", "coach", true), file("theirs", "photo", "athlete", false)] });
  try {
    assert.ok(ui.button("Record voice note"), "coaches get voice only, no menu");
    assert.equal(ui.button("Attach"), undefined);
    assert.equal(ui.host.querySelector('input[type="file"]'), null);
    await act(async () => ui.button("Open photo 1 for Sparring")!.click());
    assert.equal(ui.button("Remove photo"), undefined);
    await act(async () => ui.button("Remove voice note 1")!.click());
    assert.ok(ui.calls.some(call => call.method === "DELETE" && call.url.endsWith("/api/coach/fighters/fighter/training/attachments/mine")));
  } finally { ui.close(); }
});
