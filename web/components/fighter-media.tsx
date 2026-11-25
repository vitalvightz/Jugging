"use client";

import { useEffect, useState } from "react";
import { getFighterMedia } from "@/lib/api";
import type { FighterMedia } from "@/lib/api-schema.generated";
import { humanize, type Fighter } from "@/lib/coach";
import { formatAppDate } from "@/lib/date-format";
import { PROGRESS_PHOTO_LIMIT, TrainingVideos } from "./training-videos";
import styles from "./fighter-media.module.css";

/**
 * A fighter's shared media on the coach's fighter page: their progress photos
 * and the latest sessions or sparring reports with files. Coaches add session
 * files from Training history and can reply to sparring reports here. Files
 * are available only when the fighter shares media with the coach.
 */
export function FighterMediaPanel({ token, fighter, onSummary }: {
  token: string; fighter: Fighter;
  /** One line for the section header: what is there, and how recent. */
  onSummary?: (summary: string) => void;
}) {
  const [media, setMedia] = useState<FighterMedia | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!onSummary) return;
    if (failed) { onSummary("Couldn't load"); return; }
    if (!media) return;
    if (!media.share_media) { onSummary("Not shared"); return; }
    const latest = media.recent.map(item => item.last_at).sort().at(-1);
    const parts = [
      media.progress_count ? `${media.progress_count} progress ${media.progress_count === 1 ? "photo" : "photos"}` : null,
      media.recent.length ? `files on ${media.recent.length} ${media.recent.length === 1 ? "session" : "sessions"}` : null,
      latest ? `latest ${formatAppDate(latest.slice(0, 10))}` : null,
    ].filter(Boolean);
    onSummary(parts.length ? parts.join(" · ") : "Nothing shared yet");
  }, [media, failed, onSummary]);

  useEffect(() => {
    const controller = new AbortController();
    getFighterMedia(token, fighter.id, controller.signal)
      .then(value => { if (!controller.signal.aborted) { setMedia(value); setFailed(false); } })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [token, fighter.id]);

  if (failed) return <p className={styles.muted} role="status">Media couldn&apos;t load. Reopen this fighter to try again.</p>;
  if (!media) return <p className={styles.muted} role="status">Loading media…</p>;
  if (!media.share_media) {
    return <p className={styles.muted}>Photos, voice notes and PDFs: Not shared. {fighter.name || "Your fighter"} can turn
      sharing on from their coach settings.</p>;
  }

  // "15 Nov": short enough to sit beside Attach on the smallest phones.
  const shortDay = (day: string) => formatAppDate(day).split(" ").slice(1, 3).join(" ") || day;
  function recordName(source: string, targetId: string, exerciseRef: string, lastAt: string) {
    if (source === "sparring") {
      const report = fighter.sparring_reports?.find(item => item.id === targetId);
      return `Sparring · ${shortDay(report?.day ?? lastAt.slice(0, 10))}`;
    }
    const session = fighter.sessions.find(item => item.id === targetId);
    const title = session ? session.title?.trim() || humanize(session.type) : "Session";
    const day = shortDay(session?.day ?? lastAt.slice(0, 10));
    return exerciseRef ? `${title} exercise · ${day}` : `${title} · ${day}`;
  }

  return <div className={styles.grid}>
    <section className={styles.column} aria-label="Progress photos">
      {media.progress_count ? <TrainingVideos token={token} viewer="coach" athleteId={fighter.id} heading="Progress photos"
        label="Progress photos" only={["photo"]} limit={PROGRESS_PHOTO_LIMIT}
        target={{ source: "progress", target_id: fighter.id }} />
        : <div className={styles.placeholder}>
          <h3>Progress photos</h3>
          <p className={styles.muted}>None yet. {fighter.name || "Your fighter"} adds them from their weight page.</p>
        </div>}
    </section>
    <section className={styles.column} aria-label="Recent session files">
      {media.recent.length ? media.recent.map(item => {
        const name = recordName(item.source, item.target_id, item.exercise_ref ?? "", item.last_at);
        return <TrainingVideos key={`${item.source}:${item.target_id}:${item.exercise_ref}`} token={token} viewer="coach"
          athleteId={fighter.id} heading={name} label={name} allowAdding={item.source === "sparring"}
          target={{ source: item.source, target_id: item.target_id, exercise_ref: item.exercise_ref ?? "" }} />;
      }) : <div className={styles.placeholder}>
        <h3>Session files</h3>
        <p className={styles.muted}>No files on recent sessions yet. Open a session in Training history to send a voice note or PDF.</p>
      </div>}
    </section>
  </div>;
}
