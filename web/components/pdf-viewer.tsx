"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTopModal } from "@/lib/use-top-modal";
import styles from "./photo-viewer.module.css";
import own from "./pdf-viewer.module.css";

/**
 * Reads a PDF inside the app. Every PDF here is a file the server wrote from
 * rebuilt page images (no text, scripts, forms or links), and pdf.js runs
 * with scripting and WebAssembly off, drawing each page to a canvas only as
 * it scrolls into view. Download stays one tap away.
 */
export function PdfViewer({ name, pages, load, onDownload, onClose }: {
  name: string;
  pages: number;
  load: () => Promise<string>;
  onDownload: () => void;
  onClose: () => void;
}) {
  const [doc, setDoc] = useState<import("pdfjs-dist").PDFDocumentProxy | null>(null);
  const [failed, setFailed] = useState(false);
  const [current, setCurrent] = useState(1);
  const closeButton = useRef<HTMLButtonElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);

  useTopModal(dialog);
  useEffect(() => {
    closeButton.current?.focus();
    function keys(event: KeyboardEvent) { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); } }
    window.addEventListener("keydown", keys, true);
    return () => window.removeEventListener("keydown", keys, true);
  }, [onClose]);

  useEffect(() => {
    let active = true;
    let task: { destroy(): Promise<void> } | null = null;
    (async () => {
      const url = await load();
      const response = await fetch(url, { cache: "no-store", referrerPolicy: "no-referrer" });
      if (!response.ok) throw new Error("unavailable");
      const data = new Uint8Array(await response.arrayBuffer());
      const pdfjs = await import("pdfjs-dist");
      if (!pdfjs.GlobalWorkerOptions.workerPort) {
        pdfjs.GlobalWorkerOptions.workerPort = new Worker(new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url), { type: "module" });
      }
      // pdf.js 6 never evaluates code from a file; WebAssembly decoders and XFA forms stay off.
      const loading = pdfjs.getDocument({ data, useWasm: false, enableXfa: false, stopAtErrors: true });
      task = loading;
      const opened = await loading.promise;
      if (active) setDoc(opened); else void loading.destroy();
    })().catch(() => { if (active) setFailed(true); });
    return () => { active = false; void task?.destroy(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const total = doc?.numPages ?? pages;
  return createPortal(<dialog ref={dialog} className={styles.viewer} aria-label={name}
    onCancel={event => { event.preventDefault(); onClose(); }}>
    <div className={styles.top}>
      <span className={styles.counter} title={name}><strong className={own.name}>{name}</strong>
        <span className={own.pages}>{doc ? `Page ${current} of ${total}` : `${total} ${total === 1 ? "page" : "pages"}`}</span></span>
      <button type="button" className={styles.action} onClick={onDownload}>Download</button>
      <button ref={closeButton} type="button" className={styles.close} onClick={onClose} aria-label="Close PDF">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>
    <div ref={scroller} className={own.scroller} role="region" aria-label="PDF pages" tabIndex={0} onScroll={event => {
      const box = event.currentTarget;
      const children = [...box.querySelectorAll<HTMLElement>("[data-page]")];
      const middle = box.scrollTop + box.clientHeight / 3;
      const visible = children.findLast(child => child.offsetTop <= middle);
      if (visible) setCurrent(Number(visible.dataset.page));
    }}>
      {failed ? <p className={own.message} role="alert">This PDF couldn&apos;t be opened here. Try Download.</p>
        : !doc ? <p className={own.message} role="status">Opening PDF…</p>
        : Array.from({ length: doc.numPages }, (_, index) => <PdfPage key={index} doc={doc} number={index + 1} root={scroller} />)}
    </div>
  </dialog>, document.body);
}

function PdfPage({ doc, number, root }: {
  doc: import("pdfjs-dist").PDFDocumentProxy; number: number; root: React.RefObject<HTMLDivElement | null>;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [ratio, setRatio] = useState(1.414);
  const [drawn, setDrawn] = useState(false);

  useEffect(() => {
    let active = true;
    void doc.getPage(number).then(page => {
      const view = page.getViewport({ scale: 1 });
      if (active) setRatio(view.height / view.width);
    });
    return () => { active = false; };
  }, [doc, number]);

  useEffect(() => {
    const element = holder.current;
    if (!element || drawn) return;
    let cancelled = false;
    const observer = new IntersectionObserver(async entries => {
      if (!entries.some(entry => entry.isIntersecting) || cancelled) return;
      observer.disconnect();
      const page = await doc.getPage(number);
      const base = page.getViewport({ scale: 1 });
      const width = element.clientWidth * Math.min(2, window.devicePixelRatio || 1);
      const viewport = page.getViewport({ scale: width / base.width });
      const target = canvas.current;
      if (!target || cancelled) return;
      target.width = Math.floor(viewport.width);
      target.height = Math.floor(viewport.height);
      const context = target.getContext("2d");
      if (!context) return;
      const pdfjs = await import("pdfjs-dist");
      await page.render({ canvas: target, canvasContext: context, viewport, annotationMode: pdfjs.AnnotationMode.DISABLE }).promise;
      if (!cancelled) setDrawn(true);
    }, { root: root.current, rootMargin: "400px 0px" });
    observer.observe(element);
    return () => { cancelled = true; observer.disconnect(); };
  }, [doc, number, root, drawn]);

  return <div ref={holder} className={own.page} data-page={number} style={{ aspectRatio: `1 / ${ratio}` }}
    data-drawn={drawn || undefined}>
    <canvas ref={canvas} className={own.canvas} role="img" aria-label={`Page ${number}`} />
  </div>;
}
