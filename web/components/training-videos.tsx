"use client";

import { useEffect, useRef, useState, type DragEvent } from "react";
import {
  ApiError, attachFighterVoiceNote, attachTrainingFile, attachTrainingVideo, getAttachmentSettings, getPrivateMediaSettings,
  getPrivateUpload, listFighterTrainingAttachments, listTrainingAttachments, playFighterTrainingAttachment,
  playTrainingAttachment, processFileUpload, removeFighterTrainingAttachment, removeTrainingAttachment,
  reserveDocumentUpload, reserveFileUpload, reservePrivateUpload, type AttachmentTarget,
} from "@/lib/api";
import type { TrainingAttachment } from "@/lib/api-schema.generated";
import { formatDuration, photoProblem, preparePhoto, VOICE_MAX_SECONDS, VoiceRecorder } from "@/lib/attachments";
import { describeLoadFailure } from "@/lib/connectivity";
import { formatAppDate } from "@/lib/date-format";
import { formatBytes, pdfProblem, renderPdfPages } from "@/lib/pdf-pages";
import { getSupabaseBrowserClient } from "@/lib/supabase";
import { PdfViewer } from "./pdf-viewer";
import { PhotoViewer } from "./photo-viewer";
import { VoicePlayer } from "./voice-note";
import { SLIDE_TO_CANCEL_PX, useHoldToRecord } from "@/lib/use-hold-to-record";
import { PrivateVideoPlayer } from "./private-exercise-video";
import styles from "./training-videos.module.css";

export const MAX_VIDEOS = 6;
export const MAX_FILES = MAX_VIDEOS;
/** Progress photos on an athlete's profile (the database enforces the same number). */
export const PROGRESS_PHOTO_LIMIT = 24;
const MAX_BYTES = 20 * 1024 * 1024;
const VIDEO_TYPES = ["video/mp4", "video/quicktime", "video/webm"];
const POLL_MS = 2_000;
const POLL_LIMIT = 60;

export type Kind = "video" | "photo" | "voice" | "pdf";
type Kinds = Record<Kind, boolean>;
const NONE: Kinds = { video: false, photo: false, voice: false, pdf: false };

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
        voice: fileSettings?.voice_available === true, pdf: fileSettings?.pdfs_available === true };
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

/**
 * PDFs: the browser draws each page to a fresh JPEG and drops the original.
 * Every page goes to quarantine, is rebuilt one at a time on the server, then
 * the server writes a new image-only PDF. Any bad page rejects the whole file.
 */
async function uploadDocument(token: string, file: File, attach: (assetId: string) => Promise<TrainingAttachment>,
  onStage: (stage: string) => void) {
  onStage("Reading PDF…");
  const { pages, totalBytes } = await renderPdfPages(file, (page, total) => onStage(`Preparing ${page} of ${total}…`));
  const permission = await reserveDocumentUpload(token, totalBytes, pages.length, file.name);
  const storage = getSupabaseBrowserClient().storage.from(permission.bucket);
  for (const [index, page] of pages.entries()) {
    onStage(`Uploading ${index + 1} of ${pages.length}…`);
    const { error } = await storage.upload(`${permission.path}/${String(index + 1).padStart(3, "0")}.jpg`, page,
      { contentType: "image/jpeg", upsert: false });
    if (error) throw new Error("Upload failed. Check your connection and try again.");
  }
  for (let page = 1; page <= pages.length; page += 1) {
    onStage(`Checking ${page} of ${pages.length}…`);
    const result = await processFileUpload(token, permission.asset_id, page);
    if (result.status !== "page_ready") throw new Error(result.reason || "That PDF could not be used.");
  }
  onStage("Finishing…");
  const result = await processFileUpload(token, permission.asset_id);
  if (result.status !== "ready") throw new Error(result.reason || "That PDF could not be used.");
  return attach(permission.asset_id);
}

/** Saves a file from a one-off link. The link itself forces a download; nothing opens inline. */
function download(url: string, name: string) {
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.rel = "noopener noreferrer";
  document.body.append(link);
  link.click();
  link.remove();
}

