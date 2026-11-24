"use client";
// Coach chat: the WhatsApp layout people already know, plus quoted training
// cards. One screen for coaches (/coach/chats) and fighters (/chat).
import Link from "next/link";
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { RequireAuth } from "@/components/auth-guard";
import { useAppSession } from "@/components/auth-provider";
import { Skeleton } from "@/components/skeleton";
import { VoicePlaceholder, VoicePlayer } from "@/components/voice-note";
import { getChatFiles, notifyChatMessage, processFileUpload, reserveFileUpload } from "@/lib/api";
import type { ChatFile } from "@/lib/api-schema.generated";
import { formatDuration, photoProblem, preparePhoto, VoiceRecorder } from "@/lib/attachments";
import { isSafeAvatarImageUrl } from "@/lib/avatar-image-url";
import {
  CHAT_MAX_CHARS, CHAT_PAGE_SIZE, CHAT_REACTIONS, chatDayLabel, chatTime, fetchInbox, fetchMessages, inboxTime,
  initials, markChatRead, mergeMessages, newClientId, parseQuoteParam, reactToMessage, reactionSummary, receipt,
  reportMessage, sameDay, sendMessage, type ChatDraft, type ChatMessage, type ChatReaction, type ChatRole,
  type InboxRow, type QuoteRef,
} from "@/lib/coach-chat";
import { subscribeToChat, subscribeToInbox, type ChatRow } from "@/lib/coach-chat-live";
import {
  cacheGeneration, cachedInbox, cachedThread, cacheInbox, cacheThread, freshLinks, mediaShape, photoBox, prefetchOnce, rememberLinks,
} from "@/lib/chat-cache";
import { formatAppDate } from "@/lib/date-format";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import { getSupabaseBrowserClient } from "@/lib/supabase";
import { SLIDE_TO_CANCEL_PX, useHoldToRecord } from "@/lib/use-hold-to-record";
import { useSheetExit } from "@/lib/use-sheet-exit";
import styles from "./coach-chat.module.css";

// A signed file link lives 120 seconds; refresh a little before that.
const FILE_LINK_TTL_MS = 100_000;
const TYPING_SHOWN_MS = 4000;

export function CoachChat({ role }: { role: ChatRole }) {
  const { session, me, isMeHydrated } = useAppSession();
  const userId = me?.profile.athlete_id;
  return (
    <RequireAuth>
      {!isMeHydrated || !session || !userId ? (
        <div className={styles.shell} aria-busy="true"><Skeleton height={420} /></div>
      ) : (
        <ChatScreen key={`${userId}:${session.access_token}`} token={session.access_token} me={userId} role={role} />
      )}
    </RequireAuth>
  );
}

function readParams() {
  if (typeof window === "undefined") return { withId: null, quote: null };
  const params = new URLSearchParams(window.location.search);
  return { withId: params.get("with"), quote: parseQuoteParam(params.get("quote")) };
}

/** The chat a link points at, or a fighter's only coach. A linked session waits
 * for the first chat picked when there is a choice. */
function firstChat(rows: InboxRow[], role: ChatRole): { selected: string | null; quote: QuoteRef | null } {
  const { withId, quote } = readParams();
  const open = rows.filter(row => row.active);
  if (withId && rows.some(row => row.other_id === withId)) return { selected: withId, quote };
  if (!withId && role === "athlete" && open.length === 1) return { selected: open[0].other_id, quote };
  return { selected: null, quote };
}

function applyChatRow(row: InboxRow, chat: ChatRow, me: string): InboxRow {
  const coachSide = chat.coach_id === me;
  return {
    ...row,
    chat_id: chat.id,
    last_message_at: chat.last_message_at,
    last_preview: chat.last_preview,
    last_from_me: chat.last_sender_id === me,
    unread: coachSide ? chat.coach_unread : chat.athlete_unread,
    other_read_at: coachSide ? chat.athlete_read_at : chat.coach_read_at,
  };
}

function sortInbox(rows: InboxRow[]) {
  return [...rows].sort((a, b) => (b.last_message_at ?? "").localeCompare(a.last_message_at ?? "") || a.name.localeCompare(b.name));
}

