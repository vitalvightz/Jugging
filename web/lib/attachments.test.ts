import assert from "node:assert/strict";
import test from "node:test";
import { cleanVoice } from "../../supabase/functions/process-attachment/sanitize";
import { downsample, encodeWav, fitWithin, formatDuration, photoProblem, prepareVoice, VoiceRecorder } from "./attachments";

test("recorded voice notes are WAVs the server accepts", () => {
  const second = new Float32Array(48_000).map((_, i) => Math.sin(i / 10));
  const samples = prepareVoice(downsample(second, 48_000));
  assert.equal(samples.length, 16_000);
  const clean = cleanVoice(encodeWav(samples));
  assert.equal(clean.durationMs, 1000);
});

test("loud samples are clipped instead of wrapping around", () => {
  const wav = encodeWav(new Float32Array([2, -2]));
  const view = new DataView(wav.buffer);
  assert.equal(view.getInt16(44, true), 0x7fff);
  assert.equal(view.getInt16(46, true), -0x8000);
});

test("photos are scaled to fit 1280 px without being enlarged", () => {
  assert.deepEqual(fitWithin(4032, 3024), { width: 1280, height: 960 });
  assert.deepEqual(fitWithin(3024, 4032), { width: 960, height: 1280 });
  assert.deepEqual(fitWithin(800, 600), { width: 800, height: 600 });
});

test("only photo types under 25 MB are accepted for preparing", () => {
  assert.equal(photoProblem({ type: "image/heic", size: 4_000_000 }), null);
  assert.match(photoProblem({ type: "image/svg+xml", size: 10 })!, /JPG or PNG/);
  assert.match(photoProblem({ type: "application/pdf", size: 10 })!, /JPG or PNG/);
  assert.match(photoProblem({ type: "image/jpeg", size: 26 * 1024 * 1024 })!, /over 25 MB/);
});

test("durations read as m:ss", () => {
  assert.equal(formatDuration(12_400), "0:12");
  assert.equal(formatDuration(60_000), "1:00");
});


test("cancelling while microphone permission is pending closes the late stream", async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  let resolve!: (stream: MediaStream) => void;
  let stopped = 0;
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: {
    mediaDevices: { getUserMedia: (constraints: MediaStreamConstraints) => {
      assert.deepEqual(constraints.audio, { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true });
      return new Promise<MediaStream>(done => { resolve = done; });
    } },
  } });
  try {
    const recorder = new VoiceRecorder(() => {});
    const pending = recorder.start();
    recorder.cancel();
    resolve({ getTracks: () => [{ stop: () => { stopped++; } }] } as unknown as MediaStream);
    await assert.rejects(pending, /cancelled/);
    assert.equal(stopped, 1);
  } finally {
    if (original) Object.defineProperty(globalThis, "navigator", original);
    else Reflect.deleteProperty(globalThis, "navigator");
  }
});

function tone(hz: number, amplitude: number) {
  return Float32Array.from({ length: 16_000 }, (_, i) => amplitude * Math.sin(2 * Math.PI * hz * i / 16_000));
}

function rms(samples: Float32Array) {
  const middle = samples.subarray(4000, 12000);
  return Math.sqrt(middle.reduce((sum, value) => sum + value * value, 0) / middle.length);
}

test("voice cleanup suppresses rumble while retaining speech frequencies", () => {
  const rumble = tone(20, 0.05);
  const speech = tone(1000, 0.05);
  const rumbleRatio = rms(prepareVoice(rumble)) / rms(rumble);
  const speechRatio = rms(prepareVoice(speech)) / rms(speech);
  assert.ok(rumbleRatio / speechRatio < 0.07);
  assert.ok(speechRatio > 1.9 && speechRatio <= 2.01);
});

test("voice cleanup bounds gain, leaves silence quiet, and fades both edges", () => {
  assert.deepEqual(prepareVoice(new Float32Array(1000)), new Float32Array(1000));
  const quiet = tone(1000, 0.001);
  assert.ok(rms(prepareVoice(quiet)) <= rms(quiet) * 1.01);
  const loud = tone(1000, 2);
  const original = loud.slice();
  const result = prepareVoice(loud);
  assert.deepEqual(loud, original);
  assert.equal(result.length, loud.length);
  assert.equal(result[0], 0);
  assert.equal(Math.abs(result[result.length - 1]), 0);
  assert.ok(result.every(value => Number.isFinite(value) && Math.abs(value) <= 0.892));
  assert.equal(encodeWav(result).length, encodeWav(loud).length);
  assert.equal(cleanVoice(encodeWav(result)).durationMs, 1000);
  assert.equal(prepareVoice(new Float32Array()).length, 0);
});
