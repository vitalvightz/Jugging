// What makes coach chat open instantly, like other messengers: the inbox and
// the newest page of recent chats are kept on this device and shown at once,
// then refreshed from the server. Photo sizes and voice-note lengths are kept
// too, so a bubble has its final shape before its file arrives.
//
// Signed file links expire in two minutes, so they stay in memory only.
// Everything is per account and cleared at sign-out.
import type { ChatFile } from "@/lib/api-schema.generated";
import { CHAT_PAGE_SIZE, type ChatMessage, type InboxRow } from "@/lib/coach-chat";

const PREFIX = "unlxck.chat.v1.";
const MAX_THREADS = 8;
const MAX_MEDIA = 400;
const SAVE_DELAY_MS = 300;

export type MediaShape = { kind: "photo" | "voice"; width: number | null; height: number | null; duration_ms: number | null };

type Store = {
  inbox: InboxRow[] | null;
  // Newest page of each chat, most recently used first.
  threads: Record<string, ChatMessage[]>;
  order: string[];
  media: Record<number, MediaShape>;
};

let owner: string | null = null;
let store: Store | null = null;
let saveTimer: ReturnType<typeof setTimeout> | null = null;
// Bumped at sign-out, so work started before it can tell it is stale.
let generation = 0;
const links = new Map<number, ChatFile & { at: number }>();
const prefetching = new Map<string, Promise<void>>();
// When each chat was last warmed: inbox refreshes on every focus, and a chat
// fetched moments ago needn't be fetched again.
const warmedAt = new Map<string, number>();
const WARM_FOR_MS = 60_000;

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function empty(): Store {
  return { inbox: null, threads: {}, order: [], media: {} };
}

function open(userId: string): Store {
  if (owner === userId && store) return store;
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  owner = userId;
  links.clear();
  prefetching.clear();
  warmedAt.clear();
  store = empty();
  try {
    const raw = storage()?.getItem(PREFIX + userId);
    const parsed = raw ? JSON.parse(raw) as Partial<Store> : null;
    if (parsed && typeof parsed === "object") {
      store = {
        inbox: Array.isArray(parsed.inbox) ? parsed.inbox : null,
        threads: parsed.threads && typeof parsed.threads === "object" ? parsed.threads : {},
        order: Array.isArray(parsed.order) ? parsed.order : [],
        media: parsed.media && typeof parsed.media === "object" ? parsed.media : {},
      };
    }
  } catch {
    store = empty();
  }
  return store;
}

function save() {
  if (saveTimer || !owner) return;
  const key = PREFIX + owner;
  saveTimer = setTimeout(() => {
    saveTimer = null;
    if (!store) return;
    try {
      storage()?.setItem(key, JSON.stringify(store));
    } catch {
      // Full or blocked storage: the chat still works, just without the head start.
    }
  }, SAVE_DELAY_MS);
}

export function cachedInbox(userId: string): InboxRow[] | null {
  return open(userId).inbox;
}

export function cacheInbox(userId: string, rows: InboxRow[]) {
  open(userId).inbox = rows;
  save();
}

export function cachedThread(userId: string, chatId: string | null): ChatMessage[] | null {
  if (!chatId) return null;
  return open(userId).threads[chatId] ?? null;
}

/** Keep the newest confirmed page of a chat. */
export function cacheThread(userId: string, chatId: string, messages: readonly ChatMessage[]) {
  const data = open(userId);
  const confirmed = messages.filter(message => !message.status && message.id > 0).slice(-CHAT_PAGE_SIZE);
  if (!confirmed.length) return;
  data.threads[chatId] = confirmed;
  data.order = [chatId, ...data.order.filter(id => id !== chatId)];
  for (const stale of data.order.splice(MAX_THREADS)) delete data.threads[stale];
  save();
}

export function mediaShape(userId: string, messageId: number): MediaShape | null {
  return open(userId).media[messageId] ?? null;
}

/** File links still fresh enough to use, and the shapes they carry, remembered. */
export function rememberLinks(userId: string, files: readonly ChatFile[], at = Date.now()) {
  const data = open(userId);
  for (const file of files) {
    links.set(file.message_id, { ...file, at });
    data.media[file.message_id] = { kind: file.kind, width: file.width, height: file.height, duration_ms: file.duration_ms };
  }
  const ids = Object.keys(data.media);
  if (ids.length > MAX_MEDIA) {
    for (const id of ids.map(Number).sort((a, b) => a - b).slice(0, ids.length - MAX_MEDIA)) delete data.media[id];
  }
  save();
}

export function freshLinks(userId: string, maxAgeMs: number): Record<number, ChatFile & { at: number }> {
  open(userId);
  const now = Date.now();
  const fresh: Record<number, ChatFile & { at: number }> = {};
  for (const [id, link] of links) if (now - link.at < maxAgeMs) fresh[id] = link;
  return fresh;
}

/** Run a chat's warm-up once at a time; a second ask joins the first. */
export function prefetchOnce(userId: string, chatId: string, task: () => Promise<void>): Promise<void> {
  open(userId);
  let running = prefetching.get(chatId);
  if (!running && Date.now() - (warmedAt.get(chatId) ?? 0) < WARM_FOR_MS) return Promise.resolve();
  if (!running) {
    warmedAt.set(chatId, Date.now());
    running = task().catch(() => undefined).finally(() => prefetching.delete(chatId));
    prefetching.set(chatId, running);
  }
  return running;
}

export function cacheGeneration() {
  return generation;
}

/** Forget every account's chats on this device, e.g. at sign-out. */
export function clearChatCache() {
  generation += 1;
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  owner = null;
  store = null;
  links.clear();
  prefetching.clear();
  warmedAt.clear();
  const local = storage();
  if (!local) return;
  try {
    Object.keys(local).filter(key => key.startsWith(PREFIX)).forEach(key => local.removeItem(key));
  } catch {
    // Nothing more to do.
  }
}

/** Displayed photo size: at most 240 wide and 320 tall, keeping the photo's shape. */
export function photoBox(width: number | null | undefined, height: number | null | undefined) {
  if (!width || !height) return { width: 240, height: 240 };
  const shown = Math.round(Math.min(240, (320 * width) / height));
  return { width: shown, height: Math.round((shown * height) / width) };
}
