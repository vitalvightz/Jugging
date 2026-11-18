import assert from "node:assert/strict";
import test from "node:test";
import { placeholderPeaks, wavPeaks } from "./voice-peaks";

function wav(samples: number[]) {
  const out = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(out);
  const tag = (at: number, value: string) => [...value].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
  tag(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); tag(8, "WAVE");
  tag(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  tag(36, "data"); view.setUint32(40, samples.length * 2, true);
  samples.forEach((sample, index) => view.setInt16(44 + index * 2, sample, true));
  return out;
}

test("a voice note's bars follow its loudness: quiet slices stay short, the loudest is full height", () => {
  const quiet = Array(400).fill(100);
  const loud = Array.from({ length: 400 }, (_, i) => (i % 2 ? 20000 : -20000));
  const peaks = wavPeaks(wav([...quiet, ...loud, ...quiet, ...loud]), 4)!;
  assert.equal(peaks.length, 4);
  assert.equal(peaks[1], 1);
  assert.ok(peaks[0] < 0.2 && peaks[0] >= 0.12);
  assert.deepEqual(peaks[0], peaks[2]);
});

test("anything that isn't a WAV gets no waveform", () => {
  assert.equal(wavPeaks(new TextEncoder().encode("<html></html>".padEnd(100)).buffer as ArrayBuffer, 8), null);
  assert.equal(wavPeaks(wav([1, 2]), 8), null);
});

test("placeholder bars are steady for the same note and differ between notes", () => {
  assert.deepEqual(placeholderPeaks("a", 10), placeholderPeaks("a", 10));
  assert.notDeepEqual(placeholderPeaks("a", 10), placeholderPeaks("b", 10));
  assert.ok(placeholderPeaks("a", 30).every(value => value >= 0.25 && value <= 0.7));
});
