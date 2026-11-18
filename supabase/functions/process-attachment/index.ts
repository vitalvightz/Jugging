// process-attachment: rebuilds one uploaded photo or voice note.
//
// Called only by the UNLXCK API with its service-role key. The caller's key is
// the only credential used, so a non-service caller fails at the first
// database call (the processing functions are executable by service_role only).
// Flow: claim -> download from quarantine -> rebuild -> store in the approved
// bucket -> mark ready -> delete the original. Any failure rejects the upload.
import jpeg from "npm:jpeg-js@0.4.4";
import { cleanPhoto, cleanVoice, type JpegCodec, Rejected } from "./sanitize.ts";

const SUPABASE_URL = (Deno.env.get("SUPABASE_URL") ?? "").replace(/\/$/, "");
const MAX_DOWNLOAD = 8 * 1024 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const codec: JpegCodec = {
  decode: (data, options) => jpeg.decode(data, options),
  encode: (image, quality) => jpeg.encode(image, quality),
};

type Asset = { id: string; owner_id: string; kind: "photo" | "voice"; size_bytes: number; intake_path: string; ready_path: string | null };

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

async function download(key: string, path: string): Promise<Uint8Array> {
  const response = await fetch(objectUrl("attachment-intake", path), { headers: headers(key) });
  if (!response.ok || !response.body) throw new Rejected("Upload missing");
  const declared = Number(response.headers.get("content-length") ?? "0");
  if (declared > MAX_DOWNLOAD) { await response.body.cancel(); throw new Rejected("Upload is too large"); }
  const reader = response.body.getReader();
  const parts: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > MAX_DOWNLOAD) { await reader.cancel(); throw new Rejected("Upload is too large"); }
    parts.push(value);
  }
  const bytes = new Uint8Array(total);
  let at = 0;
  for (const part of parts) { bytes.set(part, at); at += part.length; }
  return bytes;
}

async function upload(key: string, path: string, bytes: Uint8Array, contentType: string) {
  const response = await fetch(objectUrl("attachment-ready", path), {
    method: "POST", body: bytes,
    headers: headers(key, { "Content-Type": contentType, "x-upsert": "true", "Cache-Control": "private, no-store" }),
  });
  if (!response.ok) throw new Error(`store failed (${response.status})`);
}

async function remove(key: string, bucket: string, paths: string[]) {
  if (!paths.length) return;
  await fetch(`${SUPABASE_URL}/storage/v1/object/${bucket}`, {
    method: "DELETE", headers: headers(key, { "Content-Type": "application/json" }), body: JSON.stringify({ prefixes: paths }),
  }).catch(() => undefined);
}

async function cleanup(key: string) {
  const stale = await rpc<Asset[]>(key, "attachment_cleanup_candidates", {});
  await remove(key, "attachment-intake", stale.map(asset => asset.intake_path));
  await remove(key, "attachment-ready", stale.flatMap(asset => asset.ready_path ? [asset.ready_path] : []));
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
      await upload(key, `${base}/clean.jpg`, clean.bytes, "image/jpeg");
      await rpc(key, "finish_attachment", { p_asset_id: asset.id, p_ready_path: `${base}/clean.jpg`,
        p_width: clean.width, p_height: clean.height, p_duration_ms: null });
      return { status: "ready" as const, kind: asset.kind, width: clean.width, height: clean.height };
    }
    const clean = cleanVoice(original, asset.size_bytes);
    await upload(key, `${base}/clean.wav`, clean.bytes, "audio/wav");
    await rpc(key, "finish_attachment", { p_asset_id: asset.id, p_ready_path: `${base}/clean.wav`,
      p_width: null, p_height: null, p_duration_ms: clean.durationMs });
    return { status: "ready" as const, kind: asset.kind, duration_ms: clean.durationMs };
  } catch (error) {
    // Fail closed: anything unexpected rejects the upload; the file never reaches anyone.
    const reason = error instanceof Rejected ? error.message : "Could not process this file";
    await rpc(key, "reject_attachment", { p_asset_id: asset.id, p_reason: reason }).catch(() => undefined);
    return { status: "rejected" as const, reason };
  } finally {
    await remove(key, "attachment-intake", [asset.intake_path]);
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
  try { assetId = String((await request.json())?.asset_id ?? ""); } catch { /* handled below */ }
  if (!UUID.test(assetId)) return json({ error: "asset_id required" }, 400);
  try {
    const result = await process(key, assetId);
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
