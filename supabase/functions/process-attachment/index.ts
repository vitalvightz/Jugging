// process-attachment: rebuilds one uploaded photo or voice note, or one page
// of a PDF, or assembles a PDF's rebuilt pages into a brand-new document.
//
// Called only by the UNLXCK API with its service-role key. The caller's key is
// the only credential used, so a non-service caller fails at the first
// database call (the processing functions are executable by service_role only).
// Flow: claim -> download from quarantine -> rebuild -> store in the approved
// bucket -> mark ready -> delete the original. Any failure rejects the upload.
import jpeg from "npm:jpeg-js@0.4.4";
import { cleanPage, cleanPhoto, cleanVoice, imagePdf, type JpegCodec, LIMITS, Rejected } from "./sanitize.ts";

const SUPABASE_URL = (Deno.env.get("SUPABASE_URL") ?? "").replace(/\/$/, "");
const MAX_DOWNLOAD = 8 * 1024 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const codec: JpegCodec = {
  decode: (data, options) => jpeg.decode(data, options),
  encode: (image, quality) => jpeg.encode(image, quality),
};

type Asset = { id: string; owner_id: string; kind: "photo" | "voice" | "pdf"; size_bytes: number; intake_path: string;
  ready_path: string | null; status: string; processing_token: string; page_count: number | null };
type PageClaim = { id: string; owner_id: string; intake_path: string; page: number; page_token: string };
type PageRow = { page: number; status: string; width: number; height: number };
const pageName = (page: number) => `${String(page).padStart(3, "0")}.jpg`;
const pageNames = (count: number) => Array.from({ length: count }, (_, index) => pageName(index + 1));

class Unauthorized extends Error {}

function headers(key: string, extra: Record<string, string> = {}) {
  // Legacy service keys are JWTs; new secret keys are sent as apikey only.
  const auth: Record<string, string> = key.split(".").length === 3 ? { Authorization: `Bearer ${key}` } : {};
  return { apikey: key, ...auth, ...extra };
}

async function rpc<T>(key: string, name: string, args: Record<string, unknown>): Promise<T> {
  const response = await fetch(`${SUPABASE_URL}/rest/v1/rpc/${name}`, {
    method: "POST", headers: headers(key, { "Content-Type": "application/json" }), body: JSON.stringify(args),
  });
  if (response.status === 401 || response.status === 403) throw new Unauthorized();
  const body = await response.text();
  if (!response.ok) {
    if (body.includes("42501") || body.includes("permission denied")) throw new Unauthorized();
    throw new Error(`${name} failed (${response.status})`);
  }
  return (body ? JSON.parse(body) : null) as T;
}

const objectUrl = (bucket: string, path: string) =>
  `${SUPABASE_URL}/storage/v1/object/${bucket}/${path.split("/").map(encodeURIComponent).join("/")}`;

async function download(key: string, path: string, bucket = "attachment-intake", limit = MAX_DOWNLOAD): Promise<Uint8Array> {
  const response = await fetch(objectUrl(bucket, path), { headers: headers(key) });
  if (!response.ok || !response.body) throw new Rejected("Upload missing");
  const declared = Number(response.headers.get("content-length") ?? "0");
  if (declared > limit) { await response.body.cancel(); throw new Rejected("Upload is too large"); }
  const reader = response.body.getReader();
  const parts: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > limit) { await reader.cancel(); throw new Rejected("Upload is too large"); }
    parts.push(value);
  }
  const bytes = new Uint8Array(total);
  let at = 0;
  for (const part of parts) { bytes.set(part, at); at += part.length; }
  return bytes;
}

async function upload(key: string, path: string, bytes: Uint8Array, contentType: string) {
  const response = await fetch(objectUrl("attachment-ready", path), {
    method: "POST", body: new Uint8Array(bytes).buffer,
    headers: headers(key, { "Content-Type": contentType, "x-upsert": "false", "Cache-Control": "private, no-store" }),
  });
  if (!response.ok) throw new Error(`store failed (${response.status})`);
}