function ChatScreen({ token, me, role }: { token: string; me: string; role: ChatRole }) {
  // The inbox from last time shows at once; the server's copy replaces it.
  // A remembered inbox also opens the linked (or only) chat straight away.
  const [start] = useState(() => {
    const rows = cachedInbox(me);
    return { rows, first: rows ? firstChat(rows, role) : null };
  });
  const [inbox, setInbox] = useState<InboxRow[] | null>(start.rows);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(start.first?.selected ?? null);
  const [quote, setQuote] = useState<QuoteRef | null>(start.first?.quote ?? null);
  // Only a chat actually opened counts: a link to a fighter not yet in the
  // remembered inbox still opens once the server's inbox arrives.
  const opened = useRef(Boolean(start.first?.selected));
  const selectedRef = useRef<string | null>(start.first?.selected ?? null);

  const openFirst = useCallback((rows: InboxRow[]) => {
    if (opened.current) return;
    opened.current = true;
    const first = firstChat(rows, role);
    setQuote(first.quote);
    if (first.selected) {
      selectedRef.current = first.selected;
      setSelected(first.selected);
    }
  }, [role]);

  const load = useCallback(async () => {
    try {
      const rows = await fetchInbox(token);
      setInbox(sortInbox(rows));
      setError("");
      openFirst(rows);
      // Warm the chats people open most, so they open without a wait.
      for (const row of sortInbox(rows).filter(row => row.chat_id).slice(0, 3)) void prefetchThread(token, me, row.chat_id!);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong. Please retry.");
    }
  }, [token, me, openFirst]);

  useEffect(() => { if (inbox) cacheInbox(me, inbox); }, [me, inbox]);

  // Without Realtime the inbox refreshes when the screen comes back into view.
  const inboxLive = useRef<boolean | null>(null);
  useEffect(() => {
    const first = window.setTimeout(() => void load(), 0);
    const onFocus = () => void load();
    const onVisible = () => { if (document.visibilityState === "visible" && !inboxLive.current) void load(); };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearTimeout(first);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);

  useEffect(() => subscribeToInbox(token, me, role, chat => {
    setInbox(rows => {
      if (!rows) return rows;
      const index = rows.findIndex(row => row.chat_id === chat.id
        || (!row.chat_id && row.other_id === (chat.coach_id === me ? chat.athlete_id : chat.coach_id)));
      if (index < 0) { void load(); return rows; }
      const next = [...rows];
      next[index] = applyChatRow(rows[index], chat, me);
      return sortInbox(next);
    });
  }, live => {
    // Back after an outage: catch up on anything missed while down.
    if (live && inboxLive.current === false) void load();
    inboxLive.current = live;
  }), [token, me, role, load]);

  const select = useCallback((otherId: string | null) => {
    // The linked session goes to the first chat opened, never to a later one.
    if (selectedRef.current || !otherId) setQuote(null);
    selectedRef.current = otherId;
    setSelected(otherId);
    const url = new URL(window.location.href);
    url.search = otherId ? `?with=${encodeURIComponent(otherId)}` : "";
    window.history.replaceState(window.history.state, "", url);
  }, []);

  const updateRow = useCallback((otherId: string, patch: Partial<InboxRow>) => {
    setInbox(rows => rows ? sortInbox(rows.map(row => row.other_id === otherId ? { ...row, ...patch } : row)) : rows);
  }, []);

  const current = inbox?.find(row => row.other_id === selected) ?? null;
  return (
    <div className={styles.shell}>
      <section className={styles.list} aria-label="Chats">
        <header className={styles.listHeader}>
          <h1>{role === "coach" ? "Chats" : "Coach chat"}</h1>
          {inbox ? <span className={styles.count}>{inbox.reduce((total, row) => total + row.unread, 0) || ""}</span> : null}
        </header>
        {error && !inbox ? (
          <div className={styles.problem} role="alert">{error}<button type="button" onClick={() => void load()}>Retry</button></div>
        ) : !inbox ? (
          <div className={styles.listLoading}><Skeleton height={56} /><Skeleton height={56} /><Skeleton height={56} /></div>
        ) : inbox.length === 0 ? (
          <EmptyInbox role={role} />
        ) : (
          <ul className={styles.rows}>
            {inbox.map(row => (
              <li key={row.other_id}>
                <button type="button" className={styles.row} aria-current={row.other_id === selected ? "true" : undefined}
                  onPointerDown={() => { if (row.chat_id) void prefetchThread(token, me, row.chat_id); }}
                  onClick={() => select(row.other_id)}>
                  <Avatar name={row.name} url={row.avatar_url} flag={row.attention ? "attention" : null} />
                  <span className={styles.rowCopy}>
                    <span className={styles.rowTop}>
                      <strong>{row.name}</strong>
                      <time className={row.unread ? styles.unreadTime : undefined}>{inboxTime(row.last_message_at)}</time>
                    </span>
                    <span className={styles.rowBottom}>
                      <span className={styles.preview}>
                        {row.missed_checkin ? <em className={styles.missed}>Missed check-in · </em> : null}
                        {!row.active ? "Chat ended" : row.last_message_at
                          ? `${row.last_from_me ? "You: " : ""}${row.last_preview}` : "Start the conversation"}
                      </span>
                      {row.unread ? <span className={styles.badge} aria-label={`${row.unread} unread`}>{row.unread > 99 ? "99+" : row.unread}</span> : null}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      {current ? (
        <ChatSheet name={current.name} onClose={() => select(null)}>
          {close => <ChatThread key={current.other_id} token={token} me={me} role={role} row={current} initialQuote={quote}
            onBack={close} onRow={patch => updateRow(current.other_id, patch)} />}
        </ChatSheet>
      ) : null}
    </div>
  );
}

/**
 * The open chat, in its own sheet over the inbox (like session details), so the
 * page, the tab bar and the list behind it never interfere with typing,
 * scrolling or recording.
 */
function ChatSheet({ name, onClose, children }: { name: string; onClose: () => void; children: (close: () => void) => ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const { closing, close } = useSheetExit(onClose);
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    const previous = document.activeElement as HTMLElement | null;
    // Hidden until iOS has settled the sheet's size and the thread is pinned to
    // its newest message, so the first frame shown is already the final one.
    element.dataset.settling = "true";
    if (!element.open) element.showModal();
    // showModal focuses the first button (Back), and iOS draws a focus ring on it.
    // Focus the sheet itself instead; Tab still reaches Back first.
    element.focus({ preventScroll: true });
    const release = lockOverlayScroll();
    // iOS keeps 100dvh when the keyboard opens; size the sheet to what is visible
    // so the composer sits right above the keyboard instead of behind it.
    const viewport = window.visualViewport;
    // The full screen height (100dvh), to tell a keyboard from iOS simply
    // reporting a visible area a little shorter than the screen.
    const probe = document.createElement("div");
    probe.style.cssText = "position:fixed;top:0;left:0;width:0;height:100dvh;visibility:hidden;pointer-events:none";
    document.body.appendChild(probe);
    const fit = () => {
      // Pinch-zoomed, the visible area is not the keyboard's doing: leave the layout alone.
      if (!viewport || viewport.scale > 1.01) return;
      // No keyboard: fill the screen (CSS 100dvh), so the composer sits just above
      // the home indicator rather than floating over an empty strip.
      if (probe.offsetHeight - viewport.height < 120) {
        element.style.removeProperty("--chat-viewport");
        element.style.top = "";
        return;
      }
      element.style.setProperty("--chat-viewport", `${Math.round(viewport.height)}px`);
      element.style.top = `${Math.round(viewport.offsetTop)}px`;
    };
    fit();
    let frame = window.requestAnimationFrame(() => {
      frame = window.requestAnimationFrame(() => { fit(); delete element.dataset.settling; });
    });
    viewport?.addEventListener("resize", fit);
    viewport?.addEventListener("scroll", fit);
    return () => {
      window.cancelAnimationFrame(frame);
      probe.remove();
      viewport?.removeEventListener("resize", fit);
      viewport?.removeEventListener("scroll", fit);
      element.close(); release(); previous?.focus();
    };
  }, []);
  return createPortal(
    <dialog ref={dialog} className={styles.sheet} aria-label={`Chat with ${name}`} tabIndex={-1} data-closing={closing || undefined}
      onCancel={event => { if (event.target !== event.currentTarget) return; event.preventDefault(); close(); }}
      onClick={event => {
        if (event.target !== event.currentTarget) return;
        const box = event.currentTarget.getBoundingClientRect();
        if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) close();
      }}>
      {children(close)}
    </dialog>,
    document.body,
  );
}

function EmptyInbox({ role }: { role: ChatRole }) {
  return (
    <div className={styles.empty}>
      <p>{role === "coach" ? "No fighters yet. Once a fighter accepts your invite, you can message them here." : "Connect a coach to message them here."}</p>
      <Link className="ghost-button" href={role === "coach" ? "/coach/invitations" : "/settings/coach-access"}>
        {role === "coach" ? "Invite a fighter" : "Coach access"}
      </Link>
    </div>
  );
}

function Avatar({ name, url, flag, small = false }: { name: string; url: string | null; flag?: "attention" | null; small?: boolean }) {
  return (
    <span className={`${styles.avatar} ${small ? styles.avatarSmall : ""}`} aria-hidden="true">
      {url && isSafeAvatarImageUrl(url) ? (
        // eslint-disable-next-line @next/next/no-img-element -- small remote avatar, already size-checked
        <img src={url} alt="" />
      ) : initials(name)}
      {flag ? <span className={styles.flag} /> : null}
    </span>
  );
}

/**
 * Fetch a chat's newest page and its file links ahead of opening it, and start
 * the photos downloading, so the chat shows complete the moment it opens.
 */
function prefetchThread(token: string, me: string, chatId: string) {
  const generation = cacheGeneration();
  // Signed out meanwhile: never write the previous account's chats back.
  const current = () => cacheGeneration() === generation;
  return prefetchOnce(me, chatId, async () => {
    const rows = await fetchMessages(token, chatId);
    if (!current()) return;
    cacheThread(me, chatId, mergeMessages(cachedThread(me, chatId) ?? [], rows));
    const ids = rows.filter(m => m.asset_id && m.id > 0).map(m => m.id).slice(-30);
    if (!ids.length) return;
    const files = await getChatFiles(token, ids);
    if (!current()) return;
    rememberLinks(me, files);
    for (const file of files) {
      if (file.kind !== "photo") continue;
      const image = new Image();
      image.src = file.thumb_url ?? file.url;
      // Decoded ahead too, so it paints in the chat's very first frame.
      void image.decode?.().catch(() => undefined);
    }
  });
}

// A voice note carries its recording until the upload succeeds, so a retry
// never records or uploads twice, and the note plays from the device meanwhile.
type Pending = { draft: ChatDraft; clientId: string; voice?: { wav: Blob; durationMs: number } };

async function uploadChatFile(token: string, kind: "photo" | "voice", blob: Blob, onStage: (stage: string) => void) {
  onStage(kind === "photo" ? "Uploading photo…" : "Uploading voice note…");
  const permission = await reserveFileUpload(token, kind, blob.size);
  const { error } = await getSupabaseBrowserClient().storage.from(permission.bucket)
    .upload(permission.path, blob, { contentType: permission.content_type, upsert: false });
  if (error) throw new Error("Upload failed. Check your connection and try again.");
  onStage(kind === "photo" ? "Processing photo…" : "Processing voice note…");
  const result = await processFileUpload(token, permission.asset_id);
  if (result.status !== "ready") throw new Error(result.reason || "That file could not be used.");
  return permission.asset_id;
}

function ChatThread({ token, me, role, row, initialQuote, onBack, onRow }: {
  token: string; me: string; role: ChatRole; row: InboxRow; initialQuote: QuoteRef | null;
  onBack: () => void; onRow: (patch: Partial<InboxRow>) => void;
}) {
  const [chatId, setChatId] = useState(row.chat_id);
  // The newest page from last time shows at once; the server's copy merges in.
  const [messages, setMessages] = useState<ChatMessage[]>(() => cachedThread(me, row.chat_id) ?? []);
  const [loading, setLoading] = useState(() => Boolean(row.chat_id) && !cachedThread(me, row.chat_id)?.length);
  const [hasMore, setHasMore] = useState(false);
  const [problem, setProblem] = useState("");
  const [otherReadAt, setOtherReadAt] = useState(row.other_read_at);
  const [typingUntil, setTypingUntil] = useState(0);
  const [files, setFiles] = useState<Record<number, ChatFile & { at: number }>>(() => freshLinks(me, FILE_LINK_TTL_MS));
  const [text, setText] = useState("");
  const [quote, setQuote] = useState<QuoteRef | null>(row.active ? initialQuote : null);
  const [replyTo, setReplyTo] = useState<ChatMessage | null>(null);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [reporting, setReporting] = useState<ChatMessage | null>(null);
  const [stage, setStage] = useState("");
  // "hold": recording while the mic is held down; "locked": started from the keyboard, sent with Send.
  const [recording, setRecording] = useState<"hold" | "locked" | null>(null);
  const recorderReady = useRef(false);

  const [today] = useState(() => new Date().toISOString().slice(0, 10));
  const recorder = useRef<VoiceRecorder | null>(null);
  const pending = useRef(new Map<string, Pending>());
  const live = useRef<ReturnType<typeof subscribeToChat> | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);
  const stickToBottom = useRef(true);
  const field = useRef<HTMLTextAreaElement | null>(null);
  // Recordings this device sent, by client id: they play from here at once.
  const [localVoice, setLocalVoice] = useState<Record<string, { wav: Blob; durationMs: number }>>({});
  const delivering = useRef(new Set<string>());
  const photoInput = useRef<HTMLInputElement | null>(null);
  const otherName = row.name.split(" ")[0] || row.name;

  // The newest page, once per chat.
  useEffect(() => {
    if (!chatId) return;
    const controller = new AbortController();
    fetchMessages(token, chatId, undefined, controller.signal).then(rows => {
      setMessages(current => mergeMessages(current, rows));
      setHasMore(rows.length === CHAT_PAGE_SIZE);
      setLoading(false);
    }).catch(e => {
      if (controller.signal.aborted) return;
      // Remembered messages are still worth showing; only an empty chat reports.
      if (cachedThread(me, chatId)?.length) { setLoading(false); return; }
      setLoading(false);
      setProblem(e instanceof Error ? e.message : "Something went wrong. Please retry.");
    });
    return () => controller.abort();
  }, [token, chatId, me]);

  useEffect(() => { if (chatId && !loading) cacheThread(me, chatId, messages); }, [me, chatId, messages, loading]);

  // Live messages, reactions, receipts and typing. Without Realtime (a
  // refused socket, an outage) the newest page is fetched again when the chat
  // comes back into view, and once more when Realtime recovers.
  const chatLive = useRef<boolean | null>(null);
  useEffect(() => {
    if (!chatId) return;
    chatLive.current = null;
    let refreshing: AbortController | null = null;
    const refresh = () => {
      refreshing?.abort();
      const controller = new AbortController();
      refreshing = controller;
      fetchMessages(token, chatId, undefined, controller.signal)
        .then(rows => { if (!controller.signal.aborted) setMessages(current => mergeMessages(current, rows)); })
        .catch(() => undefined);
    };
    const onVisible = () => { if (document.visibilityState === "visible" && !chatLive.current) refresh(); };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    const subscription = subscribeToChat(token, chatId, me, {
      onLive: live => {
        if (live && chatLive.current === false) refresh();
        chatLive.current = live;
      },
      onMessage: message => {
        setMessages(current => mergeMessages(current, [message]));
        if (message.sender_id !== me) setTypingUntil(0);
      },
      onChat: chat => setOtherReadAt(chat.coach_id === me ? chat.athlete_read_at : chat.coach_read_at),
      onTyping: () => setTypingUntil(Date.now() + TYPING_SHOWN_MS),
    });
    live.current = subscription;
    return () => {
      subscription.close();
      live.current = null;
      refreshing?.abort();
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, [token, chatId, me]);

  useEffect(() => {
    if (!typingUntil) return;
    const timer = window.setTimeout(() => setTypingUntil(0), Math.max(0, typingUntil - Date.now()));
    return () => window.clearTimeout(timer);
  }, [typingUntil]);

  // Read receipts: one cheap call per newest message from the other side, sent
  // when it arrives while the chat is in view, or when the reader comes back to
  // a backgrounded tab.
  const lastFromOther = useMemo(() => [...messages].reverse().find(m => m.sender_id !== me && !m.status), [messages, me]);
  const readThrough = useRef(0);
  useEffect(() => {
    if (!chatId || !lastFromOther) return;
    const target = lastFromOther.id;
    let timer = 0;
    const mark = () => {
      if (document.visibilityState !== "visible" || readThrough.current >= target) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        if (readThrough.current >= target) return;
        const previous = readThrough.current;
        readThrough.current = target;
        markChatRead(token, chatId).then(() => onRow({ unread: 0 }))
          .catch(() => { readThrough.current = previous; });
      }, 400);
    };
    mark();
    document.addEventListener("visibilitychange", mark);
    window.addEventListener("focus", mark);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", mark);
      window.removeEventListener("focus", mark);
    };
    // onRow is stable per row; the last message from the other side drives this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, chatId, lastFromOther?.id]);

  // File links for every photo and voice note on screen, in one call. A shown
  // photo keeps its loaded image; a voice note's link is renewed before it
  // expires (never mid-play), and any link that fails is fetched again, twice
  // at most.
  const [linkClock, setLinkClock] = useState(0);
  const filesRef = useRef(files);
  useEffect(() => { filesRef.current = files; }, [files]);
  const playing = useRef(new Set<number>());
  const linkFailures = useRef(new Map<number, number>());
  const dropLink = useCallback((id: number) => {
    const failures = linkFailures.current.get(id) ?? 0;
    if (failures >= 2) return;
    linkFailures.current.set(id, failures + 1);
    setFiles(current => { const next = { ...current }; delete next[id]; return next; });
  }, []);
  useEffect(() => {
    const now = Date.now();
    const needed = messages.filter(m => m.asset_id && m.id > 0 && (!files[m.id]
      || (files[m.id].kind === "voice" && !playing.current.has(m.id) && now - files[m.id].at > FILE_LINK_TTL_MS))).map(m => m.id);
    if (!needed.length) return;
    const controller = new AbortController();
    getChatFiles(token, needed.slice(-30), controller.signal).then(links => {
      const at = Date.now();
      rememberLinks(me, links, at);
      setFiles(current => ({ ...current, ...Object.fromEntries(links.map(link => [link.message_id, { ...link, at }])) }));
    }).catch(() => undefined);
    return () => controller.abort();
  }, [token, me, messages, files, linkClock]);
  useEffect(() => {
    // A played note keeps its audio, so its link is never renewed: leave it out,
    // or an expired link would wake this timer every second.
    const voice = Object.values(files).filter(file => file.kind === "voice" && !playing.current.has(file.message_id));
    if (!voice.length) return;
    const soonest = Math.min(...voice.map(file => file.at + FILE_LINK_TTL_MS));
    const timer = window.setTimeout(() => setLinkClock(tick => tick + 1), Math.max(1000, soonest - Date.now() + 50));
    return () => window.clearTimeout(timer);
  }, [files, linkClock]);

  // Stay pinned to the newest message unless the reader scrolled up, including
  // when the list itself changes size: the keyboard opening, the composer
  // growing, or a voice note or photo finishing loading. Before paint, so an
  // opened chat never shows its oldest messages first.
  useLayoutEffect(() => {
    const box = scroller.current;
    if (box && stickToBottom.current) box.scrollTop = box.scrollHeight;
  }, [messages, typingUntil]);
  useEffect(() => {
    const box = scroller.current;
    if (!box || typeof ResizeObserver === "undefined") return;
    const pin = () => { if (stickToBottom.current) box.scrollTop = box.scrollHeight; };
    const observer = new ResizeObserver(pin);
    observer.observe(box);
    for (const child of Array.from(box.children)) observer.observe(child);
    const added = new MutationObserver(records => {
      for (const record of records) record.addedNodes.forEach(node => { if (node instanceof Element) observer.observe(node); });
    });
    added.observe(box, { childList: true });
    return () => { observer.disconnect(); added.disconnect(); };
  }, []);

  // The field grows with its text up to five lines, then scrolls inside.
  // (Safari has no field-sizing, so the height is set here.)
  useLayoutEffect(() => {
    const element = field.current;
    if (!element) return;
    element.style.height = "auto";
    const borders = element.offsetHeight - element.clientHeight;
    element.style.height = `${Math.min(Math.max(element.scrollHeight + borders, 44), 140)}px`;
  }, [text]);

  async function loadOlder() {
    if (!chatId || !messages.length) return;
    const box = scroller.current;
    const before = box ? box.scrollHeight - box.scrollTop : 0;
    try {
      const older = await fetchMessages(token, chatId, messages.find(m => m.id > 0)?.id);
      stickToBottom.current = false;
      setMessages(current => mergeMessages(current, older));
      setHasMore(older.length === CHAT_PAGE_SIZE);
      window.requestAnimationFrame(() => { if (box) box.scrollTop = box.scrollHeight - before; });
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "Something went wrong. Please retry.");
    }
  }

  async function deliver(item: Pending) {
    const { clientId } = item;
    // One attempt at a time per message: a double tap on Retry never sends twice.
    if (delivering.current.has(clientId)) return;
    delivering.current.add(clientId);
    const uploading = Boolean(item.voice && !item.draft.asset_id);
    setMessages(current => {
      const existing = current.find(m => m.client_id === clientId);
      const draft = item.draft;
      return mergeMessages(current, [{
        id: existing?.id ?? -Date.now(), chat_id: chatId ?? "", coach_id: "", athlete_id: "", sender_id: me, client_id: clientId,
        kind: draft.kind, body: draft.body, asset_id: draft.asset_id ?? null, reply_to_id: draft.reply_to_id ?? null,
        quote: draft.quote ? { ...draft.quote, day: "", title: "Training session", meta: "" } : null,
        reactions: {}, created_at: existing?.created_at ?? new Date().toISOString(), status: uploading ? "uploading" : "sending",
      }]);
    });
    stickToBottom.current = true;
    let draft = item.draft;
    try {
      if (item.voice && !item.draft.asset_id) {
        // Uploaded once: a later retry reuses this file.
        const asset = await uploadChatFile(token, "voice", item.voice.wav, () => undefined);
        draft = { ...draft, asset_id: asset };
        pending.current.set(clientId, { ...item, draft });
        setMessages(current => current.map(m => m.client_id === clientId ? { ...m, status: "sending", asset_id: asset } : m));
      }
      const { created, ...message } = await sendMessage(token, row.other_id, clientId, draft);
      pending.current.delete(clientId);
      if (!chatId) setChatId(message.chat_id);
      setMessages(current => mergeMessages(current, [message]));
      onRow({ chat_id: message.chat_id, last_message_at: message.created_at, last_from_me: true,
        last_preview: message.body || (message.kind === "photo" ? "Photo" : message.kind === "voice" ? "Voice note" : message.quote?.title ?? "") });
      setProblem("");
      // Best-effort: the message is saved whether or not the push goes out.
      if (created) notifyChatMessage(token, message.id).catch(() => undefined);
    } catch (e) {
      setMessages(current => current.map(m => m.client_id === clientId ? { ...m, status: "failed" } : m));
      setProblem(e instanceof Error ? e.message : "Message not sent. Tap it to retry.");
    } finally {
      delivering.current.delete(clientId);
    }
  }

  function send(draft: ChatDraft, voice?: Pending["voice"]) {
    const item: Pending = { draft, clientId: newClientId(), voice };
    if (voice) setLocalVoice(current => ({ ...current, [item.clientId]: voice }));
    pending.current.set(item.clientId, item);
    void deliver(item);
  }

  function retry(message: ChatMessage) {
    const item = pending.current.get(message.client_id);
    if (item) void deliver(item);
  }

  function submitText() {
    const body = text.trim();
    if ((!body && !quote) || body.length > CHAT_MAX_CHARS) return;
    send({ kind: "text", body, quote, reply_to_id: replyTo?.id ?? null });
    setText("");
    setQuote(null);
    setReplyTo(null);
  }

  function onKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && window.matchMedia("(pointer: fine)").matches) {
      event.preventDefault();
      submitText();
    }
  }

  async function sendPhoto(file: File | undefined) {
    if (!file) return;
    const issue = photoProblem(file);
    if (issue) { setProblem(issue); return; }
    try {
      const asset = await uploadChatFile(token, "photo", await preparePhoto(file), setStage);
      send({ kind: "photo", body: "", asset_id: asset, reply_to_id: replyTo?.id ?? null });
      setReplyTo(null);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "That photo could not be sent.");
    } finally {
      setStage("");
    }
  }

  async function startRecording(mode: "hold" | "locked") {
    setProblem("");
    recorder.current?.cancel();
    const next = new VoiceRecorder(() => void finishRecording(true));
    recorder.current = next;
    recorderReady.current = false;
    try {
      await next.start();
    } catch {
      next.cancel();
      if (recorder.current === next) recorder.current = null;
      setProblem("Microphone unavailable. Allow microphone access to record a voice note.");
      return;
    }
    if (recorder.current !== next) { next.cancel(); return; }
    recorderReady.current = true;
    setRecording(mode);
  }

  async function finishRecording(keep: boolean) {
    const active = recorder.current;
    recorder.current = null;
    setRecording(null);
    if (!active) return;
    // Let go before the microphone was ready (or slid away): nothing to send.
    if (!keep || !recorderReady.current) {
      active.cancel();
      if (keep) setProblem("Hold the mic while you talk, then let go to send.");
      return;
    }
    const { wav, durationMs } = await active.stop();
    if (durationMs < 700) { setProblem("Too short. Hold on a little longer."); return; }
    // The note shows in the conversation at once and uploads there; the
    // composer stays free for the next message.
    send({ kind: "voice", body: "", asset_id: null, reply_to_id: replyTo?.id ?? null }, { wav, durationMs });
    setReplyTo(null);
  }

  // Press and hold the mic to record, release to send, slide left to cancel.
  const hold = useHoldToRecord(() => void startRecording("hold"), send => void finishRecording(send));

  useEffect(() => () => recorder.current?.cancel(), []);

  // Started from the keyboard: focus the recording bar once, so Tab reaches Cancel and Send.
  const lockedBar = useRef<HTMLDivElement | null>(null);
  useEffect(() => { if (recording === "locked") lockedBar.current?.focus({ preventScroll: true }); }, [recording]);

  async function react(message: ChatMessage, emoji: ChatReaction) {
    const next = message.reactions?.[me] === emoji ? null : emoji;
    setActiveId(null);
    const optimistic = { ...message.reactions };
    if (next) optimistic[me] = next; else delete optimistic[me];
    setMessages(current => current.map(m => m.id === message.id ? { ...m, reactions: optimistic } : m));
    try {
      const reactions = await reactToMessage(token, message.id, next);
      setMessages(current => current.map(m => m.id === message.id ? { ...m, reactions } : m));
    } catch (e) {
      setMessages(current => current.map(m => m.id === message.id ? { ...m, reactions: message.reactions } : m));
      setProblem(e instanceof Error ? e.message : "Reaction not saved.");
    }
  }

  const byId = useMemo(() => new Map(messages.map(m => [m.id, m])), [messages]);
  const lastMine = useMemo(() => [...messages].reverse().find(m => m.sender_id === me), [messages, me]);
  const typing = typingUntil > 0;
  const fightIn = role === "coach" && row.fight_date
    ? Math.round((Date.parse(row.fight_date) - Date.parse(today)) / 86_400_000) : null;

  return (
    <>
      <header className={styles.threadHeader}>
        <button type="button" className={styles.back} onClick={onBack} aria-label="Back to chats">
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" fill="none" stroke="currentColor"
            strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round"><path d="M15 5l-7 7 7 7" /></svg>
        </button>
        <Avatar name={row.name} url={row.avatar_url} small />
        <div className={styles.threadTitle}>
          <strong>{row.name}</strong>
          <small>{typing ? "typing…" : !row.active ? "Chat ended" : fightIn !== null && fightIn >= 0
            ? `Fight in ${fightIn} ${fightIn === 1 ? "day" : "days"}` : role === "coach" ? "Fighter" : "Your coach"}</small>
        </div>
        {role === "coach" && row.active ? (
          <Link className={styles.headerLink} href={`/coach/fighters/${row.other_id}`}>Profile</Link>
        ) : null}
      </header>

      <div className={styles.messages} ref={scroller}
        onScroll={event => {
          const box = event.currentTarget;
          stickToBottom.current = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
        }}>
        {hasMore ? <button type="button" className={styles.older} onClick={() => void loadOlder()}>Load earlier messages</button> : null}
        {loading ? <div className={styles.threadLoading}><Skeleton height={36} width="55%" /><Skeleton height={36} width="40%" /></div> : null}
        {!loading && !messages.length ? (
          <div className={styles.starter}>
            <p className={styles.kicker}>Private</p>
            <p>Only you and {otherName} see this chat. Send training questions, clips and voice notes here.</p>
            {role === "athlete" ? <p className={styles.safety}>If a message ever worries you, open it and choose Report.</p> : null}
          </div>
        ) : null}
        {messages.map((message, index) => {
          const mine = message.sender_id === me;
          const previous = messages[index - 1];
          const newDay = !previous || !sameDay(previous.created_at, message.created_at);
          const file = files[message.id];
          const local = message.kind === "voice" ? localVoice[message.client_id] : undefined;
          const shape = message.id > 0 ? mediaShape(me, message.id) : null;
          const voiceMs = file?.duration_ms ?? local?.durationMs ?? shape?.duration_ms ?? null;
          const box = message.kind === "photo" ? photoBox(file?.width ?? shape?.width, file?.height ?? shape?.height) : null;
          const reply = message.reply_to_id ? byId.get(message.reply_to_id) : null;
          const reactions = reactionSummary(message.reactions, me);
          return (
            <Fragment key={message.client_id}>
              {newDay ? <p className={styles.day}><span>{chatDayLabel(message.created_at)}</span></p> : null}
              <div className={`${styles.message} ${mine ? styles.mine : styles.theirs}`}>
                <div className={styles.bubble} data-status={message.status}
                  onClick={event => {
                    if ((event.target as HTMLElement).closest("audio, a, button, [role=slider]")) return;
                    if (message.id > 0) setActiveId(activeId === message.id ? null : message.id);
                  }}>
                  {message.id > 0 ? (
                    <button type="button" className={styles.more} aria-expanded={activeId === message.id}
                      aria-label="Message options" onClick={() => setActiveId(activeId === message.id ? null : message.id)}>⋯</button>
                  ) : null}
                  {reply ? (
                    <span className={styles.replyQuote}>
                      <b>{reply.sender_id === me ? "You" : otherName}</b>
                      {reply.body || (reply.kind === "photo" ? "Photo" : reply.kind === "voice" ? "Voice note" : reply.quote?.title)}
                    </span>
                  ) : null}
                  {message.quote ? <TrainingCard quote={message.quote} /> : null}
                  {message.kind === "photo" && box ? (
                    // The frame has the photo's final size from the start; a prefetched photo is there at once.
                    <span className={styles.photoFrame} style={{ width: box.width, height: box.height }}>
                      {file ? (
                        // eslint-disable-next-line @next/next/no-img-element -- short-lived signed link
                        <img className={styles.photo} src={file.thumb_url ?? file.url} alt="Photo"
                          width={box.width} height={box.height}
                          onError={() => dropLink(message.id)} />
                      ) : <span className="sr-only">Photo loading</span>}
                    </span>
                  ) : null}
                  {message.kind === "voice" ? (
                    // The same player as voice notes on training: a waveform, not the browser's bar.
                    // Once played it keeps the audio, so its link no longer needs renewing.
                    // A note sent from this device plays from the recording straight away;
                    // Others are playable at once too: the link is fetched on play if it
                    // hasn't arrived yet. Only a note still uploading elsewhere waits.
                    <div className={styles.voice}>
                      {file || local || message.id > 0 ? <VoicePlayer id={`chat-${message.client_id}`} durationMs={voiceMs}
                        tone={mine ? "onAccent" : "coach"} seekLabel="Voice note position"
                        playLabel={`Play voice note from ${mine ? "you" : otherName}, ${formatDuration(voiceMs)}`}
                        load={async () => {
                          if (local) return local.wav;
                          playing.current.add(message.id);
                          const known = filesRef.current[message.id]?.url;
                          if (known) return known;
                          const generation = cacheGeneration();
                          const [link] = await getChatFiles(token, [message.id]);
                          if (!link) throw new Error("unavailable");
                          if (cacheGeneration() === generation) rememberLinks(me, [link]);
                          return link.url;
                        }} />
                        : <VoicePlaceholder id={`chat-${message.client_id}`} tone={mine ? "onAccent" : "coach"}
                          label="Voice note loading" />}
                    </div>
                  ) : null}
                  {message.body ? <span className={styles.body}>{message.body}</span> : null}
                  <span className={styles.meta}>
                    {message.status === "failed" ? (
                      <button type="button" className={styles.retry} onClick={() => retry(message)}>Not sent · Retry</button>
                    ) : message.status === "uploading" ? <span role="status">Uploading…</span>
                      : message.status === "sending" ? <span role="status">Sending…</span>
                      : chatTime(message.created_at)}
                  </span>
                </div>
                {reactions.length ? (
                  <span className={styles.reactions}>
                    {reactions.map(r => <span key={r.emoji} data-mine={r.mine || undefined}>{r.emoji}{r.count > 1 ? r.count : ""}</span>)}
                  </span>
                ) : null}
                {activeId === message.id && row.active ? (
                  <div className={styles.actions} role="toolbar" aria-label="Message actions">
                    {CHAT_REACTIONS.map(emoji => (
                      <button key={emoji} type="button" aria-pressed={message.reactions?.[me] === emoji}
                        aria-label={`React ${emoji}`} onClick={() => void react(message, emoji)}>{emoji}</button>
                    ))}
                    <button type="button" className={styles.textAction} onClick={() => { setReplyTo(message); setActiveId(null); }}>Reply</button>
                    {!mine ? <button type="button" className={styles.textAction} onClick={() => { setReporting(message); setActiveId(null); }}>Report</button> : null}
                  </div>
                ) : null}
                {activeId === message.id && !row.active && !mine ? (
                  <div className={styles.actions}><button type="button" className={styles.textAction} onClick={() => { setReporting(message); setActiveId(null); }}>Report</button></div>
                ) : null}
                {mine && lastMine?.client_id === message.client_id ? (
                  <span className={styles.receipt}>{receipt(message, otherReadAt)}</span>
                ) : null}
              </div>
            </Fragment>
          );
        })}
        {typing ? <div className={`${styles.message} ${styles.theirs}`}><span className={styles.typing} aria-label={`${otherName} is typing`}><i /><i /><i /></span></div> : null}
      </div>

      {problem ? <p className={styles.problem} role="alert">{problem}<button type="button" onClick={() => setProblem("")} aria-label="Dismiss">×</button></p> : null}
      {reporting ? <ReportForm token={token} message={reporting} onDone={() => setReporting(null)} /> : null}

      {row.active ? (
        <div className={styles.composer}>
          {replyTo || quote ? (
            <div className={styles.attached}>
              {replyTo ? (
                <Chip onClear={() => setReplyTo(null)} label="Cancel reply">
                  <b>Replying to {replyTo.sender_id === me ? "yourself" : otherName}</b>
                  {replyTo.body || (replyTo.kind === "photo" ? "Photo" : replyTo.kind === "voice" ? "Voice note" : replyTo.quote?.title)}
                </Chip>
              ) : null}
              {quote ? <Chip onClear={() => setQuote(null)} label="Remove training card"><b>Training card</b>This session will be attached</Chip> : null}
            </div>
          ) : null}
          {/* One row in every state: + | field or recording | mic or send. The mic is the
              same element while idle and while held, so it never moves under the finger. */}
          <div className={styles.inputRow} data-recording={recording ?? undefined}
            data-cancelling={recording === "hold" && hold.cancelling ? true : undefined}>
            <button type="button" className={styles.iconButton} aria-label="Send a photo" disabled={Boolean(stage) || Boolean(recording)}
              onClick={() => photoInput.current?.click()}>+</button>
            <input ref={photoInput} type="file" accept="image/*" hidden
              onChange={event => { void sendPhoto(event.target.files?.[0]); event.target.value = ""; }} />
            {recording ? (
              // Focus moves here, not to Send: a held Enter that started the recording
              // must never land on Send and send it straight away.
              <div key="recording" className={styles.recordingField} role="group" aria-label="Recording voice note"
                tabIndex={-1} ref={lockedBar}>
                <span className={styles.recDot} aria-hidden="true" />
                <RecordingClock />
                <span className={styles.liveWave} aria-hidden="true"><i /><i /><i /><i /><i /></span>
                {recording === "hold" ? (
                  <span className={styles.slideHint} aria-live="polite"
                    style={{ transform: `translateX(${-Math.min(hold.slide, SLIDE_TO_CANCEL_PX)}px)` }}>
                    {hold.cancelling ? "Release to cancel" : "‹ Slide left to cancel"}
                  </span>
                ) : (
                  <button type="button" className={styles.textAction} onClick={() => void finishRecording(false)}>Cancel</button>
                )}
              </div>
            ) : (
              <label key="field" className={styles.field}>
                <span className="sr-only">Message</span>
                <textarea ref={field} rows={1} value={text} maxLength={CHAT_MAX_CHARS} placeholder={stage || "Message"} disabled={Boolean(stage)}
                  onKeyDown={onKey}
                  onChange={event => { setText(event.target.value); if (event.target.value) live.current?.typing(); }} />
              </label>
            )}
            {recording === "locked" ? (
              <button key="send-voice" type="button" className={styles.send} onClick={() => void finishRecording(true)} aria-label="Send voice note">➤</button>
            ) : !recording && (text.trim() || quote) ? (
              <button key="send" type="button" className={styles.send} onClick={submitText} aria-label="Send">➤</button>
            ) : (
              <button key="mic" type="button" className={`${styles.send} ${styles.mic}`} aria-label="Hold to record a voice note"
                title="Hold to record" disabled={Boolean(stage)} data-held={hold.holding || undefined}
                onPointerDown={event => { if (!stage && !recorder.current) hold.onPointerDown(event); }}
                onContextMenu={event => event.preventDefault()}
                onKeyDown={event => {
                  if (event.key !== "Enter" && event.key !== " ") return;
                  event.preventDefault();
                  // A held key repeats: never restart a recording that is already running.
                  if (event.repeat || recorder.current) return;
                  void startRecording("locked");
                }}>
                <MicIcon />
              </button>
            )}
          </div>
        </div>
      ) : (
        <p className={styles.readOnly}>
          {role === "athlete" ? "You're no longer connected to this coach. Your messages stay here." : "This fighter is no longer connected."}
        </p>
      )}
    </>
  );
}

