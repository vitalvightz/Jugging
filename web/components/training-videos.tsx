"use client";

import { useEffect, useRef, useState, type DragEvent } from "react";
import {
  attachFighterVoiceNote, attachTrainingFile, attachTrainingVideo, getAttachmentSettings, getPrivateMediaSettings,
  getPrivateUpload, listFighterTrainingAttachments, listTrainingAttachments, playFighterTrainingAttachment,
  playTrainingAttachment, processFileUpload, removeFighterTrainingAttachment, removeTrainingAttachment,
  reserveFileUpload, reservePrivateUpload, type AttachmentTarget,
} from "@/lib/api";
import type { TrainingAttachment } from "@/lib/api-schema.generated";
import { formatDuration, photoProblem, preparePhoto, VOICE_MAX_SECONDS, VoiceRecorder } from "@/lib/attachments";
import { describeLoadFailure } from "@/lib/connectivity";
import { getSupabaseBrowserClient } from "@/lib/supabase";
import { PrivateVideoPlayer } from "./private-exercise-video";
import styles from "./training-videos.module.css";

export const MAX_VIDEOS = 6;
export const MAX_FILES = MAX_VIDEOS;
const MAX_BYTES = 20 * 1024 * 1024;
const VIDEO_TYPES = ["video/mp4", "video/quicktime", "video/webm"];
const POLL_MS = 2_000;
const POLL_LIMIT = 60;

type Kind = "video" | "photo" | "voice";
type Kinds = Record<Kind, boolean>;
const NONE: Kinds = { video: false, photo: false, voice: false };

// One settings read per signed-in view and viewer, shared by every record on
// screen. Fails closed: if the server can't say a type is on, it stays hidden.
const kindsByKey = new Map<string, Promise<Kinds>>();
function availableKinds(token: string, athlete: boolean) {
  const key = `${athlete ? "a" : "c"}:${token}`;
  let pending = kindsByKey.get(key);
  if (!pending) {
    const video = athlete ? getPrivateMediaSettings(token).then(s => s.uploads_available === true, () => null) : Promise.resolve(false);
    const files = getAttachmentSettings(token).then(s => s, () => null);
    pending = Promise.all([video, files]).then(([videoOn, fileSettings]) => {
      if (videoOn === null || fileSettings === null) kindsByKey.delete(key);
      return { video: videoOn === true, photo: athlete && fileSettings?.photos_available === true,
        voice: fileSettings?.voice_available === true };
    });
    kindsByKey.set(key, pending);
  }
  return pending;
}

const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

/**
 * Reserve, upload straight to the private quarantine bucket, wait for the
 * isolated worker to scan and rebuild it, then attach the sanitized result.
 * The browser never keeps a copy, and nothing is playable until verified.
 */
async function uploadVideo(token: string, file: File, target: AttachmentTarget, onStage: (stage: string) => void) {
  onStage("Uploading…");
  const permission = await reservePrivateUpload(token, file);
  const { error } = await getSupabaseBrowserClient().storage.from(permission.bucket)
    .upload(permission.path, file, { contentType: file.type, upsert: false });
  if (error) throw new Error("Upload failed. Check your connection and try again.");
  onStage("Checking…");
  for (let attempt = 0; attempt < POLL_LIMIT; attempt += 1) {
    const state = await getPrivateUpload(token, permission.asset_id);
    if (state.status === "verified" && state.media) return attachTrainingVideo(token, state.media.id, target);
    if (state.status === "rejected" || state.status === "deleted") {
      throw new Error("That video could not be used. Use a clip of 30 seconds or less.");
    }
    await wait(POLL_MS);
  }
  throw new Error("Checking is taking longer than usual. Try again in a minute.");
}

/**
 * Photos and voice notes: reserve a server-chosen path, upload to quarantine,
 * have the server rebuild it in isolation, then attach the clean copy.
 */
