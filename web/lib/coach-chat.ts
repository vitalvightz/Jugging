// Coach chat: one conversation per coach-fighter pair.
//
// Speed: the inbox is one RPC, a chat page is one indexed table read, and a
// send is one RPC that writes the message and the inbox row together. New
// messages, reactions and read receipts arrive over Realtime, so nothing polls.
// Security: the browser only ever reads rows it participates in (RLS) and
// writes through database functions that recheck the live connection.

export const CHAT_PAGE_SIZE = 30;
export const CHAT_MAX_CHARS = 2000;
// Must match coach_private.chat_react in the coach chat migration.
export const CHAT_REACTIONS = ["👍", "🔥", "💪", "👊", "😂", "❤️"] as const;
export type ChatReaction = (typeof CHAT_REACTIONS)[number];

export type ChatRole = "coach" | "athlete";
export type QuoteSource = "unlxck" | "external" | "sparring";
export type ChatQuote = { source: QuoteSource; target_id: string; day: string; title: string; meta: string };
export type QuoteRef = { source: QuoteSource; target_id: string };

export type ChatMessage = {
  id: number;
  chat_id: string;
  coach_id: string;
  athlete_id: string;
  sender_id: string;
  client_id: string;
  kind: "text" | "photo" | "voice";
  body: string;
  asset_id: string | null;
  quote: ChatQuote | null;
  reply_to_id: number | null;
  reactions: Record<string, string>;
  created_at: string;
  // Local only: a voice note still uploading, a message the server hasn't
  // confirmed yet, or one that failed (kept for a retry).
  status?: "uploading" | "sending" | "failed";
};

export type InboxRow = {
  chat_id: string | null;
  other_id: string;
  name: string;
  avatar_url: string | null;
  role: ChatRole;
  active: boolean;
  last_message_at: string | null;
  last_preview: string;
  last_from_me: boolean;
  unread: number;
  other_read_at: string | null;
  fight_date: string | null;
  attention: boolean;
  missed_checkin: boolean;
};

export type ChatDraft = {
  kind: ChatMessage["kind"];
  body: string;
  asset_id?: string | null;
  quote?: QuoteRef | null;
  reply_to_id?: number | null;
};

export type CheckinNudge = { days: number; local_time: string; enabled: boolean; last_sent_day: string | null };

const MESSAGE_COLUMNS = "id,chat_id,coach_id,athlete_id,sender_id,client_id,kind,body,asset_id,quote,reply_to_id,reactions,created_at";
// Our own database messages are safe to show; anything else stays generic.
const SHOWN_CODES = new Set(["22023", "PT429", "42501"]);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function headers(token: string): Record<string, string> {
  return {
    "Content-Type": "application/json",
    apikey: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "",
    Authorization: `Bearer ${token}`,
  };
}

/** The message a database function raised, when it is one of ours. */
export function chatErrorMessage(status: number, body: unknown): string {
  const error = body && typeof body === "object" ? (body as { code?: unknown; message?: unknown }) : {};
  const message = typeof error.message === "string" ? error.message : "";
  if (status === 401) return "Your session expired. Sign in again.";
  if (typeof error.code === "string" && SHOWN_CODES.has(error.code) && message && !/^permission denied/i.test(message)) {
    return message;
  }
  return status === 403 ? "This chat is unavailable." : "Something went wrong. Please retry.";
}

