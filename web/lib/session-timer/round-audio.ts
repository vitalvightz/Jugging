import { renderTimerClip, type TimerSound, type TimerSoundSettings } from "./audio";
import { advance, crossedCues, cueSounds, phaseEndsAt, soundForEvents, warningLengthMs, type TimerState } from "./engine";

const SAMPLE_RATE = 16_000;
const CLIP_SECONDS = 4;
// Covers 12 x 3-minute rounds / 1-minute rests (47 min) without 173 MB WAVs.
export const MAX_ROUND_TRACK_MS = 50 * 60_000;
export const MAX_ROUND_TRACK_BYTES = 96 * 1024 * 1024;
export type RoundAudioCue = { atMs: number; sound: TimerSound };
export type RoundAudioTimeline = { cues: RoundAudioCue[]; durationMs: number };
export type RoundAudioStatus = "idle" | "preparing" | "playing" | "unavailable" | "interrupted";

/** Precompute only the current interval item. Never auto-start another exercise. */
export function roundAudioTimeline(state: TimerState, now: number, settings: TimerSoundSettings): RoundAudioTimeline | null {
  if (state.items[state.index]?.kind !== "interval" || state.pausedAt !== null ||
      (state.phase !== "work" && state.phase !== "rest")) return null;
  const cues: RoundAudioCue[] = [];
  let current = advance(state, now).state;
  const index = current.index;
  let durationMs = 0;
  for (let guard = 0; guard < 500; guard += 1) {
    const end = phaseEndsAt(current);
    if (end === null || current.index !== index || current.phase === "ready" || current.phase === "done") break;
    if (!Number.isFinite(end) || end <= now || end - now > MAX_ROUND_TRACK_MS) return null;
    // Match the engine's warning rules, including coincident halfway/30s cues.
    const firstCue = cues.length;
    const marks = [...new Set([current.phaseMs! / 2, 30_000, 10_000])];
    for (const mark of marks) {
      const at = end - mark;
      if (at <= now || at <= current.phaseStartedAt!) continue;
      const crossed = crossedCues(current.phase, current.phaseMs, mark + 1, mark,
        { ten: settings.warnTen, thirty: settings.warnThirty, halfway: settings.warnHalfway }, warningLengthMs(current));
      for (const sound of cueSounds(crossed, false).sounds) cues.push({ atMs: at - now, sound });
    }
    if (settings.tick) {
      const warnings = cues.slice(firstCue).map(cue => cue.atMs);
      // Align to displayed seconds. Bells and the rest countdown take priority.
      for (let at = end - (current.phase === "rest" ? 4000 : 1000); at > Math.max(now, current.phaseStartedAt!); at -= 1000) {
        if (!warnings.includes(at - now)) cues.push({ atMs: at - now, sound: "tick" });
      }
    }
    const next = advance(current, end);
    if (next.state === current) return null;
    const sound = soundForEvents(next.events);
    if (sound) cues.push({ atMs: end - now, sound });
    durationMs = end - now;
    current = next.state;
  }
  if (!cues.length || durationMs <= 0) return null;
  return { cues: cues.sort((a, b) => a.atMs - b.atMs), durationMs: durationMs + CLIP_SECONDS * 1000 };
}

