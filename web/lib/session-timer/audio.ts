/**
 * Synthesised gym sounds for the session timer.
 *
 * Everything is generated with Web Audio at play time: no sample files to
 * license, download or cache, and it works offline in the installed PWA.
 *
 * Browsers only allow audio after a user gesture, so `unlock()` must be called
 * synchronously inside the Start / Resume tap. On iOS the ringer switch mutes
 * Web Audio unless the page declares itself a playback session (Safari 17+).
 */

export type TimerSound =
  | "bell"
  | "round_start"
  | "triple_bell"
  | "clapper"
  | "beep"
  | "double_beep"
  | "halfway"
  | "go"
  | "chime"
  | "soft_chime"
  | "finish"
  | "tick";

export type TimerSoundSettings = {
  sound: boolean;
  /** 0..1 */
  volume: number;
  voice: boolean;
  vibrate: boolean;
  /** Round warnings: the ten-second clapper, a 30-second double beep, a halfway chime. */
  warnTen: boolean;
  warnThirty: boolean;
  warnHalfway: boolean;
  /** Optional quiet one-second clock pulse. */
  tick?: boolean;
  /** Countdown before round 1, so the athlete can put the phone down and get set. */
  prepSec: PrepSeconds;
};

export const PREP_OPTIONS = [0, 5, 10, 30] as const;
export type PrepSeconds = (typeof PREP_OPTIONS)[number];

export const DEFAULT_SOUND_SETTINGS: TimerSoundSettings = {
  sound: true,
  volume: 0.9,
  voice: false,
  vibrate: true,
  warnTen: true,
  warnThirty: false,
  warnHalfway: false,
  tick: false,
  prepSec: 10,
};

