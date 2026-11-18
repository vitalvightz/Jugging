#!/usr/bin/env node
// Checks the deployed photo/voice pipeline end to end, against a real project.
// Run before switching ATTACHMENT_PHOTOS_ENABLED / ATTACHMENT_VOICE_ENABLED on:
//
//   SUPABASE_URL=https://<ref>.supabase.co SUPABASE_SERVICE_ROLE_KEY=... \
//     node scripts/verify-attachment-pipeline.mjs [owner-profile-uuid]
//
// It uploads test files for that profile (default: the first admin), checks each one is
// rebuilt or rejected, then removes everything it created. Nothing is attached
// to any record and nothing is shown to anyone.
import jpeg from "jpeg-js";

let [ownerId] = process.argv.slice(2);
const url = (process.env.SUPABASE_URL ?? "").replace(/\/$/, "");
const key = process.env.SUPABASE_SERVICE_ROLE_KEY ?? "";
if (!url || !key || (ownerId && !/^[0-9a-f-]{36}$/.test(ownerId))) {
  console.error("Usage: SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... node scripts/verify-attachment-pipeline.mjs [owner-uuid]");
  process.exit(2);
}
const auth = { apikey: key, ...(key.split(".").length === 3 ? { Authorization: `Bearer ${key}` } : {}) };
if (!ownerId) {
  const response = await fetch(`${url}/rest/v1/profiles?select=id&role=eq.admin&access_status=eq.approved&order=created_at&limit=1`, { headers: auth });
  ownerId = response.ok ? (await response.json())[0]?.id : undefined;
  if (!ownerId) { console.error("No approved admin profile to run the checks as. Pass an owner id."); process.exit(2); }
}
const created = [];
let failures = 0;

