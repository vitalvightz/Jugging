/**
 * Plain text from a plan file, read entirely in the browser. The file itself
 * is never uploaded: only the text leaves the device, and only when the person
 * asks for the plan to be imported.
 *
 * PDF: pdf.js with scripting, WebAssembly and XFA off, text only (nothing is
 * drawn). Word (.docx): the file is a zip; its one document part is inflated
 * with the browser's own DecompressionStream and read as XML for paragraphs
 * and table rows. Macros, embedded objects, images and links are never read.
 */

export const PLAN_FILE_MAX_BYTES = 10 * 1024 * 1024;
const MAX_PDF_PAGES = 40;
// A Word document's text part is rarely over 2 MB; anything far larger is a zip bomb.
const MAX_XML_BYTES = 12 * 1024 * 1024;
export const PLAN_TEXT_MAX_CHARS = 30_000;

export type PlanFileKind = "pdf" | "docx" | "image";
export const MAX_SCREENSHOTS = 4;
// Tall phone screenshots stay legible at 2000px; over ~1.4 MB they're redrawn smaller.
const SCREENSHOT_MAX_SIDE = 2000;
const SCREENSHOT_MAX_BYTES = 1_400_000;

export class PlanFileError extends Error {}

export function planFileKind(file: { name: string; type: string }): PlanFileKind | null {
  const name = file.name.toLowerCase();
  if (file.type === "application/pdf" || name.endsWith(".pdf")) return "pdf";
  if (file.type === "application/vnd.openxmlformats-officedocument.wordprocessingml.document" || name.endsWith(".docx")) return "docx";
  if (/^image\/(jpeg|png|webp|heic|heif)$/.test(file.type) || /\.(jpe?g|png|webp|heic|heif)$/.test(name)) return "image";
  return null;
}

/**
 * A screenshot redrawn as a fresh JPEG (no metadata, nothing but pixels), as a
 * data URL. It goes to the plan reader once and is never stored.
 */
export async function screenshotData(file: File): Promise<string> {
  if (file.size > 25 * 1024 * 1024) throw new PlanFileError("That screenshot is over 25 MB.");
  const { preparePhoto } = await import("./attachments");
  let blob: Blob | null = null;
  for (const [maxSide, quality] of [[SCREENSHOT_MAX_SIDE, 0.85], [1600, 0.75], [1280, 0.7]] as const) {
    try { blob = await preparePhoto(file, { maxSide, quality }); } catch { throw new PlanFileError("That screenshot can't be opened here. Try a PNG or JPG."); }
    if (blob.size <= SCREENSHOT_MAX_BYTES) break;
  }
  if (!blob || blob.size > SCREENSHOT_MAX_BYTES) throw new PlanFileError("That screenshot is too large. Try cropping it.");
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return `data:image/jpeg;base64,${btoa(binary)}`;
}

export async function extractPlanText(file: File): Promise<string> {
  const kind = planFileKind(file);
  if (kind === "image") throw new PlanFileError("Screenshots are read separately.");
  if (!kind) {
    throw new PlanFileError(file.name.toLowerCase().endsWith(".doc")
      ? "Older .doc files can't be read. Save it as .docx or PDF first."
      : "Choose a PDF, Word (.docx) file or screenshots.");
  }
  if (file.size > PLAN_FILE_MAX_BYTES) throw new PlanFileError("That file is over 10 MB.");
  const bytes = new Uint8Array(await file.arrayBuffer());
  const text = kind === "pdf" ? await pdfText(bytes) : await docxText(bytes);
  const tidy = tidyText(text);
  if (tidy.replace(/\s/g, "").length < 20) {
    throw new PlanFileError(kind === "pdf"
      ? "This PDF has no readable text (it may be a scan or photo). Export it from Word or Google Docs and try again."
      : "This document has no readable text.");
  }
  return tidy.slice(0, PLAN_TEXT_MAX_CHARS);
}

