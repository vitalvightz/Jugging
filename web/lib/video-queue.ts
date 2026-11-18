"use client";

// Form-check videos picked while offline wait on this device, then upload and
// attach on their own once the connection is back (next time the app is open
// and online). Entries belong to one signed-in athlete and are wiped on sign
// out, so a shared phone never uploads one person's video as another.

import {
  attachTrainingVideo, getPrivateUpload, reservePrivateUpload, type AttachmentTarget,
} from "@/lib/api";
import type { TrainingAttachment } from "@/lib/api-schema.generated";
import { isBrowserOffline, isNetworkFailure, RETRYABLE_NETWORK_MESSAGE } from "@/lib/connectivity";
import { getSupabaseBrowserClient } from "@/lib/supabase";

export const VIDEO_QUEUE_EVENT = "unlxck:video-queue";

export type QueuedVideo = {
  id: string;
  user_id: string;
  target: Required<AttachmentTarget>;
  file: Blob;
  type: string;
  created_at: number;
  /** Set when the server refused the video; it is kept so the athlete sees why. */
  failed?: string;
};

export type VideoQueueStorage = {
  all(): Promise<QueuedVideo[]>;
  put(entry: QueuedVideo): Promise<void>;
  remove(id: string): Promise<void>;
  clear(): Promise<void>;
};

const DB = "unlxck-video-queue";
const STORE = "pending";

function request<T>(run: (store: IDBObjectStore) => IDBRequest<T>, mode: IDBTransactionMode): Promise<T> {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open(DB, 1);
    open.onupgradeneeded = () => { open.result.createObjectStore(STORE, { keyPath: "id" }); };
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
      const db = open.result;
      const tx = db.transaction(STORE, mode);
      const req = run(tx.objectStore(STORE));
      tx.oncomplete = () => { db.close(); resolve(req.result); };
      tx.onerror = () => { db.close(); reject(tx.error); };
    };
  });
}

const indexedDbStorage: VideoQueueStorage = {
  all: () => request(store => store.getAll() as IDBRequest<QueuedVideo[]>, "readonly"),
  put: entry => request(store => store.put(entry), "readwrite").then(() => undefined),
  remove: id => request(store => store.delete(id), "readwrite").then(() => undefined),
  clear: () => request(store => store.clear(), "readwrite").then(() => undefined),
};

let storage: VideoQueueStorage | null = typeof indexedDB === "undefined" ? null : indexedDbStorage;

/** Tests run without IndexedDB and pass an in-memory store. */
export function setVideoQueueStorage(next: VideoQueueStorage | null) { storage = next; }
export function videoQueueAvailable() { return storage !== null; }

function changed() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(VIDEO_QUEUE_EVENT));
}

/** The signed-in user's id, read from the access token (no network). */
export function tokenSubject(token: string): string | null {
  try {
    const part = token.split(".")[1];
    if (!part) return null;
    const json = JSON.parse(atob(part.replace(/-/g, "+").replace(/_/g, "/")));
    return typeof json.sub === "string" ? json.sub : null;
  } catch { return null; }
}

const sameTarget = (a: AttachmentTarget, b: AttachmentTarget) =>
  a.source === b.source && a.target_id === b.target_id && (a.exercise_ref ?? "") === (b.exercise_ref ?? "");

export async function queuedVideos(userId: string, target?: AttachmentTarget): Promise<QueuedVideo[]> {
  if (!storage) return [];
  try {
    const rows = await storage.all();
    return rows.filter(row => row.user_id === userId && (!target || sameTarget(row.target, target)))
      .sort((a, b) => a.created_at - b.created_at);
  } catch { return []; }
}

export async function queueVideo(userId: string, file: File, target: AttachmentTarget): Promise<QueuedVideo> {
  if (!storage) throw new Error("This device can't hold videos offline. Reconnect to attach it.");
  const entry: QueuedVideo = {
    id: typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
    user_id: userId,
    target: { source: target.source, target_id: target.target_id, exercise_ref: target.exercise_ref ?? "" },
    file, type: file.type, created_at: Date.now(),
  };
  await storage.put(entry);
  changed();
  return entry;
}

export async function removeQueuedVideo(id: string) {
  if (!storage) return;
  await storage.remove(id).catch(() => undefined);
  changed();
}

/** Sign out wipes every held video on this device. */
export async function clearVideoQueue() {
  if (!storage) return;
  await storage.clear().catch(() => undefined);
  changed();
}

const POLL_MS = 2_000;
const POLL_LIMIT = 60;
const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

/** Reserve, upload straight to private storage, wait for the isolated worker, then attach. */
export async function uploadAndAttach(token: string, file: Blob, type: string, target: AttachmentTarget,
  onStage: (stage: string) => void = () => undefined): Promise<TrainingAttachment> {
  onStage("Uploading…");
  const permission = await reservePrivateUpload(token, { size: file.size, type });
  const { error } = await getSupabaseBrowserClient().storage.from(permission.bucket)
    .upload(permission.path, file, { contentType: type, upsert: false });
  if (error) {
    // A dropped connection queues the video; anything else is a real refusal.
    if (isBrowserOffline() || isNetworkFailure(error)) throw new Error(RETRYABLE_NETWORK_MESSAGE);
    throw new Error("Upload failed. Try again.");
  }
  onStage("Processing…");
  for (let attempt = 0; attempt < POLL_LIMIT; attempt += 1) {
    const state = await getPrivateUpload(token, permission.asset_id);
    if (state.status === "verified" && state.media) return attachTrainingVideo(token, state.media.id, target);
    if (state.status === "rejected" || state.status === "deleted") {
      throw new Error("That video could not be used. Use a clip of 30 seconds or less.");
    }
    await wait(POLL_MS);
  }
  throw new Error("Processing is taking longer than usual. Try again in a minute.");
}

/** Ids being sent right now, so their tiles say "Uploading…" instead of "Queued". */
export const sendingVideos = new Set<string>();
let flushing: Promise<void> | null = null;

/** Send the signed-in athlete's held videos, oldest first. One run at a time. */
export function flushVideoQueue(token: string, upload: typeof uploadAndAttach = uploadAndAttach): Promise<void> {
  if (flushing) return flushing;
  const userId = tokenSubject(token);
  if (!userId || !storage || isBrowserOffline()) return Promise.resolve();
  flushing = (async () => {
    for (const entry of await queuedVideos(userId)) {
      if (entry.failed) continue;
      sendingVideos.add(entry.id);
      changed();
      try {
        await upload(token, entry.file, entry.type, entry.target);
        await storage?.remove(entry.id);
      } catch (error) {
        if (isBrowserOffline() || isNetworkFailure(error)) break; // Still offline: try again later.
        await storage?.put({ ...entry, failed: error instanceof Error && error.message ? error.message : "Video could not be attached." });
      } finally {
        sendingVideos.delete(entry.id);
        changed();
      }
    }
  })().finally(() => { flushing = null; });
  return flushing;
}
