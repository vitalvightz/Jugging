import assert from "node:assert/strict";
import test from "node:test";
import { cleanVoice } from "../../supabase/functions/process-attachment/sanitize";
import { downsample, encodeWav, fitWithin, formatDuration, photoProblem } from "./attachments";

test("recorded voice notes are WAVs the server accepts", () => {
  const second = new Float32Array(48_000).map((_, i) => Math.sin(i / 10));
  const samples = downsample(second, 48_000);
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

test("photos are scaled to fit 2048 px without being enlarged", () => {
  assert.deepEqual(fitWithin(4032, 3024), { width: 2048, height: 1536 });
  assert.deepEqual(fitWithin(3024, 4032), { width: 1536, height: 2048 });
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