async function remove(key: string, bucket: string, paths: string[]) {
  if (!paths.length) return;
  const response = await fetch(`${SUPABASE_URL}/storage/v1/object/${bucket}`, {
    method: "DELETE", headers: headers(key, { "Content-Type": "application/json" }), body: JSON.stringify({ prefixes: paths }),
  });
  if (!response.ok) throw new Error(`remove failed (${response.status})`);
}

async function cleanup(key: string) {
  const stale = await rpc<Asset[]>(key, "attachment_cleanup_candidates", {});
  for (const asset of stale) {
    try {
      if (asset.kind === "pdf") {
        // A document's quarantined pages and its rebuilt pages are never kept.
        const names = pageNames(asset.page_count ?? LIMITS.documentPages);
        await remove(key, "attachment-intake", names.map(name => `${asset.intake_path}/${name}`));
        await remove(key, "attachment-ready", names.map(name => `${asset.owner_id}/${asset.id}/pages/${name}`));
      } else {
        await remove(key, "attachment-intake", [asset.intake_path]);
      }
      if (asset.status === "rejected" && asset.ready_path) {
        await remove(key, "attachment-ready", [asset.ready_path]);
      }
      if (asset.status === "rejected" && asset.kind === "photo") {
        // A photo's thumbnail is written before it is ready, so remove it even if it never finished.
        await remove(key, "attachment-ready", [`${asset.owner_id}/${asset.id}/thumb.jpg`]);
      }
      await rpc(key, "ack_attachment_cleanup", { p_asset_id: asset.id, p_all_removed: asset.status === "rejected" });
    } catch { console.error("Attachment cleanup will retry", asset.id); }
  }
}

async function process(key: string, assetId: string) {
  const claimed = await rpc<Asset[]>(key, "claim_attachment_processing", { p_asset_id: assetId });
  const asset = claimed[0];
  if (!asset) return { status: "unavailable" as const };
  try {
    const original = await download(key, asset.intake_path);
    const base = `${asset.owner_id}/${asset.id}`;
    if (asset.kind === "photo") {
      const clean = cleanPhoto(original, codec, asset.size_bytes);
      // The thumbnail goes first: a photo is never ready without its preview.
      await upload(key, `${base}/thumb.jpg`, clean.thumb.bytes, "image/jpeg");
      await upload(key, `${base}/clean.jpg`, clean.bytes, "image/jpeg");
      await rpc(key, "finish_attachment", { p_asset_id: asset.id, p_token: asset.processing_token, p_ready_path: `${base}/clean.jpg`,
        p_width: clean.width, p_height: clean.height, p_duration_ms: null });
      return { status: "ready" as const, kind: asset.kind, width: clean.width, height: clean.height };
    }
    const clean = cleanVoice(original, asset.size_bytes);
    await upload(key, `${base}/clean.wav`, clean.bytes, "audio/wav");
    await rpc(key, "finish_attachment", { p_asset_id: asset.id, p_token: asset.processing_token, p_ready_path: `${base}/clean.wav`,
      p_width: null, p_height: null, p_duration_ms: clean.durationMs });
    return { status: "ready" as const, kind: asset.kind, duration_ms: clean.durationMs };
  } catch (error) {
    // Fail closed: anything unexpected rejects the upload; the file never reaches anyone.
    const reason = error instanceof Rejected ? error.message : "Could not process this file";
    await rpc(key, "reject_attachment", { p_asset_id: asset.id, p_token: asset.processing_token, p_reason: reason }).catch(() => undefined);
    return { status: "rejected" as const, reason };
  } finally {
    // A failed immediate removal remains discoverable by cleanup.
    await remove(key, "attachment-intake", [asset.intake_path]).catch(() => undefined);
  }
}

