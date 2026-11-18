"use client";

import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPrivateYouTubeClip, getCommunityExercise, getPrivateMedia, getPrivateMediaSettings } from "@/lib/api";
import type { PrivateMediaPreview, PrivateMediaSettings } from "@/lib/api-schema.generated";
import { ExerciseDemo } from "./exercise-demo";
import { demoThumbnailUrl } from "@/lib/exercise-demo";
import { InjuryDetailSheet } from "./today/injury-detail-sheet";
import styles from "./custom-plan-builder.module.css";
import layout from "./private-exercise-video.module.css";

const PrivateMediaToken = createContext<string | null>(null);
type MediaRequest = { preview: Promise<PrivateMediaPreview | undefined>; expiresAt: number };
const PrivateMediaRequests = createContext<{
  token: string | null;
  requests: Map<string, MediaRequest>;
} | null>(null);
export function PrivateExerciseMediaProvider({ token, children }: { token?: string | null; children: ReactNode }) {
  // Scoped to this signed-in view: previews and expanded players share a read,
  // while token changes discard every cached private/signed URL.
  const scope = useMemo(() => ({ token: token ?? null, requests: new Map<string, MediaRequest>() }), [token]);
  return <PrivateMediaToken.Provider value={token ?? null}>
    <PrivateMediaRequests.Provider value={scope}>{children}</PrivateMediaRequests.Provider>
  </PrivateMediaToken.Provider>;
}

function usePrivateVideoPreview(token: string | null, id: string | undefined, community: boolean) {
  const scope = useContext(PrivateMediaRequests);
  const [loaded, setLoaded] = useState<{ token: string; id: string; community: boolean; media?: PrivateMediaPreview; error?: boolean }>();
  useEffect(() => {
    if (!token || !id) return;
    let active = true;
    const controller = new AbortController();
    const cache = scope?.token === token ? scope.requests : undefined;
    const key = `${community ? "community" : "private"}:${id}`;
    const cached = cache?.get(key);
    let preview = cached && cached.expiresAt > Date.now() ? cached.preview : undefined;
    if (!preview) {
      preview = community
        ? getCommunityExercise(token, id, "revisions", cache ? undefined : controller.signal).then(entry => entry.media ?? undefined)
        : getPrivateMedia(token, id, cache ? undefined : controller.signal);
      // Uploaded renditions are signed for 60 seconds. Refresh before expiry
      // when a row/player remounts instead of reusing an expired private URL.
      cache?.set(key, { preview, expiresAt: Date.now() + 45_000 });
      // Unavailable media can be retried by reopening; don't retain a failure.
      void preview.then(media => { if (!media) cache?.delete(key); }, () => cache?.delete(key));
    }
    void preview.then(media => { if (active) setLoaded({ token, id, community, media, error: !media }); })
      .catch(() => { if (active) setLoaded({ token, id, community, error: true }); });
    return () => { active = false; controller.abort(); };
  }, [token, id, community, scope]);
  return loaded?.token === token && loaded.id === id && loaded.community === community ? loaded : undefined;
}

export function PrescribedPrivateVideoThumbnail({ id, community = false }: { id: string; community?: boolean }) {
  const token = useContext(PrivateMediaToken);
  const media = usePrivateVideoPreview(token, id, community)?.media;
  if (!token) return null;
  return <span className="ex-row-thumb" aria-hidden="true">
    {media?.provider === "youtube" && media.video_id ? (
      // eslint-disable-next-line @next/next/no-img-element -- remote YouTube thumbnail
      <img src={demoThumbnailUrl(media.video_id, "mq")} alt="" loading="lazy"
        onError={event => { event.currentTarget.style.display = "none"; }} />
    ) : media?.url ? (
      <video src={`${media.url}#t=0.1`} muted playsInline preload="metadata" tabIndex={-1} />
    ) : null}
    <span className="ex-demo-play-glyph ex-demo-play-glyph-sm" />
  </span>;
}
export function PrescribedPrivateVideo({ id, name, community = false }: { id: string; name: string; community?: boolean }) {
  const token = useContext(PrivateMediaToken);
  // On a plan card the athlete's video sits exactly where a library demo
  // would: full row width, and a portrait clip takes the same side column.
  return token ? <PrivateExerciseVideo token={token} id={id} name={name} community={community} inline /> : null;
}