export function videoProblem(file: File): string | null {
  if (!VIDEO_TYPES.includes(file.type)) return "Choose an MP4, MOV or WebM video.";
  if (file.size > MAX_BYTES) return "That video is over 20 MB. Trim it to 30 seconds or less.";
  return null;
}

const NOUN: Record<Kind, string> = { video: "Video", photo: "Photo", voice: "Voice note", pdf: "PDF" };
const kindOf = (row: TrainingAttachment): Kind => (row.kind ?? "video") as Kind;

type Recording = { stage: "recording"; seconds: number } | { stage: "preview"; wav: Blob; url: string; durationMs: number };

/**
 * Files on one logged record: a session, one exercise in it, or a sparring
 * report. The athlete attaches photos, videos, voice notes and PDFs and
 * removes anything; a coach sees them only while the athlete shares media
 * (enforced by the API), and can leave voice feedback and share PDFs.
 */
export function TrainingVideos({ token, target, viewer = "athlete", athleteId, label, heading, only, limit = MAX_FILES, emptyText, allowAdding = true }: {
  token: string; target: AttachmentTarget; viewer?: "athlete" | "coach"; athleteId?: string;
  /** What the files are for, used in accessible names ("Back squat"). */
  label: string;
  /** A section title. With one, Attach sits beside it at the top instead of under the files. */
  heading?: string;
  /** Limits what can be attached here (progress photos are photos only). */
  only?: Kind[];
  /** How many files this place holds (the database enforces the same number). */
  limit?: number;
  /** The headed empty state, when the default sentence doesn't fit. */
  emptyText?: string;
  /** The overview can show files without a second upload control. */
  allowAdding?: boolean;
}) {
  const [rows, setRows] = useState<TrainingAttachment[]>([]);
  const [kinds, setKinds] = useState<Kinds>(NONE);
  const [stage, setStage] = useState<string | null>(null);
  const [busyKind, setBusyKind] = useState<Kind | null>(null);
  const [fetching, setFetching] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [opened, setOpened] = useState<TrainingAttachment | null>(null);
  const [menu, setMenu] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [recording, setRecording] = useState<Recording | null>(null);
  const videoInput = useRef<HTMLInputElement>(null);
  const photoInput = useRef<HTMLInputElement>(null);
  const pdfInput = useRef<HTMLInputElement>(null);
  const recorder = useRef<VoiceRecorder | null>(null);
  const recorderReady = useRef(false);
  const [viewing, setViewing] = useState<string | null>(null);
  const [reading, setReading] = useState<string | null>(null);
  const opening = useRef<string | null>(null);
  const athlete = viewer === "athlete";
  const { source, target_id: targetId, exercise_ref: exerciseRef = "" } = target;
  const key: AttachmentTarget = { source, target_id: targetId, exercise_ref: exerciseRef };

  useEffect(() => {
    const controller = new AbortController();
    const request = athlete ? listTrainingAttachments(token, key, controller.signal)
      : athleteId ? listFighterTrainingAttachments(token, athleteId, key, controller.signal) : Promise.resolve([]);
    request.then(list => { if (!controller.signal.aborted) setRows(Array.isArray(list) ? list : []); })
      .catch(() => { if (!controller.signal.aborted) setRows([]); });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, athlete, athleteId, source, targetId, exerciseRef]);

  useEffect(() => {
    if (!allowAdding) return;
    let active = true;
    void availableKinds(token, athlete).then(value => { if (active) setKinds(value); });
    return () => { active = false; };
  }, [token, athlete, allowAdding]);

  // Leaving the record mid-recording releases the microphone and the preview.
  useEffect(() => () => { recorder.current?.cancel(); recorder.current = null; }, []);
  useEffect(() => () => { if (recording?.stage === "preview") URL.revokeObjectURL(recording.url); }, [recording]);

  const busy = stage !== null;
  const full = rows.length >= limit;
  // Coaches leave voice notes and share PDFs, only on a fighter whose files they can see.
  const offered = (["photo", "voice", "pdf", "video"] as Kind[]).filter(kind => allowAdding && kinds[kind]
    && (!only || only.includes(kind)) && (athlete || ((kind === "voice" || kind === "pdf") && !!athleteId)));
  const attachable = !full && offered.length > 0;

  function added(row: TrainingAttachment) {
    setRows(current => [...current.filter(item => item.id !== row.id), row]);
  }

  async function run(task: () => Promise<TrainingAttachment>, fallback: string, kind: Kind = "photo") {
    setProblem(null);
    setBusyKind(kind);
    try {
      added(await task());
      return true;
    } catch (error) {
      // Upload limits: a plain sentence, not the server's message and request id.
      setProblem(error instanceof ApiError && error.status === 429
        ? "You've sent a lot of files in the last hour. Try again a bit later."
        : describeLoadFailure(error, error instanceof Error && error.message ? error.message : fallback));
      return false;
    } finally {
      setStage(null);
      setBusyKind(null);
    }
  }

  async function attachVideo(file: File | undefined) {
    if (!file || busy) return;
    const reason = videoProblem(file);
    setProblem(reason);
    if (!reason) await run(() => uploadVideo(token, file, key, setStage), "Video could not be attached. Try again.", "video");
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

  async function attachPdf(file: File | undefined) {
    if (!file || busy) return;
    const reason = pdfProblem(file);
    setProblem(reason);
    if (!reason) {
      const attach = (assetId: string) => athlete ? attachTrainingFile(token, assetId, key)
        : attachFighterVoiceNote(token, athleteId ?? "", assetId, key);
      await run(() => uploadDocument(token, file, attach, setStage), "PDF could not be attached. Try again.", "pdf");
    }
    if (pdfInput.current) pdfInput.current.value = "";
  }

  async function startRecording() {
    setProblem(null);
    setMenu(false);
    recorder.current?.cancel();
    const next = new VoiceRecorder(() => void stopRecording());
    recorder.current = next;
    recorderReady.current = false;
    try {
      await next.start();
    } catch {
      next.cancel();
      if (recorder.current !== next) return;
      recorder.current = null;
      setProblem("Microphone unavailable. Allow microphone access to record a voice note.");
      return;
    }
    if (recorder.current !== next) { next.cancel(); return; }
    recorderReady.current = true;
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
    await sendVoice(recording.wav);
  }

  // Held mic let go: send straight away (slid away: drop it). If the upload
  // fails, the note stays as a preview with Send and Discard.
  async function finishHeld(send: boolean) {
    const active = recorder.current;
    if (!active) return;
    if (!send || !recorderReady.current) {
      discardRecording();
      if (send) setProblem("Hold the mic while you talk, then let go to send.");
      return;
    }
    recorder.current = null;
    const { wav, durationMs } = await active.stop();
    if (durationMs < 500) { setRecording(null); setProblem("That was too short. Hold on a little longer."); return; }
    setRecording({ stage: "preview", wav, url: URL.createObjectURL(wav), durationMs });
    await sendVoice(wav);
  }
  const hold = useHoldToRecord(() => void startRecording(), send => void finishHeld(send));

  async function sendVoice(wav: Blob) {
    const attach = (assetId: string) => athlete ? attachTrainingFile(token, assetId, key)
      : attachFighterVoiceNote(token, athleteId ?? "", assetId, key);
    const sent = await run(() => uploadFile(token, "voice", wav, attach, setStage), "Voice note could not be sent. Try again.", "voice");
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
    else if (kind === "pdf") pdfInput.current?.click();
    else if (kind === "video") videoInput.current?.click();
    else void startRecording();
  }

  async function open(row: TrainingAttachment) {
    // Lists carry no links. A 60-second link is issued for this one file only
    // now, after the server re-checks ownership (and a coach's consent).
    setProblem(null);
    if (kindOf(row) === "pdf") {
      // PDFs are never shown here: the link only ever downloads the rebuilt copy.
      if (fetching) return;
      setFetching(row.id);
      try {
        const fresh = athlete ? await playTrainingAttachment(token, row.id)
          : await playFighterTrainingAttachment(token, athleteId ?? "", row.id);
        if (!fresh.file?.url) throw new Error("PDF unavailable. Try again.");
        download(fresh.file.url, fresh.file.name ?? "Document.pdf");
      } catch (error) {
        setProblem(describeLoadFailure(error, "PDF unavailable. Try again."));
      } finally {
        setFetching(null);
      }
      return;
    }
    // Photos open full screen; the viewer fetches each full photo when it's shown.
    if (kindOf(row) === "photo") { setViewing(row.id); return; }
    opening.current = row.id;
    try {
      const fresh = athlete ? await playTrainingAttachment(token, row.id)
        : await playFighterTrainingAttachment(token, athleteId ?? "", row.id);
      // A slow answer for a file the person has already moved on from is dropped.
      if (opening.current !== row.id) return;
      setOpened(fresh);
    } catch (error) {
      if (opening.current !== row.id) return;
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
    if (file.type.startsWith("video/") && offered.includes("video")) void attachVideo(file);
    else if ((file.type === "application/pdf" || /\.pdf$/i.test(file.name)) && offered.includes("pdf")) void attachPdf(file);
    else if (file.type.startsWith("image/") && offered.includes("photo")) void attachPhoto(file);
    else setProblem(`Drop ${dropKinds.map(kind => kind === "pdf" ? "a PDF" : `a ${NOUN[kind].toLowerCase()}`).join(" or ")}.`);
  }

  const tiles = rows.filter(row => kindOf(row) === "photo" || kindOf(row) === "video");
  const notes = rows.filter(row => kindOf(row) === "voice");
  const docs = rows.filter(row => kindOf(row) === "pdf");
  const whoFor = (row: TrainingAttachment) => row.author === "coach" ? (athlete ? "Coach" : "You") : (athlete ? "You" : "Athlete");
  const pendingTile = busy && !recording && busyKind !== "pdf";
  const pendingDoc = busy && busyKind === "pdf";
  const numbered = (row: TrainingAttachment) => rows.filter(item => kindOf(item) === kindOf(row)).indexOf(row) + 1;
  const visualOpen = opened && kindOf(opened) === "video" ? opened : null;
  const photos = rows.filter(row => kindOf(row) === "photo");
  const readingRow = rows.find(row => row.id === reading && kindOf(row) === "pdf") ?? null;
  async function photoUrl(row: TrainingAttachment) {
    const fresh = athlete ? await playTrainingAttachment(token, row.id)
      : await playFighterTrainingAttachment(token, athleteId ?? "", row.id);
    if (!fresh.file?.url) throw new Error("File unavailable.");
    return fresh.file.url;
  }

  if (!rows.length && !attachable && !busy && !recording) return null;
  // Voice notes also get their own mic: hold to record, release to send, slide left to
  // cancel. The menu's "Voice note" (and Enter or Space on the mic) still records with
  // Stop, a preview and Send. With voice alone, the mic is the only button.
  const voiceOffered = offered.includes("voice");
  const files = offered.length === 1 && voiceOffered ? [] : offered;
  const singleLabel = files.length !== 1 ? "" : files[0] === "pdf" ? "Attach PDF" : `Attach ${NOUN[files[0]].toLowerCase()}`;
  const micButton = voiceOffered ? <button type="button" className={`${styles.attach} ${styles.mic}`} disabled={busy}
    aria-label={files.length ? "Hold to record a voice note" : "Record voice note"} title="Hold to record" data-only={files.length ? undefined : true}
    onPointerDown={event => { if (!busy && !recorder.current) hold.onPointerDown(event); }}
    onContextMenu={event => event.preventDefault()}
    onKeyDown={event => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      // A held key repeats: never restart a recording that is already running.
      if (!event.repeat && !recorder.current) void startRecording();
    }}>
    <MicIcon />{files.length ? null : <span aria-hidden="true">Hold to record</span>}
  </button> : null;
  const attachControl = attachable && !recording ? <div className={styles.attachWrap}>
    {files.length ? <button type="button" className={styles.attach} disabled={busy} aria-label={singleLabel || undefined} aria-haspopup={files.length > 1 ? "menu" : undefined}
      aria-expanded={files.length > 1 ? menu : undefined}
      onClick={() => files.length > 1 ? setMenu(value => !value) : choose(files[0])}>
      <PaperclipIcon />
      {files.length > 1 ? <span className={styles.attachText}>Attach</span> : <>
        <span className={styles.attachFull}>{singleLabel}</span>
        {/* On the narrowest cards the title keeps its room and the button says just "Attach". */}
        <span className={styles.attachShort} aria-hidden="true">Attach</span>
      </>}
    </button> : null}
    {micButton}
    {menu ? <div className={styles.menu} data-align={heading ? "end" : undefined} role="menu" aria-label={`Attach to ${label}`}>
      {files.map(kind => <button key={kind} type="button" role="menuitem" className={styles.menuItem} onClick={() => choose(kind)}>
        {kind === "photo" ? <PhotoIcon /> : kind === "voice" ? <MicIcon /> : kind === "pdf" ? <DocIcon /> : <VideoIcon />}
        {NOUN[kind]}
      </button>)}
    </div> : null}
  </div> : null;
  return <div className={styles.videos} data-headed={heading ? true : undefined} data-dragging={dragging || undefined}
    onDragOver={attachable && dropKinds.length ? event => { event.preventDefault(); setDragging(true); } : undefined}
    onDragLeave={attachable ? () => setDragging(false) : undefined} onDrop={drop}>
    {heading ? <div className={styles.head}>
      {/* The count sits under the title, so the title keeps the width beside Attach. */}
      <div className={styles.headText}>
        <h3 className={styles.headTitle} title={heading}>{heading}</h3>
        {rows.length ? <span className={styles.headCount}>{rows.length} of {limit}</span> : null}
      </div>
      {attachControl}
    </div> : null}
    {tiles.length || pendingTile ? <ul className={styles.grid} aria-label={`Photos and videos for ${label}`}>
      {tiles.map(row => <li key={row.id}>
        <button type="button" className={styles.thumb} aria-pressed={opened?.id === row.id}
          aria-label={`${kindOf(row) === "photo" ? "Open photo" : "Play video"} ${numbered(row)} for ${label}`}
          onClick={() => void open(row)}>
          {/* Photos show a small preview rebuilt from the clean file; videos load nothing until played. */}
          {kindOf(row) === "photo" && row.file?.thumb_url
            // A plain <img>: next/image would route the private, short-lived link through the image optimizer.
            // eslint-disable-next-line @next/next/no-img-element
            ? <img className={styles.thumbImage} src={row.file.thumb_url} alt="" loading="lazy" decoding="async"
                referrerPolicy="no-referrer" draggable={false} />
            : <>
              {kindOf(row) === "photo" ? <PhotoIcon /> : <span className={styles.play} aria-hidden="true" />}
              <span className={styles.thumbLabel} aria-hidden="true">{NOUN[kindOf(row)]} {numbered(row)}</span>
            </>}
        </button>
      </li>)}
      {pendingTile ? <li><span className={styles.pending} role="status">{stage}</span></li> : null}
    </ul> : null}
    {readingRow ? <PdfViewer name={readingRow.file?.name ?? "Document.pdf"} pages={readingRow.file?.pages ?? 1}
      load={() => photoUrl(readingRow)} onDownload={() => void open(readingRow)} onClose={() => setReading(null)} /> : null}
    {viewing && photos.some(photo => photo.id === viewing) ? <PhotoViewer photos={photos} startId={viewing} label={label}
      load={photoUrl} onRemove={row => remove(row)} onClose={() => setViewing(null)} /> : null}
    {visualOpen ? <div className={styles.player}>
      {visualOpen.media ? <PrivateVideoPlayer key={visualOpen.media.url ?? visualOpen.id} media={visualOpen.media} name={label} kind="Form check" /> : null}
      <div className={styles.playerSide}>
        {athlete ? <p className={styles.who}>Only you and coaches you share photos and voice notes with can see this.</p> : null}
        <div className={styles.playerActions}>
          {visualOpen.removable ? <button type="button" className={styles.textAction} onClick={() => void remove(visualOpen)}>
            Remove {NOUN[kindOf(visualOpen)].toLowerCase()}</button> : null}
          <button type="button" className={styles.textAction} onClick={() => { opening.current = null; setOpened(null); }}>Close</button>
        </div>
      </div>
    </div> : null}
    {notes.length ? <ul className={styles.notes} aria-label={`Voice notes for ${label}`}>
      {notes.map(row => {
        const who = whoFor(row);
        return <li key={row.id} className={styles.note} data-author={row.author}>
          <div className={styles.noteHead}>
            <span className={styles.noteWho}>{who}</span>
            <span className={styles.noteDate}>{formatAppDate(row.created_at.slice(0, 10)).split(" ").slice(1, 3).join(" ")}</span>
            {row.removable ? <button type="button" className={styles.noteRemove} onClick={() => void remove(row)}
              aria-label={`Remove voice note ${numbered(row)}`}><TrashIcon /></button> : null}
          </div>
          <VoicePlayer id={row.id} durationMs={row.file?.duration_ms} tone={row.author === "coach" ? "coach" : "athlete"}
            playLabel={`Play voice note ${numbered(row)} from ${who.toLowerCase()} for ${label}`}
            seekLabel={`Position in voice note ${numbered(row)}`}
            load={async () => {
              const fresh = athlete ? await playTrainingAttachment(token, row.id)
                : await playFighterTrainingAttachment(token, athleteId ?? "", row.id);
              if (!fresh.file?.url) throw new Error("Voice note unavailable.");
              return fresh.file.url;
            }} />
        </li>;
      })}
    </ul> : null}
    {docs.length || pendingDoc ? <ul className={styles.docs} aria-label={`PDFs for ${label}`}>
      {docs.map(row => {
        const name = row.file?.name ?? "Document.pdf";
        const pages = row.file?.pages ?? 0;
        const meta = [pages ? `${pages} ${pages === 1 ? "page" : "pages"}` : "", formatBytes(row.file?.size_bytes), whoFor(row)].filter(Boolean);
        return <li key={row.id} className={styles.doc}>
          {/* The row opens the PDF in the app; Download stays beside it. */}
          <button type="button" className={styles.docOpen} onClick={() => setReading(row.id)} aria-label={`View ${name}`}>
            <span className={styles.docIcon} aria-hidden="true"><DocIcon /></span>
            <span className={styles.docText}>
              <span className={styles.docName} title={name}>{name}</span>
              <span className={styles.docMeta}>{meta.join(" · ")}</span>
            </span>
          </button>
          <button type="button" className={styles.docDownload} onClick={() => void open(row)}
            aria-label={`Download ${name}`} aria-busy={fetching === row.id || undefined} disabled={fetching === row.id}>
            <DownloadIcon /><span className={styles.docDownloadLabel}>Download</span>
          </button>
          {row.removable ? <button type="button" className={styles.docRemove} onClick={() => void remove(row)}
            aria-label={`Remove ${name}`}><TrashIcon /></button>
            // Keeps Download lined up across rows that can't be removed.
            : <span className={styles.docRemove} aria-hidden="true" />}
        </li>;
      })}
      {pendingDoc ? <li className={`${styles.doc} ${styles.docPending}`}>
        <span className={styles.docIcon} aria-hidden="true"><DocIcon /></span>
        <span className={styles.docMeta} role="status">{stage}</span>
      </li> : null}
    </ul> : null}
    {recording ? <div className={styles.recorder} role="group" aria-label={`Voice note for ${label}`}>
      {recording.stage === "recording" && hold.holding ? <>
        <span className={styles.recDot} aria-hidden="true" />
        <span className={styles.recTime} role="timer" aria-live="off">{formatDuration(recording.seconds * 1000)}</span>
        <span className={styles.slideHint} data-cancelling={hold.cancelling || undefined} aria-live="polite"
          style={{ transform: `translateX(${-Math.min(hold.slide, SLIDE_TO_CANCEL_PX)}px)` }}>
          {hold.cancelling ? "Release to cancel" : "‹ Slide to cancel"}
        </span>
        <span className={styles.heldMic} aria-hidden="true"><MicIcon /></span>
      </> : recording.stage === "recording" ? <>
        <span className={styles.recDot} aria-hidden="true" />
        <span className={styles.recTime} role="timer" aria-live="off">
          {formatDuration(recording.seconds * 1000)} <span className={styles.recLimit}>/ {formatDuration(VOICE_MAX_SECONDS * 1000)}</span>
        </span>
        <button type="button" className={styles.textAction} onClick={discardRecording}>Cancel</button>
        <button type="button" className={styles.attach} onClick={() => void stopRecording()}>
          <span className={styles.stopIcon} aria-hidden="true" />Stop
        </button>
      </> : <>
        <div className={styles.previewPlayer} role="group" aria-label="Preview your voice note">
          <VoicePlayer id="preview" durationMs={recording.durationMs} playLabel="Play your voice note"
            seekLabel="Position in your voice note" load={async () => recording.wav} />
        </div>
        <div className={styles.recActions}>
          {busy ? <span className={styles.stage} role="status">{stage}</span>
            : <button type="button" className={styles.textAction} onClick={discardRecording}>Discard</button>}
          <button type="button" className={styles.attach} disabled={busy} onClick={() => void sendRecording()}>Send</button>
        </div>
      </>}
    </div> : null}
    {!heading && ((attachable && !recording) || rows.length) ? <div className={styles.bar}>
      {attachControl}
      {attachable && !recording && dropKinds.length ? <span className={styles.dropHint}>or drop one here</span> : null}
      {rows.length ? <span className={styles.count}>{rows.length} of {limit}</span> : null}
    </div> : null}
    {heading && emptyText !== "" && !rows.length && attachable && !busy && !recording ? <p className={styles.empty}>
      {emptyText ?? (athlete ? `Add ${describeKinds(offered)} to this ${label === "Sparring" ? "report" : "session"}.`
        : `Share ${describeKinds(offered)} with your fighter.`)}</p> : null}
    {allowAdding && kinds.photo && athlete ? <input ref={photoInput} type="file" accept="image/jpeg,image/png,image/heic,image/heif,image/webp" hidden
      aria-label={`Attach a photo to ${label}`} onChange={event => void attachPhoto(event.target.files?.[0])} /> : null}
    {allowAdding && kinds.pdf && (athlete || athleteId) ? <input ref={pdfInput} type="file" accept="application/pdf,.pdf" hidden
      aria-label={`Attach a PDF to ${label}`} onChange={event => void attachPdf(event.target.files?.[0])} /> : null}
    {allowAdding && kinds.video && athlete ? <input ref={videoInput} type="file" accept="video/mp4,video/quicktime,video/webm" hidden
      aria-label={`Attach a video to ${label}`} onChange={event => void attachVideo(event.target.files?.[0])} /> : null}
    {problem ? <p className={styles.problem} role="alert">{problem}</p> : null}
    {dragging ? <div className={styles.dropOverlay} aria-hidden="true">Drop to attach</div> : null}
  </div>;
}

export const TrainingAttachments = TrainingVideos;

/** "photos, voice notes or PDFs" for the kinds on offer. */
function describeKinds(kinds: Kind[]) {
  const words = kinds.map(kind => kind === "pdf" ? "PDFs" : `${NOUN[kind].toLowerCase()}s`);
  return words.length > 1 ? `${words.slice(0, -1).join(", ")} or ${words.at(-1)}` : words[0] ?? "files";
}

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
function DocIcon() {
  return <svg {...iconProps}><path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z" /><path d="M14 3v5h5M9 13h6M9 17h4" /></svg>;
}
function DownloadIcon() {
  return <svg {...iconProps}><path d="M12 4v11M7 10l5 5 5-5M5 20h14" /></svg>;
}
function TrashIcon() {
  return <svg {...iconProps}><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 002 2h6a2 2 0 002-2l1-12M9 7V4h6v3" /></svg>;
}
function VideoIcon() {
  return <svg {...iconProps}><rect x="3" y="6" width="13" height="12" rx="2" /><path d="M16 10l5-3v10l-5-3" /></svg>;
}
