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
  photoQuality: 80,
  // A small preview of the rebuilt photo, made from the same clean pixels, so
  // lists can show real thumbnails (about 15-30 KB) instead of grey boxes.
  thumbMaxSide: 360,
  thumbQuality: 72,
  voiceBytes: 2 * 1024 * 1024,
  voiceSampleRate: 16000,
  voiceMinMs: 300,
  voiceMaxMs: 60500,
  // PDF pages arrive as JPEGs rendered in the uploader's browser. A page is
  // up to 1600 px on its long side and 2.0 megapixels, which rebuilds inside
  // the same Edge limits; the whole rebuilt document must fit in 8 MB.
  pageBytes: 1024 * 1024,
  pageMaxSide: 1600,
  pageMaxPixels: 2_000_000,
  pageQuality: 80,
  documentBytes: 8 * 1024 * 1024,
  documentPages: 30,
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
export type CleanPhotoWithThumb = CleanPhoto & { thumb: CleanPhoto };
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

type ImageRules = { maxBytes: number; maxSide: number; maxPixels: number; quality: number; tooLarge: string };
const PHOTO_RULES: ImageRules = { maxBytes: LIMITS.photoBytes, maxSide: LIMITS.photoMaxSide,
  maxPixels: LIMITS.photoMaxSide * LIMITS.photoMaxSide, quality: LIMITS.photoQuality, tooLarge: "Photo is too large" };
const PAGE_RULES: ImageRules = { maxBytes: LIMITS.pageBytes, maxSide: LIMITS.pageMaxSide,
  maxPixels: LIMITS.pageMaxPixels, quality: LIMITS.pageQuality, tooLarge: "Page is too large" };