/** A standard PCM WAV. Reuse silence blobs rather than allocate a workout-sized array. */
export function roundAudioWav(timeline: RoundAudioTimeline, clips: ReadonlyMap<TimerSound, Float32Array>, volume = 1): Blob {
  const frames = Math.ceil(timeline.durationMs * SAMPLE_RATE / 1000);
  if (!Number.isFinite(frames) || frames <= 0 || 44 + frames * 2 > MAX_ROUND_TRACK_BYTES) {
    throw new RangeError("Round audio exceeds its memory budget");
  }
  const header = new ArrayBuffer(44);
  const view = new DataView(header);
  const label = (offset: number, value: string) => [...value].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  label(0, "RIFF"); view.setUint32(4, 36 + frames * 2, true); label(8, "WAVE");
  label(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, SAMPLE_RATE, true);
  view.setUint32(28, SAMPLE_RATE * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  label(36, "data"); view.setUint32(40, frames * 2, true);
  const parts: BlobPart[] = [header];
  const silence = new Blob([new Uint8Array(SAMPLE_RATE * 2 * 60)]);
  let cursor = 0;
  const appendSilence = (until: number) => {
    let bytes = (until - cursor) * 2;
    while (bytes > 0) {
      const size = Math.min(bytes, silence.size);
      parts.push(silence.slice(0, size));
      bytes -= size;
    }
    cursor = until;
  };
  // Merge overlapping clips so a short rest never truncates a ringing bell.
  for (let i = 0; i < timeline.cues.length;) {
    const first = i;
    const start = Math.round(timeline.cues[i].atMs * SAMPLE_RATE / 1000);
    let end = start;
    do {
      const cue = timeline.cues[i++];
      end = Math.max(end, Math.round(cue.atMs * SAMPLE_RATE / 1000) + (clips.get(cue.sound)?.length ?? 0));
    } while (i < timeline.cues.length && Math.round(timeline.cues[i].atMs * SAMPLE_RATE / 1000) < end);
    end = Math.min(end, frames);
    appendSilence(start);
    const mixed = new Float32Array(end - start);
    for (let j = first; j < i; j += 1) {
      const cue = timeline.cues[j];
      const clip = clips.get(cue.sound);
      if (!clip) continue;
      const offset = Math.round(cue.atMs * SAMPLE_RATE / 1000) - start;
      for (let k = 0; k < clip.length && offset + k < mixed.length; k += 1) mixed[offset + k] += clip[k];
    }
    // Safari's WAV playback must never hard-clip, even when an individual
    // synthesised triple bell already exceeds full-scale PCM. Preserve clean
    // samples unchanged; add headroom only when a peak would otherwise clip.
    const peak = mixed.reduce((max, sample) => Math.max(max, Math.abs(sample)), 0);
    const headroom = peak > 1 ? 0.95 / peak : 1;
    const pcm = new DataView(new ArrayBuffer(mixed.length * 2));
    for (let j = 0; j < mixed.length; j += 1) {
      pcm.setInt16(j * 2, Math.round(Math.max(-1, Math.min(1, mixed[j] * headroom * volume)) * 32767), true);
    }
    parts.push(pcm.buffer);
    cursor = end;
  }
  appendSilence(frames);
  return new Blob(parts, { type: "audio/wav" });
}

let clipBank: Promise<ReadonlyMap<TimerSound, Float32Array>> | null = null;
function loadClips(): Promise<ReadonlyMap<TimerSound, Float32Array>> {
  clipBank ??= Promise.all((["bell", "triple_bell", "finish", "clapper", "double_beep", "halfway", "chime", "tick"] as TimerSound[])
    .map(async (sound) => {
      const clip = await renderTimerClip(sound, SAMPLE_RATE);
      // Keep the tick short: four seconds of trailing silence would merge every
      // tick into one workout-sized temporary PCM allocation.
      return [sound, sound === "tick" ? clip.slice(0, SAMPLE_RATE / 10) : clip] as const;
    }))
    .then((entries) => new Map(entries)).catch((error: unknown) => { clipBank = null; throw error; });
  return clipBank;
}

/**
 * Real media playback carries every future bell, rather than hoping a JS callback
 * runs while hidden. The OS can still interrupt/terminate playback. No silent
 * keepalive loop, network request, worker or notification permission is involved.
 */
export class RoundAudioTrack {
  private media: HTMLAudioElement | null = null;
  private url: string | null = null;
  private generation = 0;
  private origin = 0;
  private until = 0;
  private cues: RoundAudioCue[] = [];
  private ready = false;
  private timerPaused = false;
  private onMediaPause?: () => void;
  private onMediaPlay?: () => void;

  constructor(
    private readonly status: (status: RoundAudioStatus) => void,
    private readonly clips: () => Promise<ReadonlyMap<TimerSound, Float32Array>> = loadClips,
  ) {}

  setMediaActions(onPause?: () => void, onPlay?: () => void): void {
    this.onMediaPause = onPause;
    this.onMediaPlay = onPlay;
  }

  covers(at: number): boolean {
    return Boolean(this.media && !this.media.paused && !this.media.ended &&
      at >= this.origin && at <= this.until && Math.abs(this.media.currentTime * 1000 - (at - this.origin)) < 1500);
  }

  private dueCue(sound: TimerSound, at: number): RoundAudioCue | undefined {
    return this.cues.find((cue) => cue.sound === sound &&
      at - (this.origin + cue.atMs) >= -300 && at - (this.origin + cue.atMs) < 1500);
  }

  coversCue(sound: TimerSound, at: number): boolean {
    const cue = this.dueCue(sound, at);
    const media = this.media;
    // A stalled decoder must not claim a bell it has not reached. Allow
    // moderate Safari playhead drift without synthesising a doubled strike.
    return Boolean(this.ready && cue && media && !media.paused && !media.ended &&
      !media.seeking && media.readyState >= 2 &&
      media.currentTime * 1000 >= cue.atMs - 750);
  }

  abandonStalledCue(sound: TimerSound, at: number): void {
    // If a scheduled sound is late in the media stream, discard that stream
    // before the foreground fallback rings. Otherwise its delayed bell may
    // sound again when Safari recovers.
    if (!this.ready || !this.media || !this.dueCue(sound, at) || this.coversCue(sound, at)) return;
    this.stop();
    this.status("unavailable");
  }

  stop(): void {
    this.generation += 1;
    this.ready = false;
    this.timerPaused = false;
    if (this.media) {
      this.media.onpause = this.media.onerror = this.media.onended = this.media.onplaying = this.media.onloadedmetadata = null;
      this.media.pause();
      this.media.removeAttribute("src");
      this.media.load();
      this.media.remove();
      this.media = null;
    }
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = null;
    this.until = 0;
    this.cues = [];
  }

  sync(state: TimerState, now: number, settings: TimerSoundSettings): void {
    // Preserve a paused media element so Safari's Control Centre can issue Play.
    // Only preserve a fully prepared track; cancel an in-flight render on pause.
    if (state.pausedAt !== null && this.ready && this.media && settings.sound && settings.volume > 0) {
      if (!this.timerPaused) {
        this.timerPaused = true;
        this.media.pause();
        // Keep Safari's Now Playing controls alive without retaining the
        // workout-sized WAV while paused. This silent, paused placeholder is
        // never allowed to run a clock or emit a bell.
        this.media.onloadedmetadata = null;
        if (this.url) URL.revokeObjectURL(this.url);
        this.url = URL.createObjectURL(roundAudioWav({ durationMs: 60_000, cues: [] }, new Map()));
        this.media.src = this.url;
        this.media.load();
        this.cues = [];
        this.until = 0;
      }
      this.status("idle");
      return;
    }
    this.stop();
    if (!settings.sound || settings.volume === 0 || state.pausedAt !== null ||
        state.phase === "ready" || state.phase === "done" || state.items[state.index]?.kind !== "interval") {
      this.status("idle");
      return;
    }
    const timeline = roundAudioTimeline(state, now, settings);
    if (!timeline || typeof OfflineAudioContext === "undefined" || typeof URL.createObjectURL !== "function") {
      this.status("unavailable");
      return;
    }
    const generation = this.generation;
    this.status("preparing");
    // Unlock this media element inside the Start/Resume gesture, before rendering.
    const media = document.createElement("audio");
    media.hidden = true;
    media.preload = "auto";
    media.setAttribute("aria-hidden", "true");
    media.dataset.roundAudio = "true";
    document.body.appendChild(media);
    this.media = media;
    this.url = URL.createObjectURL(roundAudioWav({ cues: [], durationMs: 100 }, new Map()));
    media.src = this.url;
    void media.play().catch(() => undefined);
    void this.clips().then(async (clips) => {
      if (generation !== this.generation) return;
      // iOS ignores HTMLMediaElement.volume; bake the selected level into PCM.
      const blob = roundAudioWav(timeline, clips, settings.volume);
      URL.revokeObjectURL(this.url!);
      this.url = URL.createObjectURL(blob);
      this.origin = now;
      this.until = now + timeline.durationMs;
      this.cues = timeline.cues;
      // Rendering/preload time must never shift the round schedule.
      const align = () => { media.currentTime = Math.max(0, (Date.now() - now) / 1000); };
      media.onloadedmetadata = align;
      media.onplaying = () => {
        if (generation !== this.generation) return;
        // Safari's own Play button must resume the workout, not only this WAV.
        if (this.timerPaused) {
          this.onMediaPlay?.();
          return;
        }
        if (Math.abs(media.currentTime * 1000 - (Date.now() - now)) >= 1500) align();
        this.status("playing");
      };
      media.src = this.url;
      await media.play();
      if (generation !== this.generation) return;
      this.ready = true;
      media.onpause = () => {
        if (generation !== this.generation || media.ended || this.timerPaused) return;
        // Native media pause, interruption, or the lock-screen Pause control
        // must freeze the actual workout clock too.
        this.status("interrupted");
        this.onMediaPause?.();
      };
      media.onerror = () => {
        if (generation !== this.generation) return;
        this.status("interrupted");
        // We cannot reliably distinguish a phone-call interruption from
        // an intentional media pause. Without bells, freeze the workout until
        // the athlete explicitly resumes it.
        this.onMediaPause?.();
      };
      media.onended = () => { this.stop(); this.status("idle"); };
      this.status("playing");
    }).catch(() => {
      if (generation !== this.generation) return;
      this.stop();
      this.status("unavailable");
    });
  }
}
