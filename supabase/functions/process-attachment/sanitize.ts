// Rebuilds untrusted uploads into fresh files. Nothing from the original file
// survives except decoded pixels (photos) or raw PCM samples (voice notes):
// no metadata, no EXIF/GPS, no extra chunks, no trailing bytes.
//
// Pure and dependency-free so it runs unchanged in the Edge Function (Deno)
// and in node tests. The JPEG codec is passed in.

export const LIMITS = {
  photoBytes: 8 * 1024 * 1024,
  // Sized so a worst-case (high-entropy) photo rebuilds well inside the Edge
  // Function limits (2 s CPU, 256 MB): about 0.6 s and 120 MB measured with jpeg-js.
  photoMaxSide: 1280,
  photoQuality: 82,
  voiceBytes: 2 * 1024 * 1024,
  voiceSampleRate: 16000,
  voiceMinMs: 300,
  voiceMaxMs: 60500,
} as const;

export class Rejected extends Error {}

export type DecodedImage = { width: number; height: number; data: Uint8Array };
export type JpegCodec = {
  decode(data: Uint8Array, options: {
    useTArray: true; formatAsRGBA: true; tolerantDecoding: false;
    maxResolutionInMP: number; maxMemoryUsageInMB: number;
  }): DecodedImage;
  encode(image: DecodedImage, quality: number): { data: Uint8Array };
};

export type CleanPhoto = { bytes: Uint8Array; width: number; height: number };
export type CleanVoice = { bytes: Uint8Array; durationMs: number };

// Baseline and progressive Huffman JPEGs only (what phones and canvas produce).
const SUPPORTED_SOF = new Set([0xc0, 0xc1, 0xc2]);
const OTHER_SOF = new Set([0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf]);

/** Reads the frame header without decoding, so oversized images are refused cheaply. */
export function jpegFrameSize(bytes: Uint8Array): { width: number; height: number } {
  if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8 || bytes[2] !== 0xff) throw new Rejected("Not a JPEG");
  let at = 2;
  while (at + 4 <= bytes.length) {
    if (bytes[at] !== 0xff) throw new Rejected("Damaged JPEG");
    const marker = bytes[at + 1];
    if (marker === 0xff) { at += 1; continue; }
    if (marker === 0xd9 || marker === 0xda) break;
    if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) { at += 2; continue; }
    const length = (bytes[at + 2] << 8) | bytes[at + 3];
    if (length < 2 || at + 2 + length > bytes.length) throw new Rejected("Damaged JPEG");
    if (OTHER_SOF.has(marker)) throw new Rejected("Unsupported JPEG");
    if (SUPPORTED_SOF.has(marker)) {
      if (length < 8) throw new Rejected("Damaged JPEG");
      const height = (bytes[at + 5] << 8) | bytes[at + 6];
      const width = (bytes[at + 7] << 8) | bytes[at + 8];
      if (!width || !height) throw new Rejected("Damaged JPEG");
      return { width, height };
    }
    at += 2 + length;
  }
  throw new Rejected("Damaged JPEG");
}

export function cleanPhoto(bytes: Uint8Array, codec: JpegCodec, declaredSize?: number): CleanPhoto {
  if (!bytes.length || bytes.length > LIMITS.photoBytes) throw new Rejected("Photo is too large");
  if (declaredSize !== undefined && bytes.length !== declaredSize) throw new Rejected("Upload size mismatch");
  const frame = jpegFrameSize(bytes);
  if (frame.width > LIMITS.photoMaxSide || frame.height > LIMITS.photoMaxSide) throw new Rejected("Photo is too large");
  let decoded: DecodedImage;
  try {
    decoded = codec.decode(bytes, {
      useTArray: true, formatAsRGBA: true, tolerantDecoding: false,
      maxResolutionInMP: (LIMITS.photoMaxSide * LIMITS.photoMaxSide) / 1_000_000 + 0.1, maxMemoryUsageInMB: 96,
    });
  } catch {
    throw new Rejected("Damaged JPEG");
  }
  const { width, height, data } = decoded;
  if (width !== frame.width || height !== frame.height || data.length !== width * height * 4) throw new Rejected("Damaged JPEG");
  // Fresh pixels into a fresh file: alpha forced opaque, nothing else carried over.
  const pixels = new Uint8Array(data.length);
  pixels.set(data);
  for (let i = 3; i < pixels.length; i += 4) pixels[i] = 255;
  const encoded = codec.encode({ width, height, data: pixels }, LIMITS.photoQuality).data;
  return { bytes: new Uint8Array(encoded), width, height };
}

