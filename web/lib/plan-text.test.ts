import assert from "node:assert/strict";
import test from "node:test";
import { deflateRawSync } from "node:zlib";
import "../components/test-dom";
(globalThis as unknown as { DOMParser: unknown }).DOMParser ??= (globalThis as unknown as { window: { DOMParser: unknown } }).window.DOMParser;
import { documentXmlText, planFileKind, tidyText, zipEntry } from "./plan-text";

const W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"';
const DOCUMENT = `<?xml version="1.0" encoding="UTF-8"?><w:document ${W}><w:body>
<w:p><w:r><w:t>Fight camp</w:t></w:r><w:r><w:tab/><w:t>Weeks 1-4</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Monday</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Squat 3x5</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>Wednesday</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Pads 6 x 3 min</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
<w:p><w:del><w:r><w:t>deleted text</w:t></w:r></w:del><w:r><w:t>Sat 5 km run</w:t></w:r></w:p>
</w:body></w:document>`;

// A minimal zip with one deflated entry, written the way Word writes them.
function zip(name: string, content: string, method: 0 | 8 = 8) {
  const raw = Buffer.from(content, "utf8");
  const data = method === 8 ? deflateRawSync(raw) : raw;
  const file = Buffer.from(name);
  const local = Buffer.alloc(30);
  local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(method, 8); local.writeUInt32LE(data.length, 18);
  local.writeUInt32LE(raw.length, 22); local.writeUInt16LE(file.length, 26);
  const central = Buffer.alloc(46);
  central.writeUInt32LE(0x02014b50, 0); central.writeUInt16LE(method, 10); central.writeUInt32LE(data.length, 20);
  central.writeUInt32LE(raw.length, 24); central.writeUInt16LE(file.length, 28); central.writeUInt32LE(0, 42);
  const centralAt = local.length + file.length + data.length;
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(1, 8); end.writeUInt16LE(1, 10);
  end.writeUInt32LE(central.length + file.length, 12); end.writeUInt32LE(centralAt, 16);
  return new Uint8Array(Buffer.concat([local, file, data, central, file, end]));
}

test("a Word file's paragraphs and table rows come out as lines, deleted text left out", async () => {
  const xml = await zipEntry(zip("word/document.xml", DOCUMENT), "word/document.xml");
  assert.ok(xml);
  const text = tidyText(documentXmlText(new TextDecoder().decode(xml)));
  assert.equal(text, "Fight camp Weeks 1-4\nMonday | Squat 3x5\nWednesday | Pads 6 x 3 min\n\nSat 5 km run");
});

test("only the named zip entry is read, stored or deflated", async () => {
  assert.equal(await zipEntry(zip("word/other.xml", DOCUMENT), "word/document.xml"), null);
  const stored = await zipEntry(zip("word/document.xml", "<x/>", 0), "word/document.xml");
  assert.equal(new TextDecoder().decode(stored!), "<x/>");
  assert.equal(await zipEntry(new Uint8Array([1, 2, 3]), "word/document.xml"), null);
});

test("PDF, .docx and screenshots are accepted by type or name; everything else is not", () => {
  assert.equal(planFileKind({ name: "plan.PDF", type: "" }), "pdf");
  assert.equal(planFileKind({ name: "plan", type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" }), "docx");
  assert.equal(planFileKind({ name: "plan.doc", type: "application/msword" }), null);
  assert.equal(planFileKind({ name: "plan.png", type: "image/png" }), "image");
  assert.equal(planFileKind({ name: "plan.gif", type: "image/gif" }), null);
  assert.equal(planFileKind({ name: "plan.svg", type: "image/svg+xml" }), null);
});
