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
    <Link className="ghost-button" href={href} style={{ justifySelf: "start" }}>
      {viewer === "coach" ? "Message about this" : "Ask your coach about this"}
    </Link>
  );
}