async function uploadFile(token: string, kind: "photo" | "voice", blob: Blob, attach: (assetId: string) => Promise<TrainingAttachment>,
  onStage: (stage: string) => void) {
  onStage("Uploading…");
  const permission = await reserveFileUpload(token, kind, blob.size);
  const { error } = await getSupabaseBrowserClient().storage.from(permission.bucket)
    .upload(permission.path, blob, { contentType: permission.content_type, upsert: false });
  if (error) throw new Error("Upload failed. Check your connection and try again.");
  onStage("Checking…");
  const result = await processFileUpload(token, permission.asset_id);
  if (result.status !== "ready") throw new Error(result.reason || "That file could not be used.");
  return attach(permission.asset_id);
}

export function videoProblem(file: File): string | null {
  if (!VIDEO_TYPES.includes(file.type)) return "Choose an MP4, MOV or WebM video.";
  if (file.size > MAX_BYTES) return "That video is over 20 MB. Trim it to 30 seconds or less.";
  return null;
}

const NOUN: Record<Kind, string> = { video: "Video", photo: "Photo", voice: "Voice note" };
const kindOf = (row: TrainingAttachment): Kind => (row.kind ?? "video") as Kind;

type Recording = { stage: "recording"; seconds: number } | { stage: "preview"; wav: Blob; url: string; durationMs: number };

/**
 * Files on one logged record: a session, one exercise in it, or a sparring
 * report. The athlete attaches photos, videos and voice notes and removes
 * anything; a coach sees them only while the athlete shares photos and voice
 * notes (enforced by the API), and can leave voice feedback.
 */