const ascii = (bytes: Uint8Array, at: number) => String.fromCharCode(bytes[at], bytes[at + 1], bytes[at + 2], bytes[at + 3]);

export function cleanVoice(bytes: Uint8Array, declaredSize?: number): CleanVoice {
  if (!bytes.length || bytes.length > LIMITS.voiceBytes) throw new Rejected("Voice note is too large");
  if (declaredSize !== undefined && bytes.length !== declaredSize) throw new Rejected("Upload size mismatch");
  if (bytes.length < 44 || ascii(bytes, 0) !== "RIFF" || ascii(bytes, 8) !== "WAVE") throw new Rejected("Not a WAV recording");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let fmtFound = false;
  let samples: Uint8Array | null = null;
  // Bytes after the RIFF container (an appended payload) are never parsed or kept.
  const riffSize = view.getUint32(4, true);
  if (riffSize < 36) throw new Rejected("Damaged WAV recording");
  const end = Math.min(bytes.length, 8 + riffSize);
  let at = 12;
  while (at + 8 <= end) {
    const id = ascii(bytes, at);
    const size = view.getUint32(at + 4, true);
    const body = at + 8;
    if (size > end - body) throw new Rejected("Damaged WAV recording");
    if (id === "fmt ") {
      if (fmtFound || size < 16) throw new Rejected("Damaged WAV recording");
      fmtFound = true;
      const format = view.getUint16(body, true);
      const channels = view.getUint16(body + 2, true);
      const rate = view.getUint32(body + 4, true);
      const byteRate = view.getUint32(body + 8, true);
      const blockAlign = view.getUint16(body + 12, true);
      const bits = view.getUint16(body + 14, true);
      if (format !== 1 || channels !== 1 || rate !== LIMITS.voiceSampleRate || bits !== 16
        || blockAlign !== 2 || byteRate !== rate * 2) throw new Rejected("Unsupported recording format");
    } else if (id === "data") {
      if (!fmtFound || samples) throw new Rejected("Damaged WAV recording");
      if (size % 2) throw new Rejected("Damaged WAV recording");
      samples = bytes.subarray(body, body + size);
    }
    // Any other chunk (LIST, metadata, anything) is skipped and never copied.
    at = body + size + (size % 2);
  }
  if (!samples) throw new Rejected("Damaged WAV recording");
  const durationMs = Math.round((samples.length / 2 / LIMITS.voiceSampleRate) * 1000);
  if (durationMs < LIMITS.voiceMinMs) throw new Rejected("Voice note is too short");
  if (durationMs > LIMITS.voiceMaxMs) throw new Rejected("Voice note is over 60 seconds");
  return { bytes: wav(samples), durationMs };
}

/** A minimal 44-byte PCM header followed by a copy of the samples. */
export function wav(samples: Uint8Array, rate: number = LIMITS.voiceSampleRate): Uint8Array {
  const out = new Uint8Array(44 + samples.length);
  const view = new DataView(out.buffer);
  const tag = (at: number, text: string) => { for (let i = 0; i < 4; i += 1) out[at + i] = text.charCodeAt(i); };
  tag(0, "RIFF"); view.setUint32(4, 36 + samples.length, true); tag(8, "WAVE");
  tag(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  tag(36, "data"); view.setUint32(40, samples.length, true);
  out.set(samples, 44);
  return out;
}
