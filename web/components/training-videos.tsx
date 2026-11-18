"use client";

import { useEffect, useRef, useState, type DragEvent } from "react";
import {
  getPrivateMediaSettings, listFighterTrainingAttachments, listTrainingAttachments, removeTrainingAttachment,
  type AttachmentTarget,
} from "@/lib/api";
import type { TrainingAttachment } from "@/lib/api-schema.generated";
import { isBrowserOffline, isNetworkFailure } from "@/lib/connectivity";
import {
  queuedVideos, queueVideo, removeQueuedVideo, sendingVideos, tokenSubject, uploadAndAttach,
  VIDEO_QUEUE_EVENT, type QueuedVideo,
} from "@/lib/video-queue";
import { PrivateVideoPlayer } from "./private-exercise-video";
import styles from "./training-videos.module.css";

export const MAX_VIDEOS = 6;
const MAX_BYTES = 20 * 1024 * 1024;
const VIDEO_TYPES = ["video/mp4", "video/quicktime", "video/webm"];
const UPLOADS_KEY = "unlxck:video-uploads-available";

// One settings read per signed-in view, shared by every record on screen.
// Offline, the last answer seen online decides, so Attach still shows.
const settingsByToken = new Map<string, Promise<boolean>>();
function uploadsAvailable(token: string) {
  let pending = settingsByToken.get(token);
  if (!pending) {
    pending = getPrivateMediaSettings(token).then(settings => {
      try { localStorage.setItem(UPLOADS_KEY, settings.uploads_available ? "1" : "0"); } catch { /* optional */ }
      return settings.uploads_available;
    }, () => {
      settingsByToken.delete(token);
      try { return localStorage.getItem(UPLOADS_KEY) === "1"; } catch { return false; }
    });
    settingsByToken.set(token, pending);
  }
  return pending;
}

export function videoProblem(file: File): string | null {
  if (!VIDEO_TYPES.includes(file.type)) return "Choose an MP4, MOV or WebM video.";
  if (file.size > MAX_BYTES) return "That video is over 20 MB. Trim it to 30 seconds or less.";
  return null;
}

/**
 * Form-check videos on one logged record: a session, one exercise in it, or a
 * sparring report. The athlete attaches and removes; a coach only watches,
 * and only while the athlete shares injury status (enforced by the API).
 */
