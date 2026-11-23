"use client";
// Coach chat: the WhatsApp layout people already know, plus quoted training
// cards. One screen for coaches (/coach/chats) and fighters (/chat).
import Link from "next/link";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { RequireAuth } from "@/components/auth-guard";
import { useAppSession } from "@/components/auth-provider";
import { Skeleton } from "@/components/skeleton";
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
import { formatAppDate } from "@/lib/date-format";
import { getSupabaseBrowserClient } from "@/lib/supabase";
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
  const [inbox, setInbox] = useState<InboxRow[] | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [quote, setQuote] = useState<QuoteRef | null>(null);
  const opened = useRef(false);
  const selectedRef = useRef<string | null>(null);

  const load = useCallback(async () => {
    try {
      const rows = await fetchInbox(token);
      setInbox(sortInbox(rows));
      setError("");
      if (!opened.current) {
        opened.current = true;
        const { withId, quote: linked } = readParams();
        const open = rows.filter(row => row.active);
        // A linked session waits for the first chat picked when there is a choice.
        setQuote(linked);
        if (withId && rows.some(row => row.other_id === withId)) {
          selectedRef.current = withId;
          setSelected(withId);
        } else if (!withId && role === "athlete" && open.length === 1) {
          selectedRef.current = open[0].other_id;
          setSelected(open[0].other_id);
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong. Please retry.");
    }
  }, [token, role]);

  useEffect(() => {
    const first = window.setTimeout(() => void load(), 0);
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    return () => { window.clearTimeout(first); window.removeEventListener("focus", onFocus); };
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
    <div className={styles.shell} data-open={current ? "thread" : "list"}>
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
      <section className={styles.thread} aria-label={current ? `Chat with ${current.name}` : "Chat"}>
        {current ? (
          <ChatThread key={current.other_id} token={token} me={me} role={role} row={current} initialQuote={quote}
            onBack={() => select(null)} onRow={patch => updateRow(current.other_id, patch)} />
        ) : (
          <div className={styles.placeholder}>
            <p className={styles.kicker}>Coach chat</p>
            <p>{inbox?.length ? "Pick a chat to start." : "Your conversations will show here."}</p>
          </div>
        )}
      </section>
    </div>
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

type Pending = { draft: ChatDraft; clientId: string };

async function uploadChatFile(token: string, kind: "photo" | "voice", blob: Blob, onStage: (stage: string) => void) {
  onStage(kind === "photo" ? "Uploading photo…" : "Uploading voice note…");
  const permission = await reserveFileUpload(token, kind, blob.size);
  const { error } = await getSupabaseBrowserClient().storage.from(permission.bucket)
    .upload(permission.path, blob, { contentType: permission.content_type, upsert: false });
  if (error) throw new Error("Upload failed. Check your connection and try again.");
  onStage("Checking…");
  const result = await processFileUpload(token, permission.asset_id);
  if (result.status !== "ready") throw new Error(result.reason || "That file could not be used.");
  return permission.asset_id;
}

function ChatThread({ token, me, role, row, initialQuote, onBack, onRow }: {
  token: string; me: string; role: ChatRole; row: InboxRow; initialQuote: QuoteRef | null;
  onBack: () => void; onRow: (patch: Partial<InboxRow>) => void;
}) {
  const [chatId, setChatId] = useState(row.chat_id);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(Boolean(row.chat_id));
  const [hasMore, setHasMore] = useState(false);
  const [problem, setProblem] = useState("");
  const [otherReadAt, setOtherReadAt] = useState(row.other_read_at);
  const [typingUntil, setTypingUntil] = useState(0);
  const [files, setFiles] = useState<Record<number, ChatFile & { at: number }>>({});
  const [text, setText] = useState("");
  const [quote, setQuote] = useState<QuoteRef | null>(row.active ? initialQuote : null);
  const [replyTo, setReplyTo] = useState<ChatMessage | null>(null);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [reporting, setReporting] = useState<ChatMessage | null>(null);
  const [stage, setStage] = useState("");
  const [recording, setRecording] = useState(false);
  const [today] = useState(() => new Date().toISOString().slice(0, 10));
  const recorder = useRef<VoiceRecorder | null>(null);
  const pending = useRef(new Map<string, Pending>());
  const live = useRef<ReturnType<typeof subscribeToChat> | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);
  const stickToBottom = useRef(true);
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
      setLoading(false);
      setProblem(e instanceof Error ? e.message : "Something went wrong. Please retry.");
    });
    return () => controller.abort();
  }, [token, chatId]);

  // Live messages, reactions, receipts and typing.
  useEffect(() => {
    if (!chatId) return;
    const subscription = subscribeToChat(token, chatId, me, {
      onMessage: message => {
        setMessages(current => mergeMessages(current, [message]));
        if (message.sender_id !== me) setTypingUntil(0);
      },
      onChat: chat => setOtherReadAt(chat.coach_id === me ? chat.athlete_read_at : chat.coach_read_at),
      onTyping: () => setTypingUntil(Date.now() + TYPING_SHOWN_MS),
    });
    live.current = subscription;
    return () => { subscription.close(); live.current = null; };
  }, [token, chatId, me]);

  useEffect(() => {
    if (!typingUntil) return;
    const timer = window.setTimeout(() => setTypingUntil(0), Math.max(0, typingUntil - Date.now()));
    return () => window.clearTimeout(timer);
  }, [typingUntil]);

  // Read receipts: one cheap call when something from the other side is unread.
  const lastFromOther = useMemo(() => [...messages].reverse().find(m => m.sender_id !== me && !m.status), [messages, me]);
  useEffect(() => {
    if (!chatId || !lastFromOther || document.visibilityState !== "visible") return;
    const timer = window.setTimeout(() => {
      markChatRead(token, chatId).then(() => onRow({ unread: 0 })).catch(() => undefined);
    }, 400);
    return () => window.clearTimeout(timer);
    // onRow is stable per row; the last message from the other side drives this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, chatId, lastFromOther?.id]);

  // File links for every photo and voice note on screen, in one call.
  useEffect(() => {
    const now = Date.now();
    const needed = messages.filter(m => m.asset_id && m.id > 0 && (!files[m.id] || now - files[m.id].at > FILE_LINK_TTL_MS)).map(m => m.id);
    if (!needed.length) return;
    const controller = new AbortController();
    getChatFiles(token, needed.slice(-30), controller.signal).then(links => {
      const at = Date.now();
      setFiles(current => ({ ...current, ...Object.fromEntries(links.map(link => [link.message_id, { ...link, at }])) }));
    }).catch(() => undefined);
    return () => controller.abort();
  }, [token, messages, files]);

  // Stay pinned to the newest message unless the reader scrolled up.
  useEffect(() => {
    const box = scroller.current;
    if (box && stickToBottom.current) box.scrollTop = box.scrollHeight;
  }, [messages, typingUntil]);

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

  async function deliver({ draft, clientId }: Pending) {
    setMessages(current => mergeMessages(current, [{
      id: -Date.now(), chat_id: chatId ?? "", coach_id: "", athlete_id: "", sender_id: me, client_id: clientId,
      kind: draft.kind, body: draft.body, asset_id: draft.asset_id ?? null, reply_to_id: draft.reply_to_id ?? null,
      quote: draft.quote ? { ...draft.quote, day: "", title: "Training session", meta: "" } : null,
      reactions: {}, created_at: new Date().toISOString(), status: "sending",
    }]));
    stickToBottom.current = true;
    try {
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
    }
  }

  function send(draft: ChatDraft) {
    const item = { draft, clientId: newClientId() };
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

  async function startRecording() {
    setProblem("");
    recorder.current?.cancel();
    const next = new VoiceRecorder(() => void finishRecording(true));
    recorder.current = next;
    try {
      await next.start();
    } catch {
      next.cancel();
      if (recorder.current === next) recorder.current = null;
      setProblem("Microphone unavailable. Allow microphone access to record a voice note.");
      return;
    }
    if (recorder.current !== next) { next.cancel(); return; }
    setRecording(true);
  }

  async function finishRecording(keep: boolean) {
    const active = recorder.current;
    recorder.current = null;
    setRecording(false);
    if (!active) return;
    if (!keep) { active.cancel(); return; }
    const { wav, durationMs } = await active.stop();
    if (durationMs < 700) { setProblem("Too short. Hold on a little longer."); return; }
    try {
      const asset = await uploadChatFile(token, "voice", wav, setStage);
      send({ kind: "voice", body: "", asset_id: asset, reply_to_id: replyTo?.id ?? null });
      setReplyTo(null);
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "That voice note could not be sent.");
    } finally {
      setStage("");
    }
  }

  useEffect(() => () => recorder.current?.cancel(), []);

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
        <button type="button" className={styles.back} onClick={onBack} aria-label="Back to chats">‹</button>
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
          const reply = message.reply_to_id ? byId.get(message.reply_to_id) : null;
          const reactions = reactionSummary(message.reactions, me);
          return (
            <Fragment key={message.client_id}>
              {newDay ? <p className={styles.day}><span>{chatDayLabel(message.created_at)}</span></p> : null}
              <div className={`${styles.message} ${mine ? styles.mine : styles.theirs}`}>
                <div className={styles.bubble} data-status={message.status}
                  onClick={event => {
                    if ((event.target as HTMLElement).closest("audio, a, button")) return;
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
                  {message.kind === "photo" ? (
                    file ? (
                      // eslint-disable-next-line @next/next/no-img-element -- short-lived signed link
                      <img className={styles.photo} src={file.thumb_url ?? file.url} alt="Photo"
                        width={file.width ?? undefined} height={file.height ?? undefined}
                        onError={() => setFiles(current => { const next = { ...current }; delete next[message.id]; return next; })} />
                    ) : <span className={styles.photoLoading}>Photo</span>
                  ) : null}
                  {message.kind === "voice" ? (
                    file ? <audio className={styles.voice} controls preload="none" src={file.url}
                      aria-label={`Voice note, ${formatDuration(file.duration_ms)}`} />
                      : <span className={styles.photoLoading}>Voice note</span>
                  ) : null}
                  {message.body ? <span className={styles.body}>{message.body}</span> : null}
                  <span className={styles.meta}>
                    {message.status === "failed" ? (
                      <button type="button" className={styles.retry} onClick={() => retry(message)}>Not sent · Retry</button>
                    ) : chatTime(message.created_at)}
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
          {recording ? (
            <div className={styles.recordingBar}>
              <span className={styles.recDot} aria-hidden="true" />
              <RecordingClock />
              <button type="button" className={styles.textAction} onClick={() => void finishRecording(false)}>Cancel</button>
              <button type="button" className={styles.send} onClick={() => void finishRecording(true)} aria-label="Send voice note">➤</button>
            </div>
          ) : (
            <div className={styles.inputRow}>
              <button type="button" className={styles.iconButton} aria-label="Send a photo" disabled={Boolean(stage)}
                onClick={() => photoInput.current?.click()}>+</button>
              <input ref={photoInput} type="file" accept="image/*" hidden
                onChange={event => { void sendPhoto(event.target.files?.[0]); event.target.value = ""; }} />
              <label className={styles.field}>
                <span className="sr-only">Message</span>
                <textarea rows={1} value={text} maxLength={CHAT_MAX_CHARS} placeholder={stage || "Message"} disabled={Boolean(stage)}
                  onKeyDown={onKey}
                  onChange={event => { setText(event.target.value); if (event.target.value) live.current?.typing(); }} />
              </label>
              {text.trim() || quote ? (
                <button type="button" className={styles.send} onClick={submitText} aria-label="Send">➤</button>
              ) : (
                <button type="button" className={styles.send} onClick={() => void startRecording()} aria-label="Record a voice note"
                  disabled={Boolean(stage)}>
                  <MicIcon />
                </button>
              )}
            </div>
          )}
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
