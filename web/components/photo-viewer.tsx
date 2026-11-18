"use client";

import { type TouchEvent, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { TrainingAttachment } from "@/lib/api-schema.generated";
import { useTopModal } from "@/lib/use-top-modal";
import styles from "./photo-viewer.module.css";

/**
 * Full-screen photos: the preview shows at once and the full photo fades in
 * over it; swipe or use the arrows (and arrow keys) to move between photos;
 * Escape, the close button or tapping outside the photo closes it. Each full
 * photo is fetched through its own short-lived link only when shown.
 */
export function PhotoViewer({ photos, startId, label, load, onRemove, onClose }: {
  photos: TrainingAttachment[];
  startId: string;
  label: string;
  load: (photo: TrainingAttachment) => Promise<string>;
  onRemove?: (photo: TrainingAttachment) => Promise<void> | void;
  onClose: () => void;
}) {
  const [index, setIndex] = useState(() => Math.max(0, photos.findIndex(photo => photo.id === startId)));
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [failed, setFailed] = useState<string | null>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const swipe = useRef<number | null>(null);
  const photo = photos[Math.min(index, photos.length - 1)];
  const count = photos.length;

  useTopModal(dialog);
  useEffect(() => { closeButton.current?.focus(); }, []);

  useEffect(() => {
    if (!photo || urls[photo.id]) return;
    let active = true;
    load(photo).then(url => { if (active) setUrls(current => ({ ...current, [photo.id]: url })); })
      .catch(() => { if (active) setFailed(photo.id); });
    return () => { active = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [photo?.id]);

  useEffect(() => {
    function keys(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); }
      if (event.key === "ArrowRight") setIndex(value => Math.min(count - 1, value + 1));
      if (event.key === "ArrowLeft") setIndex(value => Math.max(0, value - 1));
    }
    window.addEventListener("keydown", keys, true);
    return () => window.removeEventListener("keydown", keys, true);
  }, [count, onClose]);

  useEffect(() => { if (!count) onClose(); }, [count, onClose]);
  if (!photo) return null;

  function touchStart(event: TouchEvent) { swipe.current = event.touches[0]?.clientX ?? null; }
  function touchEnd(event: TouchEvent) {
    const start = swipe.current;
    swipe.current = null;
    const end = event.changedTouches[0]?.clientX;
    if (start == null || end == null || Math.abs(end - start) < 50) return;
    setIndex(value => end < start ? Math.min(count - 1, value + 1) : Math.max(0, value - 1));
  }

  const name = `Photo ${index + 1} of ${count}`;
  return createPortal(<dialog ref={dialog} className={styles.viewer} aria-label={name}
    onCancel={event => { event.preventDefault(); onClose(); }} onClick={event => { if (event.target === event.currentTarget) onClose(); }} onTouchStart={touchStart} onTouchEnd={touchEnd}>
    <div className={styles.top}>
      <span className={styles.counter}>{count > 1 ? `${index + 1} / ${count}` : label}</span>
      {onRemove && photo.removable ? <button type="button" className={styles.action} onClick={() => void onRemove(photo)}>
        Remove photo</button> : null}
      <button ref={closeButton} type="button" className={styles.close} onClick={onClose} aria-label="Close photo">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>
    <div className={styles.stage} onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
      <PhotoFrame key={photo.id} file={photo.file} url={urls[photo.id] ?? null} alt={`Photo ${index + 1} for ${label}`}
        failed={failed === photo.id} />
    </div>
    {count > 1 ? <>
      <button type="button" className={`${styles.nav} ${styles.prev}`} disabled={index === 0}
        onClick={() => setIndex(value => Math.max(0, value - 1))} aria-label="Previous photo">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7" /></svg>
      </button>
      <button type="button" className={`${styles.nav} ${styles.next}`} disabled={index === count - 1}
        onClick={() => setIndex(value => Math.min(count - 1, value + 1))} aria-label="Next photo">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7" /></svg>
      </button>
    </> : null}
    <p className={styles.note}>Only you and coaches you share photos with can see this.</p>
  </dialog>, document.body);
}

/**
 * One photo at its own shape: the preview first (softened), then the full
 * photo fading in. It never grows past the screen or past its real size.
 */
export function PhotoFrame({ file, url, alt, failed = false }: {
  file: TrainingAttachment["file"]; url: string | null; alt: string; failed?: boolean;
}) {
  const [loaded, setLoaded] = useState(false);
  const width = file?.width || 4;
  const height = file?.height || 3;
  return <div className={styles.frame} data-loaded={loaded || undefined}
    style={{ aspectRatio: `${width} / ${height}`, ["--ratio" as string]: width / height, ["--natural" as string]: `${width}px` }}>
    {/* Plain <img>s: next/image would route the private, short-lived links through the image optimizer. */}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    {file?.thumb_url ? <img className={styles.preview} src={file.thumb_url} alt="" aria-hidden="true"
      referrerPolicy="no-referrer" draggable={false} /> : null}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    {url ? <img className={styles.full} src={url} alt={alt} decoding="async" referrerPolicy="no-referrer"
      draggable={false} onLoad={() => setLoaded(true)} /> : null}
    {failed ? <span className={styles.status} role="alert">Photo unavailable. Close and try again.</span>
      : !loaded ? <span className={styles.status} role="status">Loading photo…</span> : null}
  </div>;
}
