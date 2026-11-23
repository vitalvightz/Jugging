"use client";
// The athlete's way into coach chat on a phone: a chat button beside the
// avatar, with the unread count. Shown only once the athlete has a coach to
// talk to. The count refreshes on every page change, when the app comes back
// to the front, and once a minute while it is open.
import Link from "next/link";
import { useEffect, useState } from "react";
import { useAppSession } from "@/components/auth-provider";
import { fetchInbox } from "@/lib/coach-chat";

const REFRESH_MS = 60_000;

/** Unread chat messages for the signed-in account (coach or fighter); null with no chats. */
export function useChatUnread(pathname: string, enabled = true): number | null {
  const { session } = useAppSession();
  const token = enabled ? session?.access_token : undefined;
  const [unread, setUnread] = useState<number | null>(null);

  useEffect(() => {
    if (!token) return;
    let controller = new AbortController();
    const load = () => {
      controller.abort();
      controller = new AbortController();
      fetchInbox(token, controller.signal)
        .then(rows => setUnread(rows.length ? rows.reduce((total, row) => total + row.unread, 0) : null))
        .catch(() => undefined);
    };
    load();
    const onVisible = () => { if (document.visibilityState === "visible") load(); };
    document.addEventListener("visibilitychange", onVisible);
    const timer = window.setInterval(onVisible, REFRESH_MS);
    return () => {
      controller.abort();
      document.removeEventListener("visibilitychange", onVisible);
      window.clearInterval(timer);
    };
  }, [token, pathname]);
  return unread;
}

export function PhoneChatButton({ pathname }: { pathname: string }) {
  const unread = useChatUnread(pathname);
  if (unread === null || pathname === "/chat") return null;
  return (
    <Link href="/chat" className="phone-chat-button"
      aria-label={unread ? `Coach chat, ${unread} unread` : "Coach chat"}>
      <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round">
        <path d="M4 5h16v11H9l-5 4z" />
      </svg>
      {unread ? <span className="phone-chat-badge" aria-hidden="true">{unread > 9 ? "9+" : unread}</span> : null}
    </Link>
  );
}
