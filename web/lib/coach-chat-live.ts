// Live coach chat over Supabase Realtime. Database changes are filtered by the
// same SELECT policies as a read, so a subscriber only ever receives rows they
// may see. Typing goes over a private broadcast channel per chat; it never
// touches a table, and only the chat's participants may join it.
import type { RealtimeChannel } from "@supabase/supabase-js";
import { getSupabaseBrowserClient } from "./supabase";
import type { ChatMessage, ChatRole } from "./coach-chat";

export type ChatRow = {
  id: string;
  coach_id: string;
  athlete_id: string;
  last_message_at: string | null;
  last_preview: string;
  last_sender_id: string | null;
  coach_unread: number;
  athlete_unread: number;
  coach_read_at: string | null;
  athlete_read_at: string | null;
};

const TYPING_EVERY_MS = 2500;

function client(token: string) {
  const supabase = getSupabaseBrowserClient();
  void supabase.realtime.setAuth(token);
  return supabase;
}

export function subscribeToChat(token: string, chatId: string, me: string, handlers: {
  onMessage: (message: ChatMessage) => void;
  onChat: (chat: ChatRow) => void;
  onTyping: () => void;
}) {
  const supabase = client(token);
  const filter = `chat_id=eq.${chatId}`;
  const changes: RealtimeChannel = supabase.channel(`coach-chat-rows:${chatId}`)
    .on("postgres_changes", { event: "INSERT", schema: "public", table: "coach_messages", filter },
      payload => handlers.onMessage(payload.new as ChatMessage))
    .on("postgres_changes", { event: "UPDATE", schema: "public", table: "coach_messages", filter },
      payload => handlers.onMessage(payload.new as ChatMessage))
    .on("postgres_changes", { event: "UPDATE", schema: "public", table: "coach_chats", filter: `id=eq.${chatId}` },
      payload => handlers.onChat(payload.new as ChatRow))
    .subscribe();
  const typing: RealtimeChannel = supabase.channel(`coach-chat:${chatId}`, {
    config: { private: true, broadcast: { self: false } },
  })
    .on("broadcast", { event: "typing" }, ({ payload }) => {
      if (payload && (payload as { from?: string }).from !== me) handlers.onTyping();
    })
    .subscribe();
  let lastTyping = 0;
  return {
    /** Throttled: at most one typing signal every 2.5 seconds. */
    typing() {
      const now = Date.now();
      if (now - lastTyping < TYPING_EVERY_MS) return;
      lastTyping = now;
      void typing.send({ type: "broadcast", event: "typing", payload: { from: me } });
    },
    close() {
      void supabase.removeChannel(changes);
      void supabase.removeChannel(typing);
    },
  };
}

/** Inbox rows for the caller's side of every chat: previews, unread, receipts. */
export function subscribeToInbox(token: string, me: string, role: ChatRole, onChat: (chat: ChatRow) => void) {
  const supabase = client(token);
  const column = role === "coach" ? "coach_id" : "athlete_id";
  const channel = supabase.channel(`coach-chat-inbox:${me}`)
    .on("postgres_changes", { event: "*", schema: "public", table: "coach_chats", filter: `${column}=eq.${me}` },
      payload => { if (payload.new && "id" in payload.new) onChat(payload.new as ChatRow); })
    .subscribe();
  return () => { void supabase.removeChannel(channel); };
}