export function PrivateVideoPlayer({ media, name, visibility = "private", kind = "Demo" }: { media: PrivateMediaPreview; name: string; visibility?: "private" | "community"; kind?: string }) {
  const video = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false), [slow, setSlow] = useState(false);
  const [failed, setFailed] = useState(false);
  // Uploads without a stored orientation are read from the file itself.
  const [measured, setMeasured] = useState<"portrait" | "landscape">();
  const orientation = media.orientation ?? measured;
  if (media.provider === "youtube" && media.video_id) return (
    <ExerciseDemo key={`${media.video_id}:${media.start_s}:${media.end_s}`} exerciseName={name} preview media={{
      provider: "youtube", video_id: media.video_id, start_s: media.start_s, end_s: media.end_s,
      channel_title: media.channel_title, orientation: media.orientation, source: "curated",
    }} />
  );
  if (!media.url || failed) return <p role="status">Video unavailable. Reopen the preview to try again.</p>;
  return (
    <div className="ex-demo" data-orientation={orientation === "portrait" ? "portrait" : undefined}>
      <div className="ex-demo-frame">
        <video ref={video} className="ex-demo-player" src={media.url} playsInline muted loop preload="metadata"
          aria-label={kind === "Demo" ? `${name} demonstration` : `${name} ${kind.toLowerCase()}`} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
          onLoadedMetadata={(e) => {
            const { videoWidth, videoHeight } = e.currentTarget;
            if (videoWidth && videoHeight) setMeasured(videoHeight > videoWidth ? "portrait" : "landscape");
          }}
          onError={() => setFailed(true)} />
      </div>
      <div className="ex-demo-caption"><span className="ex-demo-meta">{kind} · {visibility === "community" ? "Community" : "Private"}</span></div>
      <div className="ex-demo-controls" role="group" aria-label="Demo controls">
        <button type="button" className="ex-demo-control" onClick={() => {
          if (playing) video.current?.pause(); else void video.current?.play().catch(() => setFailed(true));
        }}>{playing ? "Pause" : "Play"}</button>
        <button type="button" className="ex-demo-control" onClick={() => {
          if (video.current) { video.current.currentTime = 0; void video.current.play().catch(() => setFailed(true)); }
        }}>Replay</button>
        <button type="button" className="ex-demo-control" aria-pressed={slow} onClick={() => {
          if (video.current) video.current.playbackRate = slow ? 1 : 0.5;
          setSlow(!slow);
        }}>0.5×</button>
        <button type="button" className="ex-demo-control" onClick={() => {
          const element = video.current as (HTMLVideoElement & { webkitEnterFullscreen?: () => void }) | null;
          // iPhone Safari only fullscreens a <video> through its own API.
          if (element?.requestFullscreen) void element.requestFullscreen().catch(() => element.webkitEnterFullscreen?.());
          else element?.webkitEnterFullscreen?.();
        }}>Full screen</button>
      </div>
      {/* Renditions intentionally strip audio and all embedded metadata. */}
    </div>
  );
}

export function PrivateExerciseVideo({ token, id, name, community = false, inline = false }: {
  token: string; id: string; name: string; community?: boolean;
  /** Render unwrapped at full width (plan cards) instead of the compact preview. */
  inline?: boolean;
}) {
  const { media, error } = usePrivateVideoPreview(token, id, community) ?? {};
  const content = media ? <PrivateVideoPlayer key={media.url ?? media.id} media={media} name={name} visibility={community ? "community" : "private"} />
    : <p role="status">{error ? "Video unavailable. You can keep using your exercise." : "Opening video…"}</p>;
  return inline ? content : <div className={layout.preview}>{content}</div>;
}

