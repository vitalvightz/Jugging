"use client";

import { type KeyboardEvent, type PointerEvent, useEffect, useRef, useState } from "react";
import { placeholderPeaks, wavPeaks } from "@/lib/voice-peaks";
import styles from "./voice-note.module.css";

const BARS = 36;
const RATES = [1, 1.5, 2] as const;
// Only one voice note plays at a time, like a chat.
let playingNow: HTMLAudioElement | null = null;

export function formatClock(ms: number | null | undefined) {
  const seconds = Math.max(0, Math.round((ms ?? 0) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/**
 * A chat-style voice note: a round play button, a waveform that fills as it
 * plays (tap or drag it to jump), the time, and a speed chip. Nothing loads
 * until Play is pressed; `load` then fetches the one short-lived link (or
 * hands over a just-recorded blob) and the waveform is drawn from the audio.
 */
export function VoicePlayer({ id, load, durationMs, playLabel, seekLabel, tone = "athlete" }: {
  id: string;
  load: () => Promise<string | Blob>;
  durationMs: number | null | undefined;
  /** Accessible name for the play button ("Play voice note 1 from coach for Boxing"). */
  playLabel: string;
  seekLabel: string;
  /** Coach notes get the accent colour, so feedback stands out from the athlete's own. */
  tone?: "athlete" | "coach";
}) {
  const audio = useRef<HTMLAudioElement | null>(null);
  const objectUrl = useRef<string | null>(null);
  const wave = useRef<HTMLDivElement>(null);
  const [peaks, setPeaks] = useState<number[] | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "playing" | "paused">("idle");
  const [position, setPosition] = useState(0);
  const [length, setLength] = useState((durationMs ?? 0) / 1000);
  const [rate, setRate] = useState<(typeof RATES)[number]>(1);
  const [failed, setFailed] = useState(false);

  useEffect(() => () => {
    audio.current?.pause();
    if (playingNow === audio.current) playingNow = null;
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
  }, []);

  async function prepare() {
    if (audio.current) return audio.current;
    setState("loading");
    setFailed(false);
    const source = await load();
    let src: string;
    let bytes: ArrayBuffer | null = null;
    if (typeof source === "string") {
      try {
        const response = await fetch(source, { cache: "no-store", referrerPolicy: "no-referrer" });
        if (!response.ok) throw new Error("unavailable");
        bytes = await response.arrayBuffer();
      } catch { bytes = null; }
      if (bytes) {
        objectUrl.current = URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
        src = objectUrl.current;
      } else src = source;
    } else {
      bytes = await source.arrayBuffer();
      objectUrl.current = URL.createObjectURL(source);
      src = objectUrl.current;
    }
    if (bytes) setPeaks(wavPeaks(bytes, BARS));
    const element = new Audio();
    element.preload = "auto";
    element.src = src;
    element.playbackRate = rate;
    element.addEventListener("timeupdate", () => setPosition(element.currentTime));
    element.addEventListener("loadedmetadata", () => { if (Number.isFinite(element.duration)) setLength(element.duration); });
    element.addEventListener("pause", () => setState(current => current === "loading" ? current : "paused"));
    element.addEventListener("play", () => setState("playing"));
    element.addEventListener("ended", () => { setState("paused"); setPosition(0); element.currentTime = 0; });
    audio.current = element;
    return element;
  }

  async function toggle() {
    try {
      const element = await prepare();
      if (!element.paused) { element.pause(); return; }
      if (playingNow && playingNow !== element) playingNow.pause();
      playingNow = element;
      await element.play();
    } catch (error) {
      // A pause pressed while play() was starting isn't a failure.
      if (error instanceof DOMException && error.name === "AbortError") return;
      setFailed(true);
      if (audio.current?.readyState) { setState("paused"); return; }
      setState("idle");
      audio.current = null;
    }
  }

  function seekTo(fraction: number) {
    const element = audio.current;
    if (!element || !length) return;
    element.currentTime = Math.max(0, Math.min(length, fraction * length));
    setPosition(element.currentTime);
  }

  function pointer(event: PointerEvent<HTMLDivElement>) {
    if (!audio.current) { void toggle(); return; }
    const box = wave.current!.getBoundingClientRect();
    seekTo((event.clientX - box.left) / box.width);
  }

  function keys(event: KeyboardEvent<HTMLDivElement>) {
    if (!audio.current) return;
    const step = event.key === "ArrowRight" ? 5 : event.key === "ArrowLeft" ? -5 : 0;
    if (!step) return;
    event.preventDefault();
    seekTo((audio.current.currentTime + step) / (length || 1));
  }

  function cycleRate() {
    const next = RATES[(RATES.indexOf(rate) + 1) % RATES.length];
    setRate(next);
    if (audio.current) audio.current.playbackRate = next;
  }

  const bars = peaks ?? placeholderPeaks(id, BARS);
  const progress = length ? Math.min(1, position / length) : 0;
  const started = state !== "idle";
  return <div className={styles.player} data-tone={tone} data-state={state}>
    <button type="button" className={styles.play} onClick={() => void toggle()} aria-label={state === "playing" ? playLabel.replace(/^Play/, "Pause") : playLabel}
      aria-busy={state === "loading" || undefined}>
      {state === "loading" ? <span className={styles.spinner} aria-hidden="true" />
        : state === "playing" ? <span className={styles.pauseIcon} aria-hidden="true" />
        : <span className={styles.playIcon} aria-hidden="true" />}
    </button>
    <div ref={wave} className={styles.wave} role="slider" tabIndex={started ? 0 : -1} aria-label={seekLabel}
      aria-valuemin={0} aria-valuemax={Math.round(length)} aria-valuenow={Math.round(position)}
      aria-valuetext={`${formatClock(position * 1000)} of ${formatClock(length * 1000)}`}
      data-real={peaks ? true : undefined} onPointerDown={pointer} onKeyDown={keys}>
      {bars.map((height, index) => <span key={index} className={styles.bar}
        data-played={index / bars.length < progress || undefined} style={{ height: `${Math.round(height * 100)}%` }} />)}
    </div>
    <span className={styles.time}>{formatClock((started && position ? position : length) * 1000)}</span>
    {started ? <button type="button" className={styles.rate} onClick={cycleRate} aria-label={`Playback speed ${rate} times`}>
      {rate}×</button> : null}
    {failed ? <span className={styles.failed} role="alert">Couldn&apos;t play. Try again.</span> : null}
  </div>;
}