async function rpc(name, args) {
  const response = await fetch(`${url}/rest/v1/rpc/${name}`, { method: "POST", headers: { ...auth, "Content-Type": "application/json" }, body: JSON.stringify(args) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${name}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}
async function reserve(kind, bytes) {
  const [asset] = await rpc("reserve_attachment_upload", { p_owner_id: ownerId, p_kind: kind, p_size_bytes: bytes.length,
    p_content_type: kind === "photo" ? "image/jpeg" : "audio/wav" });
  created.push(asset);
  const put = await fetch(`${url}/storage/v1/object/attachment-intake/${asset.intake_path}`, { method: "POST", body: bytes,
    headers: { ...auth, "Content-Type": kind === "photo" ? "image/jpeg" : "audio/wav" } });
  if (!put.ok) throw new Error(`upload: ${put.status} ${await put.text()}`);
  return asset;
}
async function processAsset(asset) {
  const response = await fetch(`${url}/functions/v1/process-attachment`, { method: "POST",
    headers: { ...auth, "Content-Type": "application/json" }, body: JSON.stringify({ asset_id: asset.id }) });
  return { status: response.status, body: await response.json() };
}
async function readClean(asset, ext) {
  const response = await fetch(`${url}/storage/v1/object/attachment-ready/${asset.owner_id}/${asset.id}/clean.${ext}`, { headers: auth });
  return response.ok ? new Uint8Array(await response.arrayBuffer()) : null;
}
function check(name, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
  if (!ok) failures += 1;
}
const has = (bytes, text) => Buffer.from(bytes).includes(Buffer.from(text));

function photo(width = 2048, height = 2048) {
  const data = new Uint8Array(width * height * 4);
  for (let i = 0; i < data.length; i += 4) { data[i] = (i / 4) % 256; data[i + 1] = 120; data[i + 2] = 200; data[i + 3] = 255; }
  const plain = new Uint8Array(jpeg.encode({ width, height, data }, 85).data);
  const exif = Buffer.from("Exif\0\0GPSLatitude 51.5074 N");
  const segment = Buffer.concat([Buffer.from([0xff, 0xe1, (exif.length + 2) >> 8, (exif.length + 2) & 0xff]), exif]);
  return new Uint8Array(Buffer.concat([plain.subarray(0, 2), segment, plain.subarray(2), Buffer.from("<html><script>x</script></html>")]));
}
function wav(seconds) {
  const samples = Math.round(seconds * 16000);
  const out = Buffer.alloc(44 + samples * 2);
  out.write("RIFF", 0); out.writeUInt32LE(36 + samples * 2, 4); out.write("WAVE", 8); out.write("fmt ", 12);
  out.writeUInt32LE(16, 16); out.writeUInt16LE(1, 20); out.writeUInt16LE(1, 22); out.writeUInt32LE(16000, 24);
  out.writeUInt32LE(32000, 28); out.writeUInt16LE(2, 32); out.writeUInt16LE(16, 34); out.write("data", 36); out.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i += 1) out.writeInt16LE(Math.round(Math.sin(i / 8) * 8000), 44 + i * 2);
  return new Uint8Array(out);
}

try {
  // 1. A real photo with GPS data and an appended HTML payload, processed twice at once.
  const good = await reserve("photo", photo());
  const [first, second] = await Promise.all([processAsset(good), processAsset(good)]);
  const statuses = [first.status, second.status].sort().join(",");
  check("photo is claimed exactly once under concurrent processing", statuses === "200,409", statuses);
  const ready = [first, second].find(result => result.status === 200)?.body;
  check("photo is rebuilt", ready?.status === "ready", JSON.stringify(ready));
  const clean = await readClean(good, "jpg");
  check("rebuilt photo has no GPS, EXIF or HTML", !!clean && !has(clean, "GPSLatitude") && !has(clean, "Exif") && !has(clean, "<html"));
  const intake = await fetch(`${url}/storage/v1/object/attachment-intake/${good.intake_path}`, { headers: auth });
  check("the original upload is deleted from quarantine", intake.status === 400 || intake.status === 404, String(intake.status));

  // 2. HTML pretending to be a JPEG.
  const fake = await reserve("photo", new Uint8Array(Buffer.from("<html><body onload=alert(1)>".padEnd(2000, " "))));
  const fakeResult = await processAsset(fake);
  check("HTML disguised as a photo is rejected", fakeResult.body?.status === "rejected", JSON.stringify(fakeResult.body));
  check("nothing is stored for a rejected photo", (await readClean(fake, "jpg")) === null);

  // 3. A decompression bomb: a header claiming 30000 x 30000.
  const bombBytes = photo(64, 64);
  for (let at = 2; at < bombBytes.length - 9; at += 1) {
    if (bombBytes[at] === 0xff && bombBytes[at + 1] === 0xc0) { bombBytes.set([0x75, 0x30, 0x75, 0x30], at + 5); break; }
  }
  const bomb = await reserve("photo", bombBytes);
  check("an oversized image header is rejected", (await processAsset(bomb)).body?.status === "rejected");

  // 4. Voice notes: a good one, and one over 60 seconds.
  const voice = await reserve("voice", wav(3));
  const voiceResult = await processAsset(voice);
  check("a 3-second voice note is rebuilt", voiceResult.body?.status === "ready" && voiceResult.body?.duration_ms === 3000, JSON.stringify(voiceResult.body));
  const long = await reserve("voice", wav(62));
  check("a voice note over 60 seconds is rejected", (await processAsset(long)).body?.status === "rejected");

  // 5. Someone without the service key gets nothing.
  const outsider = await fetch(`${url}/functions/v1/process-attachment`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ asset_id: voice.id }) });
  check("callers without the service key are refused", outsider.status === 401, String(outsider.status));
} catch (error) {
  failures += 1;
  console.error("ERROR", error instanceof Error ? error.message : error);
} finally {
  // Never forget a test row if Storage removal failed: leave it for retry.
  for (const asset of created) {
    let removed = true;
    for (const [bucket, path] of [["attachment-intake", asset.intake_path],
      ["attachment-ready", `${asset.owner_id}/${asset.id}/clean.jpg`], ["attachment-ready", `${asset.owner_id}/${asset.id}/clean.wav`]]) {
      try {
        const response = await fetch(`${url}/storage/v1/object/${bucket}`, { method: "DELETE", headers: { ...auth, "Content-Type": "application/json" },
          body: JSON.stringify({ prefixes: [path] }) });
        if (!response.ok) throw new Error(`Storage deletion: ${response.status}`);
      } catch (error) { removed = false; failures++; console.error("Cleanup must retry", asset.id, error); }
    }
    if (removed) {
      const response = await fetch(`${url}/rest/v1/attachment_assets?id=eq.${asset.id}`, { method: "DELETE", headers: auth });
      if (!response.ok) { failures++; console.error("Could not remove test row", asset.id); }
    }
  }
  console.log(failures ? `\n${failures} check(s) failed. Keep uploads switched off.` : "\nAll checks passed.");
  process.exitCode = failures ? 1 : 0;
}
