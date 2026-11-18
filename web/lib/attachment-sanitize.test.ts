// The photo and voice-note rebuild that runs in the process-attachment Edge Function.
import assert from "node:assert/strict";
import test from "node:test";
import jpeg from "jpeg-js";
import {
  cleanPage, cleanPhoto, cleanVoice, imagePdf, jpegFrameSize, LIMITS, Rejected, wav, type JpegCodec,
} from "../../supabase/functions/process-attachment/sanitize";

const codec: JpegCodec = {
  decode: (data, options) => jpeg.decode(data, options),
  encode: (image, quality) => jpeg.encode(image, quality),
};
const text = (value: string) => new TextEncoder().encode(value);
const contains = (bytes: Uint8Array, needle: string) => Buffer.from(bytes).includes(Buffer.from(needle));
const concat = (...parts: Uint8Array[]) => new Uint8Array(Buffer.concat(parts));

function photo(width = 64, height = 48): Uint8Array {
  const data = new Uint8Array(width * height * 4);
  for (let i = 0; i < data.length; i += 4) { data[i] = i % 255; data[i + 1] = 90; data[i + 2] = 200; data[i + 3] = 255; }
  return new Uint8Array(jpeg.encode({ width, height, data }, 90).data);
}

// An APP1 Exif segment carrying a GPS marker and a script string, placed right after SOI.
function withExif(source: Uint8Array): Uint8Array {
  const payload = text("Exif\0\0GPSLatitude 51.5074 N <script>alert(1)</script>");
  const segment = new Uint8Array(4 + payload.length);
  segment.set([0xff, 0xe1, ((payload.length + 2) >> 8) & 0xff, (payload.length + 2) & 0xff]);
  segment.set(payload, 4);
  return concat(source.subarray(0, 2), segment, source.subarray(2));
}

const rejects = (run: () => unknown, message: RegExp) => assert.throws(run, (error: unknown) =>
  error instanceof Rejected && message.test(error.message));

test("a photo is rebuilt from pixels: EXIF, GPS and hidden text are gone", () => {
  const dirty = withExif(photo());
  assert.ok(contains(dirty, "GPSLatitude"));
  const clean = cleanPhoto(dirty, codec, dirty.length);
  assert.equal(clean.width, 64);
  assert.equal(clean.height, 48);
  assert.ok(!contains(clean.bytes, "GPSLatitude") && !contains(clean.bytes, "Exif") && !contains(clean.bytes, "<script"));
  assert.deepEqual([...clean.bytes.subarray(0, 3)], [0xff, 0xd8, 0xff]);
  assert.deepEqual(jpegFrameSize(clean.bytes), { width: 64, height: 48 });
});

test("an HTML payload appended to a real JPEG (polyglot) does not survive", () => {
  const polyglot = concat(photo(), text("<html><body><script>fetch('/steal')</script></body></html>"));
  const clean = cleanPhoto(polyglot, codec);
  assert.ok(!contains(clean.bytes, "<html") && !contains(clean.bytes, "steal"));
});

test("non-JPEG files are refused whatever they are called", () => {
  const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13]);
  rejects(() => cleanPhoto(png, codec), /Not a JPEG/);
  rejects(() => cleanPhoto(text("<svg onload=alert(1)></svg>"), codec), /Not a JPEG/);
  rejects(() => cleanPhoto(text("GIF89a......"), codec), /Not a JPEG/);
});

test("oversized dimensions are refused from the header, before any decoding", () => {
  const big = photo();
  const frame = jpegFrameSize(big);
  assert.deepEqual(frame, { width: 64, height: 48 });
  // Patch the frame header to claim 30000 x 30000 (a decompression bomb).
  const bomb = new Uint8Array(big);
  for (let at = 2; at < bomb.length - 9; at += 1) {
    if (bomb[at] === 0xff && (bomb[at + 1] === 0xc0 || bomb[at + 1] === 0xc2)) {
      bomb.set([30000 >> 8, 30000 & 0xff, 30000 >> 8, 30000 & 0xff], at + 5);
      break;
    }
  }
  let decoded = false;
  const spy: JpegCodec = { ...codec, decode: (...args) => { decoded = true; return codec.decode(...args); } };
  rejects(() => cleanPhoto(bomb, spy), /too large/);
  assert.equal(decoded, false);
  rejects(() => cleanPhoto(photo(LIMITS.photoMaxSide + 1, 8), codec), /too large/);
});

test("truncated, damaged, mismatched or oversized photo uploads are refused", () => {
  const good = photo();
  rejects(() => cleanPhoto(good.subarray(0, Math.floor(good.length / 2)), codec), /Damaged/);
  rejects(() => cleanPhoto(good.subarray(0, 20), codec), /Damaged/);
  rejects(() => cleanPhoto(good, codec, good.length + 1), /size mismatch/);
  rejects(() => cleanPhoto(new Uint8Array(LIMITS.photoBytes + 1), codec), /too large/);
  rejects(() => cleanPhoto(new Uint8Array(0), codec), /too large/);
});

function recording(seconds: number, { rate = 16000, channels = 1, bits = 16, format = 1 } = {}): Uint8Array {
  const samples = new Uint8Array(Math.round(seconds * rate) * 2);
  for (let i = 0; i < samples.length; i += 1) samples[i] = (i * 7) % 256;
  const out = wav(samples, rate);
  const view = new DataView(out.buffer);
  view.setUint16(20, format, true); view.setUint16(22, channels, true); view.setUint16(34, bits, true);
  if (channels !== 1 || bits !== 16) { view.setUint16(32, channels * bits / 8, true); view.setUint32(28, rate * channels * bits / 8, true); }
  return out;
}

