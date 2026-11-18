/**
 * A voice note's waveform: the loudness of each slice of a 16-bit mono WAV
 * (what the server rebuilds every voice note into), scaled to 0.12-1 so quiet
 * slices still show as a short bar. Returns null for anything else.
 */
export function wavPeaks(buffer: ArrayBuffer, bars: number): number[] | null {
  const bytes = new Uint8Array(buffer);
  if (bytes.length < 44 || text(bytes, 0) !== "RIFF" || text(bytes, 8) !== "WAVE") return null;
  const view = new DataView(buffer);
  let at = 12;
  while (at + 8 <= bytes.length) {
    const id = text(bytes, at);
    const size = view.getUint32(at + 4, true);
    if (id === "data") {
      const start = at + 8;
      const count = Math.floor(Math.min(size, bytes.length - start) / 2);
      if (count < bars) return null;
      const per = Math.floor(count / bars);
      const levels: number[] = [];
      for (let bar = 0; bar < bars; bar += 1) {
        let sum = 0;
        for (let i = 0; i < per; i += 1) {
          const sample = view.getInt16(start + (bar * per + i) * 2, true) / 32768;
          sum += sample * sample;
        }
        levels.push(Math.sqrt(sum / per));
      }
      const loudest = Math.max(...levels) || 1;
      return levels.map(level => Math.round((0.12 + 0.88 * Math.min(1, level / loudest)) * 100) / 100);
    }
    at += 8 + size + (size % 2);
  }
  return null;
}

/** Quiet, even placeholder bars (shown before a note is first played), varied by id so rows don't look identical. */
export function placeholderPeaks(seed: string, bars: number): number[] {
  let hash = 2166136261;
  for (let i = 0; i < seed.length; i += 1) hash = Math.imul(hash ^ seed.charCodeAt(i), 16777619);
  return Array.from({ length: bars }, (_, index) => {
    hash = Math.imul(hash ^ index, 16777619);
    return 0.25 + ((hash >>> 0) % 1000) / 1000 * 0.45;
  });
}

function text(bytes: Uint8Array, at: number) {
  return String.fromCharCode(bytes[at], bytes[at + 1], bytes[at + 2], bytes[at + 3]);
}