export function PrivateExerciseVideoSheet({ token, name, onClose, onAttach, initialMedia }: {
  token: string; name: string; onClose: () => void; onAttach: (id: string) => void; initialMedia?: PrivateMediaPreview;
}) {
  const [settings, setSettings] = useState<PrivateMediaSettings>();
  const [url, setUrl] = useState(initialMedia?.video_id ? `https://www.youtube.com/watch?v=${initialMedia.video_id}` : ""), [start, setStart] = useState(String(initialMedia?.start_s ?? 0)), [end, setEnd] = useState(String(initialMedia?.end_s ?? 30));
  const [media, setMedia] = useState<PrivateMediaPreview>(), [pending, setPending] = useState(false);
  const [error, setError] = useState<string>();
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void getPrivateMediaSettings(token, controller.signal).then(setSettings)
      .catch(() => { if (!controller.signal.aborted) setError("Video options are temporarily unavailable."); });
    return () => { controller.abort(); request.current?.abort(); };
  }, [token]);
  function changed() { setMedia(undefined); setError(undefined); }
  async function verifyYouTube(event: React.FormEvent) {
    event.preventDefault();
    changed();
    const start_s = Number(start), end_s = Number(end);
    if (!start.trim() || !end.trim() || !Number.isInteger(start_s) || !Number.isInteger(end_s) || start_s < 0 || end_s <= start_s || end_s - start_s > 30) {
      setError("Choose a clip between 1 and 30 seconds."); return;
    }
    setPending(true);
    request.current = new AbortController();
    try { setMedia(await createPrivateYouTubeClip(token, { url, start_s, end_s }, request.current.signal)); }
    catch (reason) { if (!request.current.signal.aborted) setError(reason instanceof Error ? reason.message : "Unable to preview video."); }
    finally { setPending(false); }
  }
  const clipForm = <form onSubmit={(e) => void verifyYouTube(e)} className={layout.form}>
    <fieldset disabled={pending} className={layout.form}>
      <label><span>YouTube link</span><input type="url" required value={url} maxLength={500} onChange={(e) => { setUrl(e.target.value); changed(); }} /></label>
      <div className={layout.times}>
        <label><span>Start (seconds)</span><input inputMode="numeric" type="number" min={0} max={86400} required value={start} onChange={(e) => { setStart(e.target.value); changed(); }} /></label>
        <label><span>End (seconds)</span><input inputMode="numeric" type="number" min={1} max={86430} required value={end} onChange={(e) => { setEnd(e.target.value); changed(); }} /></label>
      </div>
      <button className="secondary-button" type="submit">{pending ? "Checking…" : "Preview clip"}</button>
    </fieldset>
  </form>;
  return (
    <InjuryDetailSheet title="Demonstration video" onClose={onClose} busy={pending} panelClassName={media ? layout.dialog : undefined}>
      <div className={`${styles.sheet} ${layout.content}`} data-preview={Boolean(media)}>
        <div className={layout.settings}>
        <p className="muted">Optional · YouTube clip up to 30 seconds · Only you can see it</p>
        {error ? <p role="alert">{error}</p> : null}
        {/* Personal uploads stay disabled; the upload API remains server-side for later. */}
        {!settings ? (error ? null : <p role="status">Opening video options…</p>)
          : !settings.youtube_available ? <p className="muted">Video links unavailable. Save without a video.</p>
          : media ? <details><summary>Edit clip</summary>{clipForm}</details> : clipForm}
        </div>
        {media ? <div className={layout.preview}><PrivateVideoPlayer key={media.id} media={media} name={name} />
          <button type="button" className="cta" onClick={() => onAttach(media.id)}>Use video</button></div> : null}
      </div>
    </InjuryDetailSheet>
  );
}
