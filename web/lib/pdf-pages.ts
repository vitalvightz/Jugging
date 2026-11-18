/**
 * Turns a PDF chosen on this device into plain page images. The original file
 * never leaves the browser: each page is drawn to a canvas (scripts, forms,
 * links and attachments are never run or kept) and saved as a fresh JPEG. The
 * server rebuilds every page again from pixels and writes a new PDF, so this
 * is a convenience and size limit, never the security boundary.
 */
// Match the server limits (supabase/functions/process-attachment/sanitize.ts).
export const PDF_MAX_BYTES = 5 * 1024 * 1024;
export const PDF_MAX_PAGES = 30;
export const PAGE_MAX_SIDE = 1600;
export const PAGE_MAX_PIXELS = 2_000_000;
export const PAGE_MAX_BYTES = 1024 * 1024;
export const DOCUMENT_MAX_BYTES = 8 * 1024 * 1024;
const QUALITIES = [0.8, 0.7, 0.6, 0.5];

export function pdfProblem(file: { name: string; type: string; size: number }): string | null {
  const looksLikePdf = file.type === "application/pdf" || (!file.type && /\.pdf$/i.test(file.name));
  if (!looksLikePdf) return "Choose a PDF file.";
  if (file.size > PDF_MAX_BYTES) return "That PDF is over 5 MB.";
  return null;
}

/** Page size in pixels: long side at most 1600, area at most 2 megapixels, never upscaled past 2x. */
export function pageSize(width: number, height: number) {
  const scale = Math.min(2, PAGE_MAX_SIDE / Math.max(width, height), Math.sqrt(PAGE_MAX_PIXELS / (width * height)));
  return { width: Math.max(1, Math.floor(width * scale)), height: Math.max(1, Math.floor(height * scale)), scale };
}

/** Bytes each page may use so the whole document fits in 8 MB (with room for the PDF wrapper). */
export function pageBudget(pageCount: number) {
  return Math.min(PAGE_MAX_BYTES, Math.floor((DOCUMENT_MAX_BYTES - 64 * 1024) / Math.max(1, pageCount)));
}

export class PdfProblem extends Error {}

export type RenderedPdf = { pages: Blob[]; totalBytes: number };

export async function renderPdfPages(file: Blob, onPage: (page: number, total: number) => void): Promise<RenderedPdf> {
  const data = new Uint8Array(await file.arrayBuffer());
  const head = new TextDecoder("latin1").decode(data.subarray(0, 1024));
  if (!head.includes("%PDF-")) throw new PdfProblem("That file isn't a PDF.");
  const pdfjs = await import("pdfjs-dist");
  if (!pdfjs.GlobalWorkerOptions.workerPort) {
    pdfjs.GlobalWorkerOptions.workerPort = new Worker(new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url), { type: "module" });
  }
  const task = pdfjs.getDocument({
    // pdf.js 6 never evaluates code from a file; WebAssembly decoders stay off (CSP blocks them anyway).
    data, useWasm: false, enableXfa: false, disableAutoFetch: true, disableStream: true, stopAtErrors: true,
  });
  try {
    let doc;
    try {
      doc = await task.promise;
    } catch (error) {
      if (error instanceof Error && error.name === "PasswordException") throw new PdfProblem("That PDF is password-protected. Remove the password and try again.");
      throw new PdfProblem("That PDF can't be opened. Try saving it again as a PDF.");
    }
    if (doc.numPages > PDF_MAX_PAGES) throw new PdfProblem(`That PDF has ${doc.numPages} pages. The limit is ${PDF_MAX_PAGES}.`);
    const budget = pageBudget(doc.numPages);
    const pages: Blob[] = [];
    let totalBytes = 0;
    for (let number = 1; number <= doc.numPages; number += 1) {
      onPage(number, doc.numPages);
      const page = await doc.getPage(number);
      const base = page.getViewport({ scale: 1 });
      let { scale } = pageSize(base.width, base.height);
      let blob: Blob | null = null;
      // Lower the quality, then the size, until the page fits its share of 8 MB.
      for (let attempt = 0; attempt < 3 && !blob; attempt += 1) {
        const viewport = page.getViewport({ scale });
        const canvas = document.createElement("canvas");
        canvas.width = Math.floor(viewport.width);
        canvas.height = Math.floor(viewport.height);
        const context = canvas.getContext("2d");
        if (!context) throw new PdfProblem("That PDF can't be opened here.");
        context.fillStyle = "#fff";
        context.fillRect(0, 0, canvas.width, canvas.height);
        // Forms and annotations are not drawn: only the page itself.
        await page.render({ canvas, canvasContext: context, viewport, annotationMode: pdfjs.AnnotationMode.DISABLE }).promise;
        for (const quality of QUALITIES) {
          const candidate = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, "image/jpeg", quality));
          if (candidate && candidate.size <= budget) { blob = candidate; break; }
        }
        canvas.width = 0;
        canvas.height = 0;
        scale *= 0.75;
      }
      page.cleanup();
      if (!blob) throw new PdfProblem(`Page ${number} is too detailed to attach. Try a smaller PDF.`);
      pages.push(blob);
      totalBytes += blob.size;
    }
    return { pages, totalBytes };
  } finally {
    await task.destroy();
  }
}

export function formatBytes(bytes: number | null | undefined) {
  if (!bytes) return "";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