function Chip({ children, onClear, label }: { children: ReactNode; onClear: () => void; label: string }) {
  return (
    <span className={styles.chip}>
      <span>{children}</span>
      <button type="button" onClick={onClear} aria-label={label}>×</button>
    </span>
  );
}

function TrainingCard({ quote }: { quote: NonNullable<ChatMessage["quote"]> }) {
  return (
    <span className={styles.card}>
      <b>{quote.title}</b>
      <small>{[quote.day ? formatAppDate(quote.day) : "", quote.meta].filter(Boolean).join(" · ")}</small>
    </span>
  );
}

function RecordingClock() {
  const [since] = useState(() => Date.now());
  const [now, setNow] = useState(since);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, []);
  return <span className={styles.clock}>{formatDuration(now - since)}</span>;
}

function ReportForm({ token, message, onDone }: { token: string; message: ChatMessage; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  async function submit() {
    setState("sending");
    try {
      await reportMessage(token, message.id, reason.trim());
      setState("sent");
    } catch {
      setState("failed");
    }
  }
  return (
    <div className={styles.report} role="dialog" aria-label="Report message">
      {state === "sent" ? (
        <>
          <p>Thanks. We&apos;ll review it. You can also disconnect from this coach in Coach access at any time.</p>
          <button type="button" className="ghost-button" onClick={onDone}>Close</button>
        </>
      ) : (
        <>
          <label>
            <span>What&apos;s wrong with this message?</span>
            <textarea rows={2} maxLength={500} value={reason} onChange={event => setReason(event.target.value)} />
          </label>
          {state === "failed" ? <p className={styles.problemInline}>Report not sent. Please retry.</p> : null}
          <div className={styles.reportActions}>
            <button type="button" className="ghost-button" onClick={onDone}>Cancel</button>
            <button type="button" className="cta" disabled={!reason.trim() || state === "sending"} onClick={() => void submit()}>
              Send report
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </svg>
  );
}