async function rpc<T>(token: string, name: string, body: object, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${process.env.NEXT_PUBLIC_SUPABASE_URL}/rest/v1/rpc/${name}`, {
    method: "POST", cache: "no-store", headers: headers(token), body: JSON.stringify(body), signal,
  });
  if (!response.ok) {
    throw new Error(chatErrorMessage(response.status, await response.json().catch(() => null)));
  }
  const text = await response.text();
  return (text ? JSON.parse(text) : null) as T;
}

async function select<T>(token: string, table: string, params: URLSearchParams, signal?: AbortSignal): Promise<T[]> {
  const response = await fetch(`${process.env.NEXT_PUBLIC_SUPABASE_URL}/rest/v1/${table}?${params}`, {
    cache: "no-store", headers: headers(token), signal,
  });
  if (!response.ok) throw new Error(chatErrorMessage(response.status, await response.json().catch(() => null)));
  const rows = await response.json();
  if (!Array.isArray(rows)) throw new Error("Something went wrong. Please retry.");
  return rows as T[];
}

export function fetchInbox(token: string, signal?: AbortSignal) {
  return rpc<InboxRow[]>(token, "coach_chat_inbox", {}, signal);
}

/** The newest page of a chat, or the page before ``beforeId``, oldest first. */
export async function fetchMessages(token: string, chatId: string, beforeId?: number, signal?: AbortSignal) {
  const params = new URLSearchParams({
    select: MESSAGE_COLUMNS, chat_id: `eq.${chatId}`, order: "id.desc", limit: String(CHAT_PAGE_SIZE),
  });
  if (beforeId) params.set("id", `lt.${beforeId}`);
  const rows = await select<ChatMessage>(token, "coach_messages", params, signal);
  return rows.reverse();
}

export function sendMessage(token: string, otherId: string, clientId: string, draft: ChatDraft) {
  return rpc<ChatMessage & { created: boolean }>(token, "coach_chat_send", {
    p_other_id: otherId,
    p_client_id: clientId,
    p_kind: draft.kind,
    p_body: draft.body,
    p_asset_id: draft.asset_id ?? null,
    p_quote_source: draft.quote?.source ?? null,
    p_quote_target: draft.quote?.target_id ?? null,
    p_reply_to: draft.reply_to_id ?? null,
  });
}

export function markChatRead(token: string, chatId: string) {
  return rpc<null>(token, "coach_chat_read", { p_chat_id: chatId });
}

export function reactToMessage(token: string, messageId: number, emoji: ChatReaction | null) {
  return rpc<Record<string, string>>(token, "coach_chat_react", { p_message_id: messageId, p_emoji: emoji });
}

export function reportMessage(token: string, messageId: number, reason: string) {
  return rpc<null>(token, "coach_chat_report", { p_message_id: messageId, p_reason: reason });
}

export async function fetchCheckinNudge(token: string, athleteId: string, signal?: AbortSignal) {
  const params = new URLSearchParams({ select: "days,local_time,enabled,last_sent_day", athlete_id: `eq.${athleteId}`, limit: "1" });
  const [row] = await select<CheckinNudge>(token, "coach_checkin_nudges", params, signal);
  return row ?? null;
}

export function saveCheckinNudge(token: string, athleteId: string, days: number, time: string, enabled: boolean) {
  return rpc<CheckinNudge>(token, "coach_set_checkin_nudge", {
    p_athlete_id: athleteId, p_days: days, p_time: time, p_enabled: enabled,
  });
}

const coachCheck = new Map<string, Promise<boolean>>();

/** Whether the signed-in fighter has a connected coach. Asked once per session. */
export function hasConnectedCoach(token: string): Promise<boolean> {
  let check = coachCheck.get(token);
  if (!check) {
    const params = new URLSearchParams({ select: "id", status: "eq.accepted", limit: "1" });
    check = select<{ id: string }>(token, "coach_connections", params).then(rows => rows.length > 0, () => false);
    coachCheck.clear();
    coachCheck.set(token, check);
  }
  return check;
}

export function newClientId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return "10000000-1000-4000-8000-100000000000".replace(/[018]/g, c =>
    (Number(c) ^ (Math.random() * 16) >> (Number(c) / 4)).toString(16));
}

/**
 * Merge a page, a send result or a Realtime row into what is on screen. The
 * client id is the identity, so a confirmed send replaces its pending bubble
 * and a message seen twice (send result and Realtime) shows once.
 */
export function mergeMessages(current: readonly ChatMessage[], incoming: readonly ChatMessage[]): ChatMessage[] {
  const byClient = new Map(current.map(message => [message.client_id, message]));
  for (const message of incoming) {
    const existing = byClient.get(message.client_id);
    // A late Realtime echo never replaces a newer local reaction state.
    byClient.set(message.client_id, existing && !existing.status && !message.status
      ? { ...existing, ...message } : message);
  }
  return [...byClient.values()].sort((a, b) => {
    const pendingA = a.status ? 1 : 0;
    const pendingB = b.status ? 1 : 0;
    if (pendingA !== pendingB) return pendingA - pendingB;
    return pendingA ? a.created_at.localeCompare(b.created_at) : a.id - b.id;
  });
}

function localDay(iso: string) {
  const date = new Date(iso);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

/** "Today", "Yesterday", or a short date, for day separators. */
export function chatDayLabel(iso: string, now = new Date()): string {
  const day = localDay(iso);
  if (day === localDay(now.toISOString())) return "Today";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (day === localDay(yesterday.toISOString())) return "Yesterday";
  const date = new Date(iso);
  return date.toLocaleDateString(undefined, {
    weekday: "short", day: "numeric", month: "short",
    ...(date.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
}

export function chatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/** The inbox column: a time today, otherwise a day. */
export function inboxTime(iso: string | null, now = new Date()): string {
  if (!iso) return "";
  const label = chatDayLabel(iso, now);
  return label === "Today" ? chatTime(iso) : label;
}

export function sameDay(a: string, b: string): boolean {
  return localDay(a) === localDay(b);
}

export function reactionSummary(reactions: Record<string, string> | null | undefined, me: string) {
  const counts = new Map<string, { emoji: string; count: number; mine: boolean }>();
  for (const [who, emoji] of Object.entries(reactions ?? {})) {
    const entry = counts.get(emoji) ?? { emoji, count: 0, mine: false };
    entry.count += 1;
    entry.mine ||= who === me;
    counts.set(emoji, entry);
  }
  return [...counts.values()].sort((a, b) => CHAT_REACTIONS.indexOf(a.emoji as ChatReaction) - CHAT_REACTIONS.indexOf(b.emoji as ChatReaction));
}

/** WhatsApp-style receipt under the caller's last message. */
export function receipt(message: ChatMessage, otherReadAt: string | null): string {
  if (message.status === "sending") return "Sending…";
  if (message.status === "failed") return "Not sent";
  return otherReadAt && otherReadAt >= message.created_at ? "Read" : "Delivered";
}

/** "unlxck:<uuid>" from a link into a quote, or null when it is not one. */
export function parseQuoteParam(value: string | null | undefined): QuoteRef | null {
  const [source, target] = (value ?? "").split(":");
  if (!["unlxck", "external", "sparring"].includes(source) || !UUID.test(target ?? "")) return null;
  return { source: source as QuoteSource, target_id: target };
}

/** Where a "Message about this" link opens the chat. */
export function chatLink(role: ChatRole, otherId: string, quote?: QuoteRef): string {
  const params = new URLSearchParams({ with: otherId });
  if (quote) params.set("quote", `${quote.source}:${quote.target_id}`);
  return `${role === "coach" ? "/coach/chats" : "/chat"}?${params}`;
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "") + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase() || "?";
}

// Check-in nudge days: bit 0 is Monday ... bit 6 is Sunday.
export const NUDGE_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;
export const EVERY_DAY = 0b1111111;
export const WEEKDAYS = 0b0011111;

export function hasDay(mask: number, index: number): boolean {
  return (mask & (1 << index)) !== 0;
}

export function toggleDay(mask: number, index: number): number {
  return mask ^ (1 << index);
}

export function describeDays(mask: number): string {
  if (mask === EVERY_DAY) return "Every day";
  if (mask === WEEKDAYS) return "Weekdays";
  if (mask === 0b1100000) return "Weekends";
  return NUDGE_DAYS.filter((_, index) => hasDay(mask, index)).join(", ") || "No days";
}