function rebuildJpeg(bytes: Uint8Array, codec: JpegCodec, rules: ImageRules, declaredSize?: number): CleanPhoto & { pixels: Uint8Array } {
  if (!bytes.length || bytes.length > rules.maxBytes) throw new Rejected(rules.tooLarge);
  if (declaredSize !== undefined && bytes.length !== declaredSize) throw new Rejected("Upload size mismatch");
  const frame = jpegFrameSize(bytes);
  if (frame.width > rules.maxSide || frame.height > rules.maxSide || frame.width * frame.height > rules.maxPixels) {
    throw new Rejected(rules.tooLarge);
  }
  let decoded: DecodedImage;
  try {
    decoded = codec.decode(bytes, {
      useTArray: true, formatAsRGBA: true, tolerantDecoding: false,
      maxResolutionInMP: rules.maxPixels / 1_000_000 + 0.1, maxMemoryUsageInMB: 96,
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
  const encoded = codec.encode({ width, height, data: pixels }, rules.quality).data;
  return { bytes: new Uint8Array(encoded), width, height, pixels };
}

/** Box-averages clean RGBA pixels down so the long side is at most maxSide. */
export function downscale(pixels: Uint8Array, width: number, height: number, maxSide: number): DecodedImage {
  const scale = Math.min(1, maxSide / Math.max(width, height));
  const w = Math.max(1, Math.round(width * scale));
  const h = Math.max(1, Math.round(height * scale));
  const out = new Uint8Array(w * h * 4);
  for (let y = 0; y < h; y += 1) {
    const y0 = Math.floor((y * height) / h);
    const y1 = Math.max(y0 + 1, Math.floor(((y + 1) * height) / h));
    for (let x = 0; x < w; x += 1) {
      const x0 = Math.floor((x * width) / w);
      const x1 = Math.max(x0 + 1, Math.floor(((x + 1) * width) / w));
      let r = 0, g = 0, b = 0;
      for (let sy = y0; sy < y1; sy += 1) {
        let at = (sy * width + x0) * 4;
        for (let sx = x0; sx < x1; sx += 1, at += 4) { r += pixels[at]; g += pixels[at + 1]; b += pixels[at + 2]; }
      }
      const n = (y1 - y0) * (x1 - x0);
      const to = (y * w + x) * 4;
      out[to] = r / n; out[to + 1] = g / n; out[to + 2] = b / n; out[to + 3] = 255;
    }
  }
  return { width: w, height: h, data: out };
}

/** The rebuilt photo, plus a small thumbnail drawn from the same clean pixels. */
export function cleanPhoto(bytes: Uint8Array, codec: JpegCodec, declaredSize?: number): CleanPhotoWithThumb {
  const { pixels, ...photo } = rebuildJpeg(bytes, codec, PHOTO_RULES, declaredSize);
  const small = downscale(pixels, photo.width, photo.height, LIMITS.thumbMaxSide);
  const thumb = { bytes: new Uint8Array(codec.encode(small, LIMITS.thumbQuality).data), width: small.width, height: small.height };
  return { ...photo, thumb };
}

/** One PDF page image, rebuilt from decoded pixels exactly like a photo. */
export function cleanPage(bytes: Uint8Array, codec: JpegCodec): CleanPhoto {
  const { pixels: _pixels, ...page } = rebuildJpeg(bytes, codec, PAGE_RULES);
  return page;
}

/**
 * Writes a brand-new PDF whose pages are only the given rebuilt JPEGs: no
 * text, fonts, scripts, actions, forms, links, attachments or metadata.
 * Each page is sized so its long side is 842 pt (A4), keeping its shape.
 */
export function imagePdf(pages: CleanPhoto[]): Uint8Array {
  if (!pages.length || pages.length > LIMITS.documentPages) throw new Rejected("Document has too many pages");
  const encoder = new TextEncoder();
  const chunks: Uint8Array[] = [];
  const offsets: number[] = [];
  let length = 0;
  const push = (part: Uint8Array | string) => {
    const bytes = typeof part === "string" ? encoder.encode(part) : part;
    chunks.push(bytes); length += bytes.length;
  };
  const object = (id: number, ...parts: (Uint8Array | string)[]) => {
    offsets[id] = length;
    push(`${id} 0 obj\n`); for (const part of parts) push(part); push("\nendobj\n");
  };
  push("%PDF-1.4\n%\xff\xff\xff\xff\n");
  // Objects: 1 catalog, 2 page tree, then per page: page, image, contents.
  const kids = pages.map((_, index) => `${3 + index * 3} 0 R`).join(" ");
  object(1, "<< /Type /Catalog /Pages 2 0 R >>");
  object(2, `<< /Type /Pages /Count ${pages.length} /Kids [${kids}] >>`);
  pages.forEach((page, index) => {
    const id = 3 + index * 3;
    const scale = 842 / Math.max(page.width, page.height);
    const w = Math.max(1, Math.round(page.width * scale * 100) / 100);
    const h = Math.max(1, Math.round(page.height * scale * 100) / 100);
    const draw = `q ${w} 0 0 ${h} 0 0 cm /Im0 Do Q`;
    object(id, `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${w} ${h}] /Resources << /XObject << /Im0 ${id + 1} 0 R >> >> /Contents ${id + 2} 0 R >>`);
    object(id + 1, `<< /Type /XObject /Subtype /Image /Width ${page.width} /Height ${page.height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${page.bytes.length} >>\nstream\n`,
      page.bytes, "\nendstream");
    object(id + 2, `<< /Length ${draw.length} >>\nstream\n${draw}\nendstream`);
  });
  const count = 3 + pages.length * 3;
  const xref = length;
  push(`xref\n0 ${count}\n0000000000 65535 f \n`);
  for (let id = 1; id < count; id += 1) push(`${String(offsets[id]).padStart(10, "0")} 00000 n \n`);
  push(`trailer\n<< /Size ${count} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`);
  const out = new Uint8Array(length);
  let at = 0;
  for (const chunk of chunks) { out.set(chunk, at); at += chunk.length; }
  if (out.length > LIMITS.documentBytes) throw new Rejected("Document is too large");
  return out;
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
