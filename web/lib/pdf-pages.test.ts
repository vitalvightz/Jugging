import assert from "node:assert/strict";
import test from "node:test";
import { DOCUMENT_MAX_BYTES, formatBytes, PAGE_MAX_BYTES, pageBudget, pageSize, pdfProblem } from "./pdf-pages";

test("only PDFs up to 5 MB are accepted", () => {
  assert.equal(pdfProblem({ name: "plan.pdf", type: "application/pdf", size: 1000 }), null);
  assert.equal(pdfProblem({ name: "plan.PDF", type: "", size: 1000 }), null);
  assert.equal(pdfProblem({ name: "plan.html", type: "text/html", size: 1000 }), "Choose a PDF file.");
  assert.equal(pdfProblem({ name: "plan.pdf", type: "application/pdf", size: 5 * 1024 * 1024 + 1 }), "That PDF is over 5 MB.");
});

test("pages fit the server limits: 1600 px long side, 2 megapixels, never more than 2x", () => {
  const a4 = pageSize(595, 842);
  assert.ok(a4.height <= 1600 && a4.width * a4.height <= 2_000_000);
  const letterLandscape = pageSize(792, 612);
  assert.ok(letterLandscape.width <= 1600 && letterLandscape.width * letterLandscape.height <= 2_000_000);
  assert.deepEqual(pageSize(100, 50), { width: 200, height: 100, scale: 2 });
  const poster = pageSize(5000, 5000);
  assert.ok(poster.width * poster.height <= 2_000_000);
});

test("every page shares the 8 MB document budget", () => {
  assert.equal(pageBudget(1), PAGE_MAX_BYTES);
  assert.ok(pageBudget(30) * 30 < DOCUMENT_MAX_BYTES);
  assert.equal(formatBytes(640_000), "625 KB");
  assert.equal(formatBytes(2_400_000), "2.3 MB");
});
