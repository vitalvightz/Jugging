/**
 * Client-side preparation for photos and voice notes. The browser only ever
 * produces plain JPEG (re-drawn pixels) and plain PCM WAV; the server rebuilds
 * both again in isolation before anyone can open them, so this is a
 * convenience and size limit, never the security boundary.
 */
// Matches the server rebuild limit (supabase/functions/process-attachment/sanitize.ts).
export const PHOTO_MAX_SIDE = 1280;
export const PHOTO_MAX_INPUT_BYTES = 25 * 1024 * 1024;
export const VOICE_RATE = 16_000;
export const VOICE_MAX_SECONDS = 60;
const PHOTO_TYPES = ["image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", ""];

export function photoProblem(file: { type: string; size: number }): string | null {
  if (!PHOTO_TYPES.includes(file.type.toLowerCase())) return "Choose a photo (JPG or PNG).";
  if (file.size > PHOTO_MAX_INPUT_BYTES) return "That photo is over 25 MB.";
  return null;
}

/** Fit inside maxSide, keeping the aspect ratio and never upscaling. */
export function fitWithin(width: number, height: number, maxSide = PHOTO_MAX_SIDE) {
  const scale = Math.min(1, maxSide / Math.max(width, height));
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
}

/** Re-draws a photo into a fresh JPEG (camera rotation applied, metadata dropped). */
export async function preparePhoto(file: Blob): Promise<Blob> {
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    throw new Error("This photo can't be opened here. Try a JPG or PNG.");
  }
  try {
    const size = fitWithin(bitmap.width, bitmap.height);
    const canvas = document.createElement("canvas");
    canvas.width = size.width;
    canvas.height = size.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("This photo can't be opened here. Try a JPG or PNG.");
    context.fillStyle = "#fff";
    context.fillRect(0, 0, size.width, size.height);
    context.drawImage(bitmap, 0, 0, size.width, size.height);
    const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, "image/jpeg", 0.85));
    if (!blob) throw new Error("This photo can't be opened here. Try a JPG or PNG.");
    return blob;
  } finally {
    bitmap.close();
  }
}

/** Averages input samples down to the target rate (a simple low-pass and decimate). */
export function downsample(input: Float32Array, fromRate: number, toRate = VOICE_RATE): Float32Array {
  if (fromRate === toRate) return input.slice();
  const ratio = fromRate / toRate;
  const out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j += 1) sum += input[j];
    out[i] = end > start ? sum / (end - start) : 0;
  }
  return out;
}

/** Mono 16-bit PCM WAV: the only voice format the server accepts. */
export function encodeWav(samples: Float32Array, rate = VOICE_RATE): Uint8Array {
  const out = new Uint8Array(44 + samples.length * 2);
  const view = new DataView(out.buffer);
  const tag = (at: number, text: string) => { for (let i = 0; i < 4; i += 1) out[at + i] = text.charCodeAt(i); };
  tag(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); tag(8, "WAVE");
  tag(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  tag(36, "data"); view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i += 1) {
    const value = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  }
  return out;
}

export function formatDuration(ms: number | null | undefined) {
  const seconds = Math.max(0, Math.round((ms ?? 0) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/**
 * Records mono microphone audio in memory. Nothing is stored or sent until
 * the person presses Send; stop() releases the microphone.
 */
export class VoiceRecorder {
  private cancelled = false;
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private processor: ScriptProcessorNode | null = null;
  private chunks: Float32Array[] = [];
  private length = 0;
  private rate = VOICE_RATE;

  constructor(private readonly onLimit: () => void) {}

  get seconds() { return this.length / this.rate; }

  async start() {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    if (this.cancelled) {
      stream.getTracks().forEach(track => track.stop());
      throw new Error("Recording cancelled");
    }
    this.stream = stream;
    const Context = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    try { this.context = new Context({ sampleRate: VOICE_RATE }); } catch { this.context = new Context(); }
    this.rate = this.context.sampleRate;
    const sourceNode = this.context.createMediaStreamSource(this.stream);
    this.processor = this.context.createScriptProcessor(4096, 1, 1);
    this.processor.onaudioprocess = event => {
      const limit = VOICE_MAX_SECONDS * this.rate;
      if (this.length >= limit) return;
      const data = event.inputBuffer.getChannelData(0).slice(0, limit - this.length);
      this.chunks.push(data);
      this.length += data.length;
      if (this.length >= limit) this.onLimit();
    };
    sourceNode.connect(this.processor);
    this.processor.connect(this.context.destination);
  }

  /** Stops the microphone and returns the recording as a WAV, with its length. */
  async stop(): Promise<{ wav: Blob; durationMs: number }> {
    this.processor?.disconnect();
    this.stream?.getTracks().forEach(track => track.stop());
    await this.context?.close().catch(() => undefined);
    const joined = new Float32Array(this.length);
    let at = 0;
    for (const chunk of this.chunks) { joined.set(chunk, at); at += chunk.length; }
    const samples = downsample(joined, this.rate);
    this.chunks = [];
    const bytes = encodeWav(samples);
    return { wav: new Blob([bytes.buffer as ArrayBuffer], { type: "audio/wav" }), durationMs: Math.round((samples.length / VOICE_RATE) * 1000) };
  }

  cancel() {
    this.cancelled = true;
    this.processor?.disconnect();
    this.stream?.getTracks().forEach(track => track.stop());
    void this.context?.close().catch(() => undefined);
    this.chunks = [];
  }
}