// One page of a PDF: quarantine -> rebuild -> approved bucket (service-only).
async function processPage(key: string, assetId: string, page: number) {
  const claimed = await rpc<PageClaim[]>(key, "claim_attachment_page", { p_asset_id: assetId, p_page: page });
  const claim = claimed[0];
  if (!claim) return { status: "unavailable" as const };
  const source = `${claim.intake_path}/${pageName(page)}`;
  try {
    const original = await download(key, source, "attachment-intake", LIMITS.pageBytes);
    const clean = cleanPage(original, codec);
    await upload(key, `${claim.owner_id}/${claim.id}/pages/${pageName(page)}`, clean.bytes, "image/jpeg");
    await rpc(key, "finish_attachment_page", { p_asset_id: claim.id, p_page: page, p_token: claim.page_token,
      p_width: clean.width, p_height: clean.height });
    return { status: "page_ready" as const, page };
  } catch (error) {
    // One bad page rejects the whole document; no part of it is ever released.
    const reason = error instanceof Rejected ? `Page ${page}: ${error.message}` : "Could not process this file";
    await rpc(key, "reject_attachment_page", { p_asset_id: claim.id, p_page: page, p_token: claim.page_token, p_reason: reason })
      .catch(() => undefined);
    return { status: "rejected" as const, reason };
  } finally {
    await remove(key, "attachment-intake", [source]).catch(() => undefined);
  }
}

// Every page rebuilt: write a new image-only PDF from the rebuilt pages.
async function assemble(key: string, assetId: string) {
  const claimed = await rpc<Asset[]>(key, "claim_attachment_assembly", { p_asset_id: assetId });
  const asset = claimed[0];
  if (!asset || !asset.ready_path) return { status: "unavailable" as const };
  const base = `${asset.owner_id}/${asset.id}/pages`;
  try {
    const rows = await rpc<PageRow[]>(key, "attachment_document_pages", { p_asset_id: asset.id });
    if (!rows.length || rows.length !== asset.page_count || rows.some(row => row.status !== "ready")) throw new Rejected("Pages missing");
    const pages = [];
    let total = 0;
    for (const row of rows) {
      const bytes = await download(key, `${base}/${pageName(row.page)}`, "attachment-ready", LIMITS.pageBytes);
      total += bytes.length;
      if (total > LIMITS.documentBytes) throw new Rejected("Document is too large");
      pages.push({ bytes, width: row.width, height: row.height });
    }
    const pdf = imagePdf(pages);
    await upload(key, asset.ready_path, pdf, "application/pdf");
    await rpc(key, "finish_attachment", { p_asset_id: asset.id, p_token: asset.processing_token, p_ready_path: asset.ready_path,
      p_width: null, p_height: null, p_duration_ms: null });
    return { status: "ready" as const, kind: "pdf" as const, pages: pages.length, size_bytes: pdf.length };
  } catch (error) {
    const reason = error instanceof Rejected ? error.message : "Could not process this file";
    await rpc(key, "reject_attachment", { p_asset_id: asset.id, p_token: asset.processing_token, p_reason: reason }).catch(() => undefined);
    return { status: "rejected" as const, reason };
  } finally {
    // Rebuilt page images are only an intermediate step.
    await remove(key, "attachment-ready", pageNames(asset.page_count ?? 0).map(name => `${base}/${name}`)).catch(() => undefined);
  }
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
});

Deno.serve(async request => {
  if (request.method !== "POST") return json({ error: "Method not allowed" }, 405);
  const key = (request.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "").trim()
    || (request.headers.get("apikey") ?? "").trim();
  if (!key || !SUPABASE_URL) return json({ error: "Unauthorized" }, 401);
  let assetId = "";
  let page: unknown = undefined;
  let assembleDocument = false;
  try {
    const body = await request.json();
    assetId = String(body?.asset_id ?? "");
    page = body?.page;
    assembleDocument = body?.assemble === true;
  } catch { /* handled below */ }
  if (!UUID.test(assetId)) return json({ error: "asset_id required" }, 400);
  if (page !== undefined && (!Number.isInteger(page) || (page as number) < 1 || (page as number) > LIMITS.documentPages)) {
    return json({ error: "page must be 1 to 30" }, 400);
  }
  try {
    const result = assembleDocument ? await assemble(key, assetId)
      : page !== undefined ? await processPage(key, assetId, page as number) : await process(key, assetId);
    // Opportunistic cleanup after the response is ready; errors here never affect the upload.
    const pending = cleanup(key).catch(() => undefined);
    // deno-lint-ignore no-explicit-any
    (globalThis as any).EdgeRuntime?.waitUntil?.(pending);
    return json(result, result.status === "unavailable" ? 409 : 200);
  } catch (error) {
    if (error instanceof Unauthorized) return json({ error: "Unauthorized" }, 401);
    return json({ error: "Processing unavailable" }, 503);
  }
});
