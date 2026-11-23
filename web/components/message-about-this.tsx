"use client";
// "Message about this" on a session: opens the coach chat with the session
// attached as a training card. Fighters see it only with a connected coach.
import Link from "next/link";
import { useEffect, useState } from "react";
import { chatLink, hasConnectedCoach, type QuoteSource } from "@/lib/coach-chat";

export function MessageAboutThis({ token, viewer, athleteId, source, targetId }: {
  token: string; viewer: "athlete" | "coach"; athleteId: string; source: QuoteSource; targetId: string;
}) {
  const [show, setShow] = useState(viewer === "coach");
  useEffect(() => {
    if (viewer === "coach") return;
    let active = true;
    void hasConnectedCoach(token).then(connected => { if (active) setShow(connected); });
    return () => { active = false; };
  }, [token, viewer]);
  if (!show) return null;
  const quote = { source, target_id: targetId };
  const href = viewer === "coach" ? chatLink("coach", athleteId, quote)
    : `/chat?${new URLSearchParams({ quote: `${source}:${targetId}` })}`;
  return (
    <Link className="ghost-button" href={href} style={{ justifySelf: "start" }}
      aria-label={viewer === "coach" ? "Message about this session" : "Ask your coach about this session"}>
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round">
        <path d="M4 5h16v11H9l-5 4z" />
      </svg>
      {viewer === "coach" ? "Message" : "Ask your coach"}
    </Link>
  );
}