export function tidyText(text: string) {
  return text.replace(/\r/g, "").split("\n").map(line => line.replace(/[ \t ]+/g, " ").trim())
    .join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

// -- PDF ----------------------------------------------------------------------

async function pdfText(bytes: Uint8Array) {
  if (String.fromCharCode(...bytes.slice(0, 1024)).indexOf("%PDF-") < 0) throw new PlanFileError("That file isn't a PDF.");
  const pdfjs = await import("pdfjs-dist");
  if (typeof window !== "undefined" && !pdfjs.GlobalWorkerOptions.workerPort) {
    pdfjs.GlobalWorkerOptions.workerPort = new Worker(new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url), { type: "module" });
  }
  const task = pdfjs.getDocument({ data: bytes, useWasm: false, enableXfa: false, stopAtErrors: true });
  let doc;
  try { doc = await task.promise; } catch { throw new PlanFileError("That PDF couldn't be opened. It may be damaged or password protected."); }
  try {
    const lines: string[] = [];
    for (let number = 1; number <= Math.min(doc.numPages, MAX_PDF_PAGES); number++) {
      const page = await doc.getPage(number);
      const content = await page.getTextContent();
      let line = "", lastY: number | null = null;
      for (const item of content.items) {
        if (!("str" in item)) continue;
        const y = item.transform[5];
        if (lastY !== null && Math.abs(y - lastY) > 2 && line) { lines.push(line); line = ""; }
        // Table cells on one line are kept apart, so "Squat 3x5" stays readable.
        line += line && item.str && !line.endsWith(" ") && !item.str.startsWith(" ") ? "  " + item.str : item.str;
        lastY = y;
        if (item.hasEOL) { lines.push(line); line = ""; lastY = null; }
      }
      if (line) lines.push(line);
      lines.push("");
      page.cleanup();
    }
    return lines.join("\n");
  } finally {
    void task.destroy();
  }
}

// -- Word (.docx) ---------------------------------------------------------------

async function docxText(bytes: Uint8Array) {
  const xml = await zipEntry(bytes, "word/document.xml");
  if (!xml) throw new PlanFileError("That Word file couldn't be read. Save it again as .docx or PDF.");
  return documentXmlText(new TextDecoder("utf-8").decode(xml));
}

/** Paragraphs as lines and table rows as cells joined by " | ". */
export function documentXmlText(xml: string) {
  const doc = new DOMParser().parseFromString(xml, "application/xml");
  if (doc.getElementsByTagName("parsererror").length) throw new PlanFileError("That Word file couldn't be read.");
  const W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
  const body = doc.getElementsByTagNameNS(W, "body")[0];
  if (!body) return "";
  const out: string[] = [];
  const paragraph = (node: Element) => {
    let text = "";
    const walk = (element: Element) => {
      for (const child of Array.from(element.children)) {
        if (child.namespaceURI !== W) continue;
        if (child.localName === "t") text += child.textContent ?? "";
        else if (child.localName === "tab") text += "\t";
        else if (child.localName === "br" || child.localName === "cr") text += "\n";
        else if (child.localName === "del" || child.localName === "instrText") continue;
        else walk(child);
      }
    };
    walk(node);
    return text;
  };
  const block = (element: Element) => {
    for (const child of Array.from(element.children)) {
      if (child.namespaceURI !== W) continue;
      if (child.localName === "p") out.push(paragraph(child));
      else if (child.localName === "tbl") {
        for (const row of Array.from(child.children).filter(node => node.localName === "tr")) {
          const cells = Array.from(row.children).filter(node => node.localName === "tc")
            .map(cell => Array.from(cell.children).filter(node => node.localName === "p").map(paragraph).join(" ").trim());
          out.push(cells.filter(Boolean).join(" | "));
        }
        out.push("");
      } else if (child.localName === "sdt" || child.localName === "sdtContent" || child.localName === "customXml") block(child);
    }
  };
  block(body);
  return out.join("\n");
}

/** One file out of a zip, or null. Only stored and deflated entries are read. */
export async function zipEntry(bytes: Uint8Array, wanted: string): Promise<Uint8Array | null> {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65_557); i--) {
    if (view.getUint32(i, true) === 0x06054b50) { end = i; break; }
  }
  if (end < 0) return null;
  const count = view.getUint16(end + 10, true);
  let at = view.getUint32(end + 16, true);
  for (let n = 0; n < count && at + 46 <= bytes.length; n++) {
    if (view.getUint32(at, true) !== 0x02014b50) return null;
    const method = view.getUint16(at + 10, true);
    const compressed = view.getUint32(at + 20, true);
    const nameLength = view.getUint16(at + 28, true);
    const extra = view.getUint16(at + 30, true), comment = view.getUint16(at + 32, true);
    const local = view.getUint32(at + 42, true);
    const name = new TextDecoder().decode(bytes.subarray(at + 46, at + 46 + nameLength));
    at += 46 + nameLength + extra + comment;
    if (name !== wanted) continue;
    if (local + 30 > bytes.length || view.getUint32(local, true) !== 0x04034b50) return null;
    const start = local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
    const data = bytes.subarray(start, start + compressed);
    if (data.length !== compressed) return null;
    if (method === 0) return data.length <= MAX_XML_BYTES ? data : null;
    if (method !== 8) return null;
    return inflate(data);
  }
  return null;
}

async function inflate(data: Uint8Array) {
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  const reader = stream.getReader();
  const parts: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.length;
      if (total > MAX_XML_BYTES) { void reader.cancel(); throw new PlanFileError("That Word file is too large to read."); }
      parts.push(value);
    }
  } catch (error) {
    if (error instanceof PlanFileError) throw error;
    return null;
  }
  const out = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) { out.set(part, offset); offset += part.length; }
  return out;
}