function flag(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

const SETTINGS_KEY = "unlxck.session-timer.settings";

export function loadSoundSettings(): TimerSoundSettings {
  try {
    const raw = window.localStorage.getItem(SETTINGS_KEY);
    if (!raw) return DEFAULT_SOUND_SETTINGS;
    const parsed = JSON.parse(raw) as Partial<TimerSoundSettings>;
    return {
      sound: flag(parsed.sound, DEFAULT_SOUND_SETTINGS.sound),
      volume:
        typeof parsed.volume === "number" && parsed.volume >= 0 && parsed.volume <= 1
          ? parsed.volume
          : DEFAULT_SOUND_SETTINGS.volume,
      voice: flag(parsed.voice, DEFAULT_SOUND_SETTINGS.voice),
      vibrate: flag(parsed.vibrate, DEFAULT_SOUND_SETTINGS.vibrate),
      warnTen: flag(parsed.warnTen, DEFAULT_SOUND_SETTINGS.warnTen),
      warnThirty: flag(parsed.warnThirty, DEFAULT_SOUND_SETTINGS.warnThirty),
      warnHalfway: flag(parsed.warnHalfway, DEFAULT_SOUND_SETTINGS.warnHalfway),
      tick: flag(parsed.tick, false),
      prepSec: PREP_OPTIONS.includes(parsed.prepSec as PrepSeconds)
        ? (parsed.prepSec as PrepSeconds)
        : DEFAULT_SOUND_SETTINGS.prepSec,
    };
  } catch {
    return DEFAULT_SOUND_SETTINGS;
  }
}

export function saveSoundSettings(settings: TimerSoundSettings): void {
  try {
    window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // Settings are a convenience; a blocked store just means defaults next time.
  }
}

/** Classic struck-bell partial ratios; the inharmonic spread is what reads as "bell". */
const BELL_PARTIALS: Array<[ratio: number, gain: number, decay: number]> = [
  [1, 1, 2.4],
  [2, 0.55, 1.6],
  [2.76, 0.42, 1.2],
  [4.07, 0.26, 0.8],
  [5.4, 0.18, 0.55],
  [8.93, 0.08, 0.3],
];

const VIBRATION: Partial<Record<TimerSound, number[]>> = {
  bell: [250],
  round_start: [250],
  triple_bell: [180, 90, 180, 90, 180],
  clapper: [60, 60, 60],
  double_beep: [90, 90, 90],
  halfway: [150],
  go: [120],
  finish: [300, 120, 300],
};

type AudioSessionNavigator = Navigator & { audioSession?: { type: string } };
type WebkitWindow = Window & { webkitAudioContext?: typeof AudioContext };

class TimerAudio {
  private context: AudioContext | OfflineAudioContext | null = null;
  private master: GainNode | null = null;
  private settings: TimerSoundSettings = DEFAULT_SOUND_SETTINGS;

  private connect(context: AudioContext | OfflineAudioContext): void {
    this.context = context;
    const compressor = context.createDynamicsCompressor();
    compressor.threshold.value = -14;
    compressor.knee.value = 8;
    compressor.ratio.value = 4;
    this.master = context.createGain();
    this.master.gain.value = this.settings.volume;
    this.master.connect(compressor);
    compressor.connect(context.destination);
  }

  /** Render the same gym sounds into reusable PCM clips for media playback. */
  static async renderClip(sound: TimerSound, sampleRate: number): Promise<Float32Array> {
    const context = new OfflineAudioContext(1, sampleRate * 4, sampleRate);
    const audio = new TimerAudio();
    audio.settings = { ...DEFAULT_SOUND_SETTINGS, volume: 1 };
    audio.connect(context);
    audio.soundAt(sound, 0.01);
    const rendered = await context.startRendering();
    return rendered.getChannelData(0).slice();
  }

  configure(settings: TimerSoundSettings): void {
    this.settings = settings;
    if (this.master && this.context) {
      this.master.gain.setValueAtTime(settings.volume, this.context.currentTime);
    }
  }

  /** Call inside a tap handler, before any await. */
  unlock(): void {
    if (typeof window === "undefined") return;
    try {
      const session = (navigator as AudioSessionNavigator).audioSession;
      if (session) session.type = "playback";
    } catch {
      // Unsupported: the ringer switch may mute the timer on older iOS.
    }
    if (!this.context) {
      const Context = window.AudioContext ?? (window as WebkitWindow).webkitAudioContext;
      if (!Context) return;
      this.connect(new Context());
    }
    const context = this.context!;
    if (context.state !== "running" && context.state !== "closed") {
      void context.resume().catch(() => undefined);
    }
    // A silent one-sample buffer fully unlocks playback on older WebKit.
    const buffer = context.createBuffer(1, 1, 22050);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    source.start(0);
  }

  /** A silent buzz, when the athlete keeps vibration on (best effort). */
  buzz(pattern: number[]): void {
    if (!this.settings.vibrate || typeof navigator === "undefined" || !("vibrate" in navigator)) return;
    try {
      navigator.vibrate(pattern);
    } catch {
      // Vibration is best effort (unsupported on iOS).
    }
  }

  play(sound: TimerSound): void {
    this.buzzSound(sound);
    const ctx = this.context;
    if (!this.settings.sound || !ctx || !this.master) return;
    if (ctx.state !== "running" && ctx.state !== "closed") void ctx.resume().catch(() => undefined);
    this.soundAt(sound, ctx.currentTime + 0.01);
  }

  buzzSound(sound: TimerSound): void {
    const pattern = VIBRATION[sound];
    if (pattern) this.buzz(pattern);
  }

  private soundAt(sound: TimerSound, t: number): void {
    switch (sound) {
      case "tick":
        this.tone(t, 1700, 0.035, 0.11, "triangle");
        break;
      case "bell":
        this.bell(t, 1);
        break;
      case "round_start":
        // UNLXCK's start cue: a low, decisive strike under the ring bell.
        this.tone(t, 160, 0.14, 0.38, "triangle");
        this.bell(t, 1);
        break;
      case "triple_bell":
        this.bell(t, 1);
        this.bell(t + 0.32, 0.95);
        this.bell(t + 0.64, 0.95);
        break;
      // Warnings and counts are pitched to cut through a gym at about the
      // bell's loudness: a quiet cue is a missing cue.
      case "clapper":
        this.clack(t);
        this.clack(t + 0.14);
        this.clack(t + 0.28);
        break;
      case "beep":
        this.tone(t, 880, 0.22, 2.4, "square");
        break;
      case "double_beep":
        this.tone(t, 988, 0.17, 1.7, "square");
        this.tone(t + 0.25, 988, 0.17, 1.7, "square");
        break;
      case "halfway":
        this.tone(t, 1046.5, 0.7, 0.3, "sine");
        this.tone(t + 0.18, 1568, 0.8, 0.26, "sine");
        break;
      case "go":
        this.tone(t, 1320, 0.3, 1.5, "square");
        break;
      case "chime":
        this.tone(t, 1046.5, 0.9, 0.45, "sine");
        this.tone(t + 0.12, 1568, 0.9, 0.35, "sine");
        break;
      case "soft_chime":
        this.tone(t, 1318.5, 0.7, 0.25, "sine");
        break;
      case "finish":
        this.bell(t, 1);
        this.bell(t + 0.32, 0.95);
        this.bell(t + 0.64, 0.95);
        this.tone(t + 1.1, 1046.5, 1.2, 0.35, "sine");
        this.tone(t + 1.1, 1318.5, 1.2, 0.3, "sine");
        this.tone(t + 1.1, 1568, 1.2, 0.25, "sine");
        break;
    }
  }

  say(text: string): void {
    if (!this.settings.voice || typeof window === "undefined" || !("speechSynthesis" in window)) {
      return;
    }
    try {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = 1.05;
      utterance.volume = this.settings.volume;
      window.speechSynthesis.speak(utterance);
    } catch {
      // Speech is optional.
    }
  }

  /** A boxing-ring bell: inharmonic partials, hard strike, long ring. */
  private bell(at: number, level: number): void {
    const ctx = this.context!;
    const base = 740;
    for (const [ratio, gain, decay] of BELL_PARTIALS) {
      const osc = ctx.createOscillator();
      const env = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(base * ratio, at);
      env.gain.setValueAtTime(0.0001, at);
      env.gain.exponentialRampToValueAtTime(0.32 * gain * level, at + 0.004);
      env.gain.exponentialRampToValueAtTime(0.0001, at + decay);
      osc.connect(env);
      env.connect(this.master!);
      osc.start(at);
      osc.stop(at + decay + 0.05);
    }
    // The hammer strike: a very short bright noise transient.
    this.noise(at, 0.025, 3200, 1.2, 0.35 * level);
  }

  /** Two wooden sticks knocked together (the ten-second warning). */
  private clack(at: number): void {
    this.noise(at, 0.08, 2200, 2, 2.4);
    this.tone(at, 1900, 0.07, 0.9, "triangle");
  }

  private tone(at: number, frequency: number, length: number, level: number, type: OscillatorType): void {
    const ctx = this.context!;
    const osc = ctx.createOscillator();
    const env = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(frequency, at);
    env.gain.setValueAtTime(0.0001, at);
    env.gain.exponentialRampToValueAtTime(level * (type === "square" ? 0.35 : 1), at + 0.008);
    env.gain.exponentialRampToValueAtTime(0.0001, at + length);
    osc.connect(env);
    env.connect(this.master!);
    osc.start(at);
    osc.stop(at + length + 0.05);
  }

  private noise(at: number, length: number, frequency: number, q: number, level: number): void {
    const ctx = this.context!;
    const frames = Math.max(1, Math.floor(ctx.sampleRate * length));
    const buffer = ctx.createBuffer(1, frames, ctx.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < frames; i += 1) {
      data[i] = (Math.random() * 2 - 1) * (1 - i / frames);
    }
    const source = ctx.createBufferSource();
    const filter = ctx.createBiquadFilter();
    const env = ctx.createGain();
    source.buffer = buffer;
    filter.type = "bandpass";
    filter.frequency.value = frequency;
    filter.Q.value = q;
    env.gain.setValueAtTime(level, at);
    env.gain.exponentialRampToValueAtTime(0.0001, at + length);
    source.connect(filter);
    filter.connect(env);
    env.connect(this.master!);
    source.start(at);
    source.stop(at + length + 0.02);
  }
}

let shared: TimerAudio | null = null;

export function timerAudio(): TimerAudio {
  shared ??= new TimerAudio();
  return shared;
}

export const renderTimerClip = TimerAudio.renderClip;
