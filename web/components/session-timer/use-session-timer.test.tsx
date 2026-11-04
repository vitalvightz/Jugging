import "../test-dom";
import test from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { addTime, endSession, pause, resume, startItem } from "@/lib/session-timer/engine";
import { timerAudio } from "@/lib/session-timer/audio";
import { freeRoundsItem } from "@/lib/session-timer/contact";
import { useSessionTimer, type SessionTimerController } from "./use-session-timer";

test("a running minimised timer retains one wake lock, reacquires after hiding, and releases on finish", async () => {
  window.localStorage.clear();
  const locks: Array<{ released: boolean; release: () => Promise<void> }> = [];
  Object.defineProperty(navigator, "wakeLock", { configurable: true, value: {
    request: async () => {
      const lock = { released: false, release: async () => { lock.released = true; } };
      locks.push(lock);
      return lock;
    },
  } });
  let controller: SessionTimerController;
  function Probe({ visible }: { visible: boolean }) {
    controller = useSessionTimer({ items: [freeRoundsItem()], storageKey: "wake-test", audible: visible });
    return null;
  }
  const node = document.createElement("div");
  const root = createRoot(node);
  try {
    await act(async () => root.render(<Probe visible />));
    assert.equal(locks.length, 1);
    await act(async () => controller.run(startItem));
    await act(async () => root.render(<Probe visible={false} />));
    assert.equal(locks[0].released, false, "minimising during work must not release the screen lock");
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    assert.equal(locks.length, 1, "visible events must not acquire duplicate locks");
    // Browsers automatically release the lock when a page becomes hidden.
    locks[0].released = true;
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    assert.equal(locks.length, 2);
    await act(async () => controller.update(pause));
    assert.equal(locks[1].released, true);
    await act(async () => controller.update(resume));
    assert.equal(locks.length, 3);
    await act(async () => controller.update(endSession));
    assert.equal(locks[2].released, true);
  } finally {
    await act(async () => root.unmount());
    Reflect.deleteProperty(navigator, "wakeLock");
    window.localStorage.clear();
  }
});

for (const scenario of [
  { label: "halfway", before: 80_000, after: 100_000 },
  { label: "30-second", before: 140_000, after: 160_000 },
  { label: "10-second", before: 160_000, after: 177_000 },
]) {
  test(`waking in the same round does not replay a stale ${scenario.label} warning, voice or vibration`, async () => {
    window.localStorage.clear();
    window.localStorage.setItem("unlxck.session-timer.settings", JSON.stringify({
      warnHalfway: true, warnThirty: true, warnTen: true, voice: true, prepSec: 0,
    }));
    const realNow = Date.now;
    let now = 1000;
    Date.now = () => now;
    const audio = timerAudio();
    const original = { play: audio.play, say: audio.say, buzzSound: audio.buzzSound };
    const emitted: string[] = [];
    audio.play = (sound) => { emitted.push(`sound:${sound}`); };
    audio.say = (text) => { emitted.push(`voice:${text}`); };
    audio.buzzSound = (sound) => { emitted.push(`vibrate:${sound}`); };
    let controller: SessionTimerController;
    function Probe() {
      controller = useSessionTimer({ items: [{ ...freeRoundsItem(), rounds: 3, workSec: 180 }], storageKey: "stale-warning", audible: true });
      return null;
    }
    const root = createRoot(document.createElement("div"));
    const tickAt = async (elapsed: number) => {
      now = 1000 + elapsed;
      await act(async () => window.dispatchEvent(new Event("pageshow")));
    };
    try {
      await act(async () => root.render(<Probe />));
      await act(async () => controller.run(startItem));
      await tickAt(scenario.before);
      emitted.length = 0;
      await tickAt(scenario.after);
      assert.equal(controller!.state.phase, "work");
      assert.equal(controller!.state.unit, 1);
      assert.equal(emitted.length, 0, "a missed warning is stale even when playback is unavailable/interrupted");
      // Later fresh cues must still work, rather than muting the whole round.
      await tickAt(179_950);
      emitted.length = 0;
      await act(async () => controller.update((state, at) => addTime(state, 20, at)));
      await tickAt(189_900);
      emitted.length = 0;
      await tickAt(190_050);
      assert.ok(emitted.includes("sound:clapper"));
      assert.ok(emitted.includes("voice:10 seconds"));
    } finally {
      await act(async () => root.unmount());
      Date.now = realNow;
      Object.assign(audio, original);
      window.localStorage.clear();
    }
  });
}

test("returning after several suspended rounds catches up without ringing old bells", async () => {
  window.localStorage.clear();
  const realNow = Date.now;
  let now = 1000;
  Date.now = () => now;
  const audio = timerAudio();
  const realPlay = audio.play;
  const played: string[] = [];
  audio.play = (sound) => { played.push(sound); };
  let controller: SessionTimerController;
  function Probe() {
    controller = useSessionTimer({ items: [{ ...freeRoundsItem(), rounds: 3, workSec: 5, restSec: 2 }], storageKey: "sleep-test", audible: true });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => controller.run(startItem));
    assert.deepEqual(played, ["bell"]);
    played.length = 0;
    now = 17_000; // Round 3 began at 15,000; round 1/2 bells are stale.
    await act(async () => window.dispatchEvent(new Event("pageshow")));
    assert.equal(controller!.state.unit, 3);
    assert.deepEqual(controller!.state.completed, [2]);
    assert.deepEqual(played, []);
  } finally {
    await act(async () => root.unmount());
    Date.now = realNow;
    audio.play = realPlay;
    window.localStorage.clear();
  }
});

// Planned timers deliberately use persisted session start/completion evidence,
// not the free-round-timer event stream.
test("free timer transitions are measured automatically in beta; planned timer transitions stay separate", async () => {
  const { AppSessionContext } = await import("../auth-provider");
  const original = globalThis.fetch;
  const events: { event_name: string; feature: string; flow_id: string }[] = [];
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    if (path.endsWith("/events")) events.push(JSON.parse(String(init?.body)));
    return new Response(JSON.stringify({ accepted: true }), { status: 200 });
  };
  const context = { isReady: true, isMeHydrated: true, hasTransientMeError: false, session: { access_token: "timer-measurement" }, me: null,
    previewAppearanceMode: () => {}, refreshMe: async () => {}, replaceMe: () => {}, signOut: async () => {} };
  let controller: SessionTimerController;
  function Probe({ storageKey }: { storageKey: string }) {
    controller = useSessionTimer({ items: [freeRoundsItem()], storageKey, audible: false });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    for (const key of ["unlxck.session-timer.free", "unlxck.session-timer.planned:synthetic"]) {
      await act(async () => root.render(<AppSessionContext.Provider value={context}><Probe key={key} storageKey={key} /></AppSessionContext.Provider>));
      await act(async () => { controller.run(startItem); await new Promise(resolve => setTimeout(resolve, 30)); });
      await act(async () => { controller.update(endSession); await new Promise(resolve => setTimeout(resolve, 30)); });
    }
    assert.deepEqual(events.map(e => e.event_name), ["timer_started", "timer_exited"]);
    assert.equal(events[0].flow_id, events[1].flow_id);
    assert.equal(events[0].feature, "timer");
  } finally {
    await act(async () => root.unmount());
    window.localStorage.clear();
    globalThis.fetch = original;
  }
});
