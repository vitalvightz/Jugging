import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { isolateFromNetwork } from "./support";

// PDFs on a logged session: rows show name, pages and size, nothing loads until
// the PDF is opened or downloaded, the link is fetched for that one file only,
// tapping a row reads it in the app (pages drawn to canvases), and a new
// PDF is drawn to page images in the browser, uploaded page by page, checked
// page by page and only then attached.
const day = "2026-10-19";
const athleteId = "00000000-0000-4000-8000-000000000001";
const storage = (process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").replace(/\/$/, "");
const session = { id: "manual", source: "external", title: "Boxing", activity: "boxing", duration_minutes: 40, effort: "hard", status: "done", training_day: day };
const doc = (id: string, name: string, pages: number, size: number, author: "athlete" | "coach" = "athlete") => ({
  id, created_at: `${day}T10:00:00Z`, kind: "pdf", author, removable: author === "athlete", media: null,
  file: { url: null, width: null, height: null, duration_ms: null, name, pages, size_bytes: size },
});

// A small two-page PDF with plain vector shapes (no fonts needed to render).
function twoPagePdf() {
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Count 2 /Kids [3 0 R 5 0 R] >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R >>",
    "",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 6 0 R >>",
    "",
  ];
  const draw = ["0.1 0.1 0.1 rg 60 700 475 40 re f 60 640 300 14 re f 60 610 380 14 re f", "0.7 0.2 0.2 rg 60 400 200 200 re f"];
  objects[3] = `<< /Length ${draw[0].length} >>\nstream\n${draw[0]}\nendstream`;
  objects[5] = `<< /Length ${draw[1].length} >>\nstream\n${draw[1]}\nendstream`;
  let body = "%PDF-1.4\n";
  const offsets: number[] = [];
  objects.forEach((object, index) => { offsets.push(body.length); body += `${index + 1} 0 obj\n${object}\nendobj\n`; });
  const xref = body.length;
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.map(o => `${String(o).padStart(10, "0")} 00000 n \n`).join("")}`;
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(body, "latin1");
}

const block = { feedback_ref: "11111111-1111-4111-8111-111111111111", name: "Trap bar deadlift", kind: "exercise", source: "manual",
  format: null, prescribed: null, actual: { sets: 3, reps: 5, load: { value: 140, unit: "kg" } }, status: "done", reason: null, notes: null };

async function setup(page: Page, baseURL: string, { exercises = false } = {}) {
  await isolateFromNetwork(page, baseURL);
  const now = new Date(`${day}T12:00:00Z`);
  await page.clock.setFixedTime(now);
  const key = `sb-${new URL(storage).hostname.split(".")[0]}-auth-token`;
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [key, JSON.stringify({
    access_token: "pdf-test", refresh_token: "test", token_type: "bearer", expires_at: now.getTime() / 1000 + 86400,
    user: { id: athleteId, email: "fighter@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  let rows = [doc("d1", "Fight week nutrition and weight cut plan - final version.pdf", 12, 2_400_000, "coach"),
    doc("d2", "Week 1 plan.pdf", 3, 640_000)];
  const calls: string[] = [];
  const uploads: { path: string; type: string; size: number; jpeg: boolean; pdf: boolean }[] = [];
  await page.route(`${storage}/storage/v1/object/attachment-intake/**`, route => {
    const request = route.request();
    const body = request.postDataBuffer() ?? Buffer.alloc(0);
    uploads.push({ path: new URL(request.url()).pathname, type: request.headers()["content-type"] ?? "", size: body.length,
      jpeg: body.includes(Buffer.from([0xff, 0xd8, 0xff])), pdf: body.includes(Buffer.from("%PDF")) });
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ Key: "ok" }) });
  });
  await page.route(`${storage}/storage/v1/object/sign/**`, route => route.fulfill({
    contentType: "application/pdf", headers: { "Content-Disposition": 'attachment; filename="Week 1 plan.pdf"' }, body: twoPagePdf(),
  }));
  await page.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    calls.push(`${request.method()} ${path}${url.search}`);
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/me") return json({ profile: { athlete_id: athleteId, email: "fighter@example.com", full_name: "Carlos", role: "athlete", access_status: "approved", technical_style: ["boxing"], tactical_style: [], athlete_timezone: "Europe/London", athlete_locale: "en", appearance_mode: "dark", nutrition_profile: {}, date_of_birth: "1995-01-01", meets_minimum_age: true, is_minor: false, terms_accepted: true, health_consent_granted: true, private_trial_ack_at: `${day}T12:00:00Z` }, plan_count: 0, latest_plan: null, latest_intake: null, username_rate_limit: {} });
    if (path === "/api/training/history") return json({ sessions: [session] });
    if (path.startsWith("/api/training/history/")) return json({ session, athlete_id: athleteId, prescription_source: "manual",
      blocks: exercises ? [block] : [] });
    if (path === "/api/private-media/settings") return json({ uploads_available: false, youtube_available: false });
    if (path === "/api/attachments/settings") return json({ photos_available: true, voice_available: true, pdfs_available: true });
    if (path === "/api/attachments/uploads") {
      const body = request.postDataJSON();
      return json({ asset_id: "a0000000-0000-4000-8000-000000000009", bucket: "attachment-intake", content_type: "image/jpeg",
        path: `${athleteId}/a0000000-0000-4000-8000-000000000009/pages`, expires_at: `${day}T12:15:00Z`, page_count: body.page_count }, 201);
    }
    if (path.endsWith("/process")) return json({ asset_id: "a0000000-0000-4000-8000-000000000009", status: url.searchParams.get("page") ? "page_ready" : "ready" });
    if (path.endsWith("/play")) {
      const id = path.split("/").at(-2);
      const found = rows.find(item => item.id === id)!;
      return json({ ...found, file: { ...found.file, url: `${storage}/storage/v1/object/sign/attachment-ready/${id}?token=t&download=${encodeURIComponent(found.file.name)}` } });
    }
    if (path === "/api/training/attachments" && request.method() === "POST") {
      const row = doc("d3", "Sparring notes.pdf", 2, 180_000);
      rows = [...rows, row];
      return json(row, 201);
    }
    if (path === "/api/training/attachments" && request.method() === "GET") return json(rows);
    if (request.method() === "DELETE") { rows = rows.filter(item => !path.endsWith(item.id)); return route.fulfill({ status: 204 }); }
    if (path === "/api/plans/active") return json({ detail: "No active plan" }, 404);
    if (path === "/api/plans") return json([]);
    return json({ notifications: [], unread_count: 0, level: 1, total_xp: 0, logs: [] });
  });
  return { calls, uploads };
}

for (const width of [320, 390, 1280]) {
  test(`PDFs on a session at ${width}px`, async ({ page, baseURL }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const { calls } = await setup(page, baseURL!);
    const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
    const signed: string[] = [];
    page.on("request", request => { if (request.url().includes("/storage/v1/object/sign/")) signed.push(request.url()); });
    await page.goto("/history");
    await page.getByRole("button", { name: /View Boxing details/ }).click();
    const dialog = page.getByRole("dialog", { name: "Boxing" });
    const list = dialog.getByRole("list", { name: "PDFs for Boxing" });
    await expect(list.getByRole("listitem")).toHaveCount(2);
    await expect(list).toContainText("Week 1 plan.pdf");
    await expect(list).toContainText("3 pages · 625 KB · You");
    await expect(list).toContainText("12 pages · 2.3 MB · Coach");
    // A coach's file can't be removed by the athlete from here; their own can.
    await expect(list.getByRole("button", { name: /^Remove / })).toHaveCount(1);
    // Nothing is fetched until someone asks, and no label wraps or overflows.
    expect(signed).toEqual([]);
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    for (const row of await list.getByRole("listitem").all()) {
      const box = await row.boundingBox();
      expect(box!.height).toBeLessThanOrEqual(72);
    }
    await list.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath(`${width}-1-pdfs.png`) });

    await dialog.getByRole("button", { name: "Attach", exact: true }).click();
    await expect(dialog.getByRole("menuitem")).toHaveText(["Photo", "Voice note", "PDF"]);
    await page.screenshot({ path: info.outputPath(`${width}-2-menu.png`) });
    await dialog.getByRole("button", { name: "Attach", exact: true }).click();

    const downloaded = page.waitForEvent("download");
    await list.getByRole("button", { name: "Download Week 1 plan.pdf" }).click();
    const file = await downloaded;
    expect(file.suggestedFilename()).toBe("Week 1 plan.pdf");
    expect(calls.filter(call => call.endsWith("/play"))).toEqual(["GET /api/training/attachments/d2/play"]);
    // The browser's own PDF plug-in is never used.
    await expect(page.locator("iframe, embed, object")).toHaveCount(0);
    expect((await new AxeBuilder({ page }).include("dialog").analyze()).violations
      .filter(v => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);

    // Tapping the row reads the PDF in the app: each page drawn to a canvas, with Download one tap away.
    await list.getByRole("button", { name: "View Week 1 plan.pdf" }).click();
    const reader = page.getByRole("dialog", { name: "Week 1 plan.pdf" });
    await expect(reader.getByRole("img", { name: "Page 1" })).toBeVisible();
    await expect(reader).toContainText("Page 1 of 2");
    await expect(reader.locator("[data-page='1'][data-drawn]")).toHaveCount(1);
    const canvas = reader.getByRole("img", { name: "Page 1" });
    const box = await canvas.boundingBox();
    expect(box!.width).toBeLessThanOrEqual(width);
    if (width < 600) expect(box!.width).toBeGreaterThan(width - 40);
    // The drawn page isn't blank: the dark header bar from the test PDF is on it.
    expect(await canvas.evaluate(element => {
      const c = element as HTMLCanvasElement;
      const pixels = c.getContext("2d")!.getImageData(0, 0, c.width, Math.floor(c.height / 5)).data;
      for (let i = 0; i < pixels.length; i += 4) if (pixels[i] < 60) return true;
      return false;
    })).toBe(true);
    expect(await reader.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await expect(page.locator("iframe, embed, object")).toHaveCount(0);
    await page.screenshot({ path: info.outputPath(`${width}-3-reader.png`) });
    expect((await new AxeBuilder({ page }).include("dialog[aria-label='Week 1 plan.pdf']").analyze()).violations
      .filter(v => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);
    const again = page.waitForEvent("download");
    await reader.getByRole("button", { name: "Download", exact: true }).click();
    expect((await again).suggestedFilename()).toBe("Week 1 plan.pdf");
    await reader.getByRole("button", { name: "Close PDF" }).click();
    await expect(reader).toHaveCount(0);
    await expect(dialog).toBeVisible();
    expect(errors).toEqual([]);
  });
}

test("a PDF is drawn to page images, uploaded and checked page by page, then attached", async ({ page, baseURL }, info) => {
  await page.setViewportSize({ width: 390, height: 900 });
  const { calls, uploads } = await setup(page, baseURL!);
  await page.goto("/history");
  await page.getByRole("button", { name: /View Boxing details/ }).click();
  const dialog = page.getByRole("dialog", { name: "Boxing" });
  await dialog.getByLabel("Attach a PDF to Boxing").setInputFiles({ name: "Sparring notes.pdf", mimeType: "application/pdf", buffer: twoPagePdf() });
  await expect(dialog.getByRole("list", { name: "PDFs for Boxing" })).toContainText("Sparring notes.pdf");
  await page.screenshot({ path: info.outputPath("390-3-attached.png") });
  // Only JPEG page images reach storage; the PDF itself is never uploaded.
  expect(uploads.map(upload => upload.path.replace(/^.*\/pages\//, ""))).toEqual(["001.jpg", "002.jpg"]);
  expect(uploads.map(({ jpeg, pdf }) => ({ jpeg, pdf }))).toEqual([{ jpeg: true, pdf: false }, { jpeg: true, pdf: false }]);
  const reserve = calls.filter(call => call.startsWith("POST /api/attachments/uploads"));
  expect(reserve).toEqual([
    "POST /api/attachments/uploads",
    "POST /api/attachments/uploads/a0000000-0000-4000-8000-000000000009/process?page=1",
    "POST /api/attachments/uploads/a0000000-0000-4000-8000-000000000009/process?page=2",
    "POST /api/attachments/uploads/a0000000-0000-4000-8000-000000000009/process",
  ]);
});

test("files that aren't PDFs, or are too large, are refused before upload", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  const { calls, uploads } = await setup(page, baseURL!);
  await page.goto("/history");
  await page.getByRole("button", { name: /View Boxing details/ }).click();
  const dialog = page.getByRole("dialog", { name: "Boxing" });
  const input = dialog.getByLabel("Attach a PDF to Boxing");
  await input.setInputFiles({ name: "evil.pdf", mimeType: "application/pdf", buffer: Buffer.from("<html><script>alert(1)</script></html>") });
  await expect(dialog.getByRole("alert").filter({ hasText: "PDF" })).toHaveText("That file isn't a PDF.");
  await input.setInputFiles({ name: "big.pdf", mimeType: "application/pdf", buffer: Buffer.alloc(5 * 1024 * 1024 + 1) });
  await expect(dialog.getByRole("alert").filter({ hasText: "PDF" })).toHaveText("That PDF is over 5 MB.");
  expect(uploads).toEqual([]);
  expect(calls.filter(call => call.startsWith("POST /api/attachments/uploads"))).toEqual([]);
});

for (const width of [320, 1280]) {
  test(`Attach sits at the top of a session that has exercises at ${width}px`, async ({ page, baseURL }, info) => {
    await page.setViewportSize({ width, height: 900 });
    await setup(page, baseURL!, { exercises: true });
    await page.goto("/history");
    await page.getByRole("button", { name: /View Boxing details/ }).click();
    const dialog = page.getByRole("dialog", { name: "Boxing" });
    const heading = dialog.getByRole("heading", { name: "Attachments" });
    const exercises = dialog.getByRole("heading", { name: "Exercises" });
    await expect(heading).toBeVisible();
    await expect(dialog.getByText("Trap bar deadlift")).toBeVisible();
    // Above the exercises, with Attach on the same line as its title.
    expect((await heading.boundingBox())!.y).toBeLessThan((await exercises.boundingBox())!.y);
    const attach = dialog.getByRole("button", { name: "Attach", exact: true }).first();
    // Measured together, so the sheet's slide-in can't skew the comparison.
    const offset = await dialog.evaluate(element => {
      const center = (node: Element | null) => { const box = node!.getBoundingClientRect(); return box.top + box.height / 2; };
      const title = [...element.querySelectorAll("h3")].find(node => node.textContent === "Attachments")!;
      // The title block (title, and the count under it once there are files) lines up with Attach.
      return Math.abs(center(title.parentElement) - center(title.parentElement!.parentElement!.querySelector("button")));
    });
    expect(offset).toBeLessThanOrEqual(4);
    await attach.click();
    await expect(dialog.getByRole("menuitem")).toHaveText(["Photo", "Voice note", "PDF"]);
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`${width}-4-top-attach.png`) });
  });
}