export function TrainingVideos({ token, target, viewer = "athlete", athleteId, label }: {
  token: string; target: AttachmentTarget; viewer?: "athlete" | "coach"; athleteId?: string;
  /** What the files are for, used in accessible names ("Back squat"). */
  label: string;
}) {
  const [rows, setRows] = useState<TrainingAttachment[]>([]);
  const [kinds, setKinds] = useState<Kinds>(NONE);
  const [stage, setStage] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [opened, setOpened] = useState<TrainingAttachment | null>(null);
  const [menu, setMenu] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [recording, setRecording] = useState<Recording | null>(null);
  const videoInput = useRef<HTMLInputElement>(null);
  const photoInput = useRef<HTMLInputElement>(null);
  const recorder = useRef<VoiceRecorder | null>(null);
  const athlete = viewer === "athlete";
  const { source, target_id: targetId, exercise_ref: exerciseRef = "" } = target;
  const key: AttachmentTarget = { source, target_id: targetId, exercise_ref: exerciseRef };

  useEffect(() => {
    const controller = new AbortController();
    const request = athlete ? listTrainingAttachments(token, key, controller.signal)
      : athleteId ? listFighterTrainingAttachments(token, athleteId, key, controller.signal) : Promise.resolve([]);
    request.then(list => { if (!controller.signal.aborted) setRows(list); })
      .catch(() => { if (!controller.signal.aborted) setRows([]); });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, athlete, athleteId, source, targetId, exerciseRef]);

  useEffect(() => {
    let active = true;
    void availableKinds(token, athlete).then(value => { if (active) setKinds(value); });
    return () => { active = false; };
  }, [token, athlete]);

  // Leaving the record mid-recording releases the microphone and the preview.
  useEffect(() => () => { recorder.current?.cancel(); }, []);
  useEffect(() => () => { if (recording?.stage === "preview") URL.revokeObjectURL(recording.url); }, [recording]);

  const busy = stage !== null;
  const full = rows.length >= MAX_FILES;
  // Coaches only leave voice notes, and only on a fighter whose files they can see.
  const offered = (["photo", "voice", "video"] as Kind[]).filter(kind => kinds[kind] && (athlete || (kind === "voice" && !!athleteId)));
  const attachable = !full && offered.length > 0;

  function added(row: TrainingAttachment) {
    setRows(current => [...current.filter(item => item.id !== row.id), row]);
  }

  async function run(task: () => Promise<TrainingAttachment>, fallback: string) {
    setProblem(null);
    try {
      added(await task());
      return true;
    } catch (error) {
      setProblem(describeLoadFailure(error, error instanceof Error && error.message ? error.message : fallback));
      return false;
    } finally {
      setStage(null);
    }
  }

  async function attachVideo(file: File | undefined) {
    if (!file || busy) return;
    const reason = videoProblem(file);
    setProblem(reason);
    if (!reason) await run(() => uploadVideo(token, file, key, setStage), "Video could not be attached. Try again.");
    if (videoInput.current) videoInput.current.value = "";
  }

  async function attachPhoto(file: File | undefined) {
    if (!file || busy) return;
    const reason = photoProblem(file);
    setProblem(reason);
    if (!reason) {
      await run(async () => {
        setStage("Preparing…");
        const jpeg = await preparePhoto(file);
        return uploadFile(token, "photo", jpeg, assetId => attachTrainingFile(token, assetId, key), setStage);
      }, "Photo could not be attached. Try again.");
    }
    if (photoInput.current) photoInput.current.value = "";
  }

  async function startRecording() {
    setProblem(null);
    setMenu(false);
    const next = new VoiceRecorder(() => void stopRecording());
    try {
      await next.start();
    } catch {
      next.cancel();
      setProblem("Microphone unavailable. Allow microphone access to record a voice note.");
      return;
    }
    recorder.current = next;
    setRecording({ stage: "recording", seconds: 0 });
  }

  async function stopRecording() {
    const active = recorder.current;
    if (!active) return;
    recorder.current = null;
    const { wav, durationMs } = await active.stop();
    if (durationMs < 500) { setRecording(null); setProblem("That was too short. Hold on a little longer."); return; }
    setRecording({ stage: "preview", wav, url: URL.createObjectURL(wav), durationMs });
  }

  function discardRecording() {
    recorder.current?.cancel();
    recorder.current = null;
    setRecording(null);
  }

  async function sendRecording() {
    if (recording?.stage !== "preview" || busy) return;
    const { wav } = recording;
    const attach = (assetId: string) => athlete ? attachTrainingFile(token, assetId, key)
      : attachFighterVoiceNote(token, athleteId ?? "", assetId, key);
    const sent = await run(() => uploadFile(token, "voice", wav, attach, setStage), "Voice note could not be sent. Try again.");
    if (sent) setRecording(null);
  }

  // The elapsed time while recording; stops itself at the limit.
  useEffect(() => {
    if (recording?.stage !== "recording") return;
    const timer = setInterval(() => {
      const seconds = recorder.current?.seconds ?? 0;
      setRecording(current => current?.stage === "recording" ? { stage: "recording", seconds } : current);
    }, 250);
    return () => clearInterval(timer);
  }, [recording?.stage]);

  function choose(kind: Kind) {
    setMenu(false);
    if (kind === "photo") photoInput.current?.click();
    else if (kind === "video") videoInput.current?.click();
    else void startRecording();
  }

  async function open(row: TrainingAttachment) {
    // Lists carry no links. A 60-second link is issued for this one file only
    // now, after the server re-checks ownership (and a coach's consent).
    setProblem(null);
    try {
      setOpened(athlete ? await playTrainingAttachment(token, row.id)
        : await playFighterTrainingAttachment(token, athleteId ?? "", row.id));
    } catch (error) {
      setOpened(null);
      setProblem(describeLoadFailure(error, `${NOUN[kindOf(row)]} unavailable. Try again.`));
    }
  }

  async function remove(row: TrainingAttachment) {
    try {
      if (athlete) await removeTrainingAttachment(token, row.id);
      else await removeFighterTrainingAttachment(token, athleteId ?? "", row.id);
      setRows(current => current.filter(item => item.id !== row.id));
      setOpened(null);
    } catch { setProblem(`${NOUN[kindOf(row)]} could not be removed. Try again.`); }
  }

  const dropKinds = offered.filter(kind => kind !== "voice");
  function drop(event: DragEvent<HTMLDivElement>) {
    if (!attachable || !dropKinds.length) return;
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files[0];
    if (!file) return;
    if (file.type.startsWith("video/") && kinds.video && athlete) void attachVideo(file);
    else if (kinds.photo && athlete) void attachPhoto(file);
    else setProblem("Drop a photo or a video.");
  }

  const tiles = rows.filter(row => kindOf(row) !== "voice");
  const notes = rows.filter(row => kindOf(row) === "voice");
  const numbered = (row: TrainingAttachment) => rows.filter(item => kindOf(item) === kindOf(row)).indexOf(row) + 1;
  const visualOpen = opened && kindOf(opened) !== "voice" ? opened : null;

  if (!rows.length && !attachable && !busy && !recording) return null;
  return <div className={styles.videos} data-dragging={dragging || undefined}
    onDragOver={attachable && dropKinds.length ? event => { event.preventDefault(); setDragging(true); } : undefined}
    onDragLeave={attachable ? () => setDragging(false) : undefined} onDrop={drop}>
    {tiles.length || (busy && !recording) ? <ul className={styles.grid} aria-label={`Photos and videos for ${label}`}>
      {tiles.map(row => <li key={row.id}>
        <button type="button" className={styles.thumb} aria-pressed={opened?.id === row.id}
          aria-label={`${kindOf(row) === "photo" ? "Open photo" : "Play video"} ${numbered(row)} for ${label}`}
          onClick={() => void open(row)}>
          {/* No preview: nothing loads until the file is asked for. */}
          {kindOf(row) === "photo" ? <PhotoIcon /> : <span className={styles.play} aria-hidden="true" />}
          <span className={styles.thumbLabel} aria-hidden="true">{NOUN[kindOf(row)]} {numbered(row)}</span>
        </button>
      </li>)}
      {busy && !recording ? <li><span className={styles.pending} role="status">{stage}</span></li> : null}
    </ul> : null}
    {visualOpen ? <div className={styles.player}>
      {kindOf(visualOpen) === "photo" && visualOpen.file?.url
        // A plain <img>: next/image would route the private, short-lived link through the image optimizer.
        // eslint-disable-next-line @next/next/no-img-element
        ? <img className={styles.photo} src={visualOpen.file.url} alt={`Photo ${numbered(rows.find(r => r.id === visualOpen.id) ?? visualOpen)} for ${label}`}
            referrerPolicy="no-referrer" draggable={false} />
        : visualOpen.media ? <PrivateVideoPlayer key={visualOpen.media.url ?? visualOpen.id} media={visualOpen.media} name={label} kind="Form check" /> : null}
      <div className={styles.playerSide}>
        {athlete ? <p className={styles.who}>Only you and coaches you share photos and voice notes with can see this.</p> : null}
        <div className={styles.playerActions}>
          {visualOpen.removable ? <button type="button" className={styles.textAction} onClick={() => void remove(visualOpen)}>
            Remove {NOUN[kindOf(visualOpen)].toLowerCase()}</button> : null}
          <button type="button" className={styles.textAction} onClick={() => setOpened(null)}>Close</button>
        </div>
      </div>
    </div> : null}
    {notes.length ? <ul className={styles.notes} aria-label={`Voice notes for ${label}`}>
      {notes.map(row => {
        const playing = opened?.id === row.id && opened.file?.url;
        const who = row.author === "coach" ? (athlete ? "Coach" : "You") : (athlete ? "You" : "Athlete");
        return <li key={row.id} className={styles.note}>
          {playing ? <audio className={styles.audio} src={opened.file!.url!} controls autoPlay
            aria-label={`Voice note from ${who.toLowerCase()} for ${label}`} />
            : <button type="button" className={styles.noteButton} onClick={() => void open(row)}
              aria-label={`Play voice note ${numbered(row)} from ${who.toLowerCase()} for ${label}`}>
              <span className={styles.playSmall} aria-hidden="true" />
              <span className={styles.noteLabel}>Voice note</span>
              <span className={styles.noteMeta}>{who}</span>
              <span className={styles.noteTime}>{formatDuration(row.file?.duration_ms)}</span>
            </button>}
          {playing ? <div className={styles.noteActions}>
            {row.removable ? <button type="button" className={styles.textAction} onClick={() => void remove(row)}>Remove</button> : null}
            <button type="button" className={styles.textAction} onClick={() => setOpened(null)}>Close</button>
          </div> : null}
        </li>;
      })}
    </ul> : null}
    {recording ? <div className={styles.recorder} role="group" aria-label={`Voice note for ${label}`}>
      {recording.stage === "recording" ? <>
        <span className={styles.recDot} aria-hidden="true" />
        <span className={styles.recTime} role="timer" aria-live="off">
          {formatDuration(recording.seconds * 1000)} <span className={styles.recLimit}>/ {formatDuration(VOICE_MAX_SECONDS * 1000)}</span>
        </span>
        <button type="button" className={styles.textAction} onClick={discardRecording}>Cancel</button>
        <button type="button" className={styles.attach} onClick={() => void stopRecording()}>
          <span className={styles.stopIcon} aria-hidden="true" />Stop
        </button>
      </> : <>
        <audio className={styles.audio} src={recording.url} controls aria-label="Preview your voice note" />
        <div className={styles.recActions}>
          {busy ? <span className={styles.stage} role="status">{stage}</span>
            : <button type="button" className={styles.textAction} onClick={discardRecording}>Discard</button>}
          <button type="button" className={styles.attach} disabled={busy} onClick={() => void sendRecording()}>Send</button>
        </div>
      </>}
    </div> : null}
    {(attachable && !recording) || rows.length ? <div className={styles.bar}>
      {attachable && !recording ? <div className={styles.attachWrap}>
        <button type="button" className={styles.attach} disabled={busy} aria-haspopup={offered.length > 1 ? "menu" : undefined}
          aria-expanded={offered.length > 1 ? menu : undefined}
          onClick={() => offered.length > 1 ? setMenu(value => !value) : choose(offered[0])}>
          {offered.length === 1 && offered[0] === "voice" ? <MicIcon /> : <PaperclipIcon />}
          {offered.length > 1 ? "Attach" : offered[0] === "voice" ? "Record voice note" : `Attach ${NOUN[offered[0]].toLowerCase()}`}
        </button>
        {menu ? <div className={styles.menu} role="menu" aria-label={`Attach to ${label}`}>
          {offered.map(kind => <button key={kind} type="button" role="menuitem" className={styles.menuItem} onClick={() => choose(kind)}>
            {kind === "photo" ? <PhotoIcon /> : kind === "voice" ? <MicIcon /> : <VideoIcon />}
            {kind === "photo" ? "Photo" : kind === "voice" ? "Voice note" : "Video"}
          </button>)}
        </div> : null}
      </div> : null}
      {attachable && !recording && dropKinds.length ? <span className={styles.dropHint}>or drop one here</span> : null}
      {rows.length ? <span className={styles.count}>{rows.length} of {MAX_FILES}</span> : null}
    </div> : null}
    {kinds.photo && athlete ? <input ref={photoInput} type="file" accept="image/jpeg,image/png,image/heic,image/heif,image/webp" hidden
      aria-label={`Attach a photo to ${label}`} onChange={event => void attachPhoto(event.target.files?.[0])} /> : null}
    {kinds.video && athlete ? <input ref={videoInput} type="file" accept="video/mp4,video/quicktime,video/webm" hidden
      aria-label={`Attach a video to ${label}`} onChange={event => void attachVideo(event.target.files?.[0])} /> : null}
    {problem ? <p className={styles.problem} role="alert">{problem}</p> : null}
    {dragging ? <div className={styles.dropOverlay} aria-hidden="true">Drop to attach</div> : null}
  </div>;
}

export const TrainingAttachments = TrainingVideos;

const iconProps = {
  width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2,
  strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true,
};

function PaperclipIcon() {
  return <svg {...iconProps}><path d="M21.4 11.1l-9.2 9.2a6 6 0 01-8.5-8.5l9.2-9.2a4 4 0 015.7 5.7l-9.2 9.2a2 2 0 01-2.8-2.8l8.5-8.5" /></svg>;
}
function PhotoIcon() {
  return <svg {...iconProps}><rect x="3" y="5" width="18" height="14" rx="2" /><circle cx="9" cy="10" r="1.6" /><path d="M21 16l-5-5-8 8" /></svg>;
}
function MicIcon() {
  return <svg {...iconProps}><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0014 0M12 18v3" /></svg>;
}
function VideoIcon() {
  return <svg {...iconProps}><rect x="3" y="6" width="13" height="12" rx="2" /><path d="M16 10l5-3v10l-5-3" /></svg>;
}