test("a voice note is rebuilt from its samples, dropping every other chunk", () => {
  const plain = recording(2);
  const list = concat(text("LIST"), new Uint8Array([26, 0, 0, 0]), text("INFOICMT<script>x</script>"));
  // RIFF, fmt, LIST metadata, data, and trailing junk after the file.
  const dirty = concat(plain.subarray(0, 36), list, plain.subarray(36), text("<html>trailing</html>"));
  new DataView(dirty.buffer).setUint32(4, plain.length - 8 + list.length, true);
  const clean = cleanVoice(dirty);
  assert.equal(clean.durationMs, 2000);
  assert.equal(clean.bytes.length, 44 + 2 * 16000 * 2);
  assert.ok(!contains(clean.bytes, "<script") && !contains(clean.bytes, "LIST") && !contains(clean.bytes, "<html"));
  assert.deepEqual(clean.bytes.subarray(44), plain.subarray(44));
});

test("only mono 16 kHz 16-bit PCM between 0.3 and 60 seconds is accepted", () => {
  rejects(() => cleanVoice(recording(1, { channels: 2 })), /format/);
  rejects(() => cleanVoice(recording(1, { rate: 44100 })), /format/);
  rejects(() => cleanVoice(recording(1, { bits: 8 })), /format/);
  rejects(() => cleanVoice(recording(1, { format: 3 })), /format/);
  rejects(() => cleanVoice(recording(0.1)), /too short/);
  rejects(() => cleanVoice(recording(61)), /over 60 seconds/);
  assert.equal(cleanVoice(recording(60)).durationMs, 60000);
});

test("files pretending to be WAV are refused", () => {
  rejects(() => cleanVoice(text("<html><script>alert(1)</script></html>".padEnd(64, " "))), /Not a WAV/);
  rejects(() => cleanVoice(photo()), /Not a WAV/);
  const lying = recording(1);
  new DataView(lying.buffer).setUint32(40, 10_000_000, true);
  rejects(() => cleanVoice(lying), /Damaged/);
  const noData = recording(1).subarray(0, 36);
  rejects(() => cleanVoice(concat(noData, new Uint8Array(16))), /Damaged/);
  rejects(() => cleanVoice(recording(1), 5), /size mismatch/);
  rejects(() => cleanVoice(new Uint8Array(LIMITS.voiceBytes + 1)), /too large/);
});

// PDF pages: each page image is rebuilt like a photo, then written into a new
// image-only PDF. Nothing from the uploader's file can reach the output.
// A mostly white page with dark text-like bars, like a rendered document.
function documentPage(width: number, height: number): Uint8Array {
  const data = new Uint8Array(width * height * 4).fill(255);
  for (let y = 80; y < height - 80; y += 40) {
    for (let x = 80; x < width - 80; x += 1) {
      for (let row = 0; row < 12; row += 1) data.fill(30, ((y + row) * width + x) * 4, ((y + row) * width + x) * 4 + 3);
    }
  }
  return new Uint8Array(jpeg.encode({ width, height, data }, 85).data);
}

test("a PDF page is rebuilt from pixels within the page limits", () => {
  const page = withExif(documentPage(1131, 1600));
  const clean = cleanPage(page, codec);
  assert.deepEqual([clean.width, clean.height], [1131, 1600]);
  assert.ok(!contains(clean.bytes, "GPSLatitude") && !contains(clean.bytes, "<script"));
  rejects(() => cleanPage(documentPage(1601, 900), codec), /Page is too large/);
  rejects(() => cleanPage(documentPage(1500, 1500), codec), /Page is too large/);
  rejects(() => cleanPage(new Uint8Array(LIMITS.pageBytes + 1), codec), /Page is too large/);
  rejects(() => cleanPage(text("%PDF-1.7 /JavaScript (app.alert(1))"), codec), /Not a JPEG/);
});

test("the rebuilt PDF holds only page images", async () => {
  const pages = [cleanPage(documentPage(900, 1273), codec), cleanPage(documentPage(1273, 900), codec)];
  const pdf = imagePdf(pages);
  const source = Buffer.from(pdf).toString("latin1");
  assert.ok(source.startsWith("%PDF-1.4"));
  for (const banned of ["/JavaScript", "/JS", "/OpenAction", "/AA", "/URI", "/Launch", "/EmbeddedFile", "/AcroForm", "/Font", "/Info"]) {
    assert.ok(!source.includes(banned), banned);
  }
  assert.equal(source.match(/\/Subtype \/Image/g)?.length, 2);
  const { getDocument } = await import("pdfjs-dist/legacy/build/pdf.mjs");
  const doc = await getDocument({ data: new Uint8Array(pdf), useWasm: false }).promise;
  assert.equal(doc.numPages, 2);
  const [first, second] = [await doc.getPage(1), await doc.getPage(2)];
  assert.ok(first.view[3] > first.view[2] && second.view[2] > second.view[3]);
  assert.equal(Math.round(Math.max(first.view[2], first.view[3])), 842);
  await doc.cleanup();
  assert.throws(() => imagePdf([]), Rejected);
  assert.throws(() => imagePdf(Array.from({ length: 31 }, () => pages[0])), Rejected);
});