export function TrainingVideos({ token, target, viewer = "athlete", athleteId, label }: {
  token: string; target: AttachmentTarget; viewer?: "athlete" | "coach"; athleteId?: string;
  /** What the videos are for, used in accessible names ("Back squat"). */
  label: string;
}) {
  const [videos, setVideos] = useState<TrainingAttachment[]>([]);
  const [canUpload, setCanUpload] = useState(false);
  const [stage, setStage] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [playing, setPlaying] = useState<TrainingAttachment | null>(null);
  const [dragging, setDragging] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [held, setHeld] = useState<QueuedVideo[]>([]);
  const input = useRef<HTMLInputElement>(null);
  const athlete = viewer === "athlete";
  const { source, target_id: targetId, exercise_ref: exerciseRef = "" } = target;
  const userId = athlete ? tokenSubject(token) : null;

  // Videos held on this device while offline. When one is sent, the list of
  // attached videos is read again so it moves from "Queued" to a thumbnail.
  useEffect(() => {
    if (!userId) return;
    let active = true;
    let count = -1;
    const read = () => void queuedVideos(userId, { source, target_id: targetId, exercise_ref: exerciseRef }).then(rows => {
      if (!active) return;
      if (count > rows.filter(row => !row.failed).length) setAttempt(value => value + 1);
      count = rows.filter(row => !row.failed).length;
      setHeld(rows);
    });
    read();
    window.addEventListener(VIDEO_QUEUE_EVENT, read);
    return () => { active = false; window.removeEventListener(VIDEO_QUEUE_EVENT, read); };
  }, [userId, source, targetId, exerciseRef]);

  useEffect(() => {
    const controller = new AbortController();
    const key = { source, target_id: targetId, exercise_ref: exerciseRef };
    const request = athlete ? listTrainingAttachments(token, key, controller.signal)
      : athleteId ? listFighterTrainingAttachments(token, athleteId, key, controller.signal) : Promise.resolve([]);
    request.then(rows => { if (!controller.signal.aborted) setVideos(rows); })
      .catch(() => { if (!controller.signal.aborted) setVideos([]); });
    return () => controller.abort();
  }, [token, athlete, athleteId, source, targetId, exerciseRef, attempt]);

  useEffect(() => {
    let active = true;
    if (athlete) void uploadsAvailable(token).then(value => { if (active) setCanUpload(value); });
    return () => { active = false; };
  }, [token, athlete]);

  const busy = stage !== null;
  const full = videos.length + held.length >= MAX_VIDEOS;
  const attachable = athlete && canUpload && !full;

  async function attach(file: File | undefined) {
    if (!file || busy) return;
    const reason = videoProblem(file);
    setProblem(reason);
    if (reason) return;
    const key = { source, target_id: targetId, exercise_ref: exerciseRef };
    const hold = async () => {
      if (!userId) throw new Error("Reconnect to attach this video.");
      await queueVideo(userId, file, key);
    };
    try {
      if (isBrowserOffline()) await hold();
      else {
        const added = await uploadAndAttach(token, file, file.type, key, setStage);
        setVideos(current => [...current.filter(row => row.id !== added.id), added]);
      }
    } catch (error) {
      if (isBrowserOffline() || isNetworkFailure(error)) {
        try { await hold(); } catch (held) { setProblem(held instanceof Error ? held.message : "Reconnect to attach this video."); }
      } else setProblem(error instanceof Error && error.message ? error.message : "Video could not be attached. Try again.");
    } finally {
      setStage(null);
      if (input.current) input.current.value = "";
    }
  }

  async function open(video: TrainingAttachment) {
    // Private links last 60 seconds: re-read so the player gets a fresh one.
    try {
      const key = { source, target_id: targetId, exercise_ref: exerciseRef };
      const rows = athlete ? await listTrainingAttachments(token, key)
        : await listFighterTrainingAttachments(token, athleteId ?? "", key);
      setVideos(rows);
      setPlaying(rows.find(row => row.id === video.id) ?? null);
    } catch { setProblem("Video unavailable. Try again."); }
  }

  async function remove(video: TrainingAttachment) {
    try {
      await removeTrainingAttachment(token, video.id);
      setVideos(current => current.filter(row => row.id !== video.id));
      setPlaying(null);
    } catch { setProblem("Video could not be removed. Try again."); }
  }

  function drop(event: DragEvent<HTMLDivElement>) {
    if (!attachable) return;
    event.preventDefault();
    setDragging(false);
    void attach(event.dataTransfer.files[0]);
  }

  const waiting = held.filter(row => !row.failed);
  const refused = held.filter(row => row.failed);
  if (!videos.length && !held.length && !attachable && !busy) return null;
  return <div className={styles.videos} data-dragging={dragging || undefined}
    onDragOver={attachable ? event => { event.preventDefault(); setDragging(true); } : undefined}
    onDragLeave={attachable ? () => setDragging(false) : undefined} onDrop={drop}>
    {videos.length || held.length || busy ? <ul className={styles.grid} aria-label={`Videos for ${label}`}>
      {videos.map((video, index) => <li key={video.id}>
        <button type="button" className={styles.thumb} aria-pressed={playing?.id === video.id}
          aria-label={`Play video ${index + 1} for ${label}`} onClick={() => void open(video)}>
          {video.media.url ? <video src={`${video.media.url}#t=0.1`} muted playsInline preload="metadata" tabIndex={-1} /> : null}
          <span className={styles.play} aria-hidden="true" />
        </button>
      </li>)}
      {held.map(row => <li key={row.id}>{row.failed
        ? <button type="button" className={styles.pending} data-failed="" aria-label={`Remove video that could not be attached to ${label}`}
          onClick={() => void removeQueuedVideo(row.id)}><CrossIcon />Remove</button>
        : <span className={styles.pending}><CloudIcon />{sendingVideos.has(row.id) ? "Uploading…" : "Queued"}</span>}</li>)}
      {busy ? <li><span className={styles.pending} role="status">{stage}</span></li> : null}
    </ul> : null}
    {playing ? <div className={styles.player}>
      <PrivateVideoPlayer key={playing.media.url ?? playing.id} media={playing.media} name={label} kind="Form check" />
      <div className={styles.playerSide}>
      {athlete ? <p className={styles.who}>Only you and coaches you share injury status with can watch this.</p> : null}
      <div className={styles.playerActions}>
        {athlete ? <button type="button" className={styles.textAction} onClick={() => void remove(playing)}>Remove video</button> : null}
        <button type="button" className={styles.textAction} onClick={() => setPlaying(null)}>Close</button>
      </div>
      </div>
    </div> : null}
    {athlete && (attachable || videos.length || held.length) ? <div className={styles.bar}>
      {attachable ? <>
        <button type="button" className={styles.attach} disabled={busy} onClick={() => input.current?.click()}>
          <PaperclipIcon />Attach video
        </button>
        <span className={styles.dropHint}>or drop one here</span>
        <input ref={input} type="file" accept="video/mp4,video/quicktime,video/webm" hidden
          aria-label={`Attach a video to ${label}`} onChange={event => void attach(event.target.files?.[0])} />
      </> : null}
      {videos.length + held.length ? <span className={styles.count}>{videos.length + held.length} of {MAX_VIDEOS}</span> : null}
    </div> : null}
    {waiting.length ? <p className={styles.note} role="status">{waiting.length === 1 ? "1 video uploads" : `${waiting.length} videos upload`} when you&apos;re back online.</p> : null}
    {refused.length ? <p className={styles.problem} role="alert">{refused[0].failed}</p> : null}
    {problem ? <p className={styles.problem} role="alert">{problem}</p> : null}
    {dragging ? <div className={styles.dropOverlay} aria-hidden="true">Drop video to attach</div> : null}
  </div>;
}

function PaperclipIcon() {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M21.4 11.1l-9.2 9.2a6 6 0 01-8.5-8.5l9.2-9.2a4 4 0 015.7 5.7l-9.2 9.2a2 2 0 01-2.8-2.8l8.5-8.5" />
  </svg>;
}

function CloudIcon() {
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M7 18a4.5 4.5 0 01-.6-8.96A6 6 0 0118 9.5a4 4 0 01-.5 8.5H7z" /><path d="M12 15v-5m-2 2l2-2 2 2" />
  </svg>;
}

function CrossIcon() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>;
}
