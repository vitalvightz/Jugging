import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { deflateRawSync } from "node:zlib";
import { setup } from "./custom-plan-fixture";

// Import a plan from a file: only the text leaves the browser, the draft opens
// in the builder unsaved, and nothing is stored until Save.
const W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"';
const DOCX_XML = `<?xml version="1.0" encoding="UTF-8"?><w:document ${W}><w:body>
<w:p><w:r><w:t>Fight camp block</w:t></w:r></w:p><w:p><w:r><w:t>Weeks 1-4</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Monday</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Trap bar deadlift 3x5 @ 140kg</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>Wednesday</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Pads 6 x 3 min rounds</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>`;

function docx() {
  const raw = Buffer.from(DOCX_XML), data = deflateRawSync(raw), name = Buffer.from("word/document.xml");
  const local = Buffer.alloc(30);
  local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(8, 8); local.writeUInt32LE(data.length, 18); local.writeUInt32LE(raw.length, 22); local.writeUInt16LE(name.length, 26);
  const central = Buffer.alloc(46);
  central.writeUInt32LE(0x02014b50, 0); central.writeUInt16LE(8, 10); central.writeUInt32LE(data.length, 20); central.writeUInt32LE(raw.length, 24); central.writeUInt16LE(name.length, 28);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(1, 8); end.writeUInt16LE(1, 10);
  end.writeUInt32LE(central.length + name.length, 12); end.writeUInt32LE(local.length + name.length + data.length, 16);
  return Buffer.concat([local, name, data, central, name, end]);
}

// A one-page PDF with real text in a standard font.
function textPdf(lines: string[]) {
  const stream = `BT /F1 14 Tf 72 760 Td ${lines.map((line, index) => `${index ? "0 -22 Td " : ""}(${line}) Tj`).join(" ")} ET`;
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
    `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  let body = "%PDF-1.4\n";
  const offsets: number[] = [];
  objects.forEach((object, index) => { offsets.push(body.length); body += `${index + 1} 0 obj\n${object}\nendobj\n`; });
  const xref = body.length;
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.map(o => `${String(o).padStart(10, "0")} 00000 n \n`).join("")}`;
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(body, "latin1");
}

const id = () => crypto.randomUUID();
const mondays = ["2026-11-23", "2026-11-30", "2026-12-07", "2026-12-14"];
const padIds = mondays.map(() => id());
const result = {
  weeks: 4, sessions: 8, exercises: 12,
  flags: [
    { where: "Plan", message: "No upcoming start date written, so it starts Monday 23 Nov. Change the start date in the builder if needed." },
    { where: "Weeks 1-4 · Strength · Trap bar deadlift", message: "RPE 15 isn't between 1 and 10, so it's kept in the notes." },
    { where: "Not placed", message: "“Drink 3L water a day”" },
  ],
  // The coach wrote no day for pads: they wait on Monday until the person picks one.
  needs_day: [{ where: "Weeks 1-4 · Pads", session_ids: padIds }],
  plan: {
    name: "Fight camp block", schedule_mode: "dated", start_date: "2026-11-23", end_date: "2026-12-20",
    sessions: mondays.flatMap((monday, week) => [
      { id: id(), name: "Strength", weekday: null, scheduled_date: monday, session_type: "strength_power", notes: "Brace hard, own every rep.",
        exercises: [{ id: id(), origin: "manual", name: "Trap bar deadlift", notes: "RPE as written: 15", dose: { mode: "reps", sets: 3, reps: 5, load_or_intensity: "140kg" } },
          { id: id(), origin: "manual", name: "Shadow boxing", notes: null, dose: { mode: "intervals", rounds: 3 } }] },
      { id: padIds[week], name: "Pads", weekday: null, scheduled_date: monday, session_type: "skill", notes: null,
        exercises: [{ id: id(), origin: "manual", name: "Pad rounds", notes: "Finish on the jab", dose: { mode: "intervals", rounds: 6, work_seconds: 180, rest_seconds: 60 } }] },
    ]),
  },
};

async function routeImport(page: Page) {
  const bodies: { text: string; images: string[]; file_name: string; start_date: string }[] = [];
  await page.route("**/api/custom-plans/import", async route => {
    bodies.push(route.request().postDataJSON());
    await new Promise(resolve => setTimeout(resolve, 300));
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(result) });
  });
  return bodies;
}

for (const width of [320, 390, 1280]) {
  test(`import a Word plan and review it in the builder at ${width}px`, async ({ page, baseURL }, info) => {
    test.skip(Boolean(process.env.CUSTOM_PLAN_TEST_API), "Uses the offline fixture.");
    await page.setViewportSize({ width, height: 900 });
    const { saved } = await setup(page, baseURL!);
    const bodies = await routeImport(page);
    const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
    const uploads: string[] = [];
    page.on("request", request => { if (/storage\/v1|attachments/.test(request.url())) uploads.push(request.url()); });

    await page.goto("/create");
    await page.getByRole("link", { name: /Import Your Plan/ }).click();
    await expect(page.getByRole("heading", { name: "Bring in your plan" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`${width}-1-pick.png`) });

    await page.getByLabel("Plan file (PDF, Word or screenshots)").setInputFiles({ name: "Camp plan.docx",
      mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", buffer: docx() });
    await expect(page.getByRole("status").filter({ hasText: "Building weeks and sessions" })).toBeVisible();
    await page.screenshot({ path: info.outputPath(`${width}-2-building.png`) });
    await expect(page.getByText("Found in")).toBeVisible();
    // Only the text was sent: table rows as lines, nothing uploaded to storage.
    expect(bodies).toHaveLength(1);
    expect(bodies[0].text).toBe("Fight camp block\nWeeks 1-4\nMonday | Trap bar deadlift 3x5 @ 140kg\nWednesday | Pads 6 x 3 min rounds");
    expect(bodies[0].file_name).toBe("Camp plan.docx");
    expect(bodies[0].start_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(uploads).toEqual([]);
    await expect(page.getByRole("status")).toContainText("Weeks4Sessions8Exercises12");
    // What couldn't be mapped exactly is listed openly, by where it belongs.
    await expect(page.getByText("3 things to check")).toBeVisible();
    await expect(page.getByText("“Drink 3L water a day”")).toBeVisible();
    // No day was written for pads: nothing opens until the person picks one.
    const review = page.getByRole("button", { name: "Review and edit" });
    await expect(review).toBeDisabled();
    await expect(page.getByText("Choose a day above to continue.")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`${width}-3-found.png`) });
    expect((await new AxeBuilder({ page }).include("main").analyze()).violations
      .filter(v => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);

    await page.getByLabel("Weeks 1-4 · Pads").selectOption("Wednesday");
    await expect(review).toBeEnabled();
    await review.click();
    await expect(page).toHaveURL(/\/plans\/custom\?imported=1/);
    await expect(page.getByText("Imported from Camp plan.docx. Tick each thing to check once it's fixed or checked, then save.")).toBeVisible();
    // A dose the coach only partly wrote shows what's there, not "Set intervals".
    await expect(page.getByText("3 rounds · duration unspecified").first()).toBeVisible();
    // The coach's session note is on the session itself.
    await expect(page.getByText("Brace hard, own every rep.").first()).toBeVisible();
    await expect(page.getByText("Trap bar deadlift").first()).toBeVisible();
    // Nothing is saved until the person saves it.
    expect(saved.size).toBe(0);
    await page.screenshot({ path: info.outputPath(`${width}-4-builder.png`) });
    const save = page.getByRole("button", { name: "Save draft", exact: true });
    await expect(save).toBeEnabled();
    // The things to check stay until each one is ticked.
    await save.click();
    await expect(page.getByRole("main").getByRole("alert")).toContainText("Tick the 3 things to check above before saving.");
    expect(saved.size).toBe(0);
    for (const box of await page.getByRole("checkbox").all()) await box.check();
    await expect(page.getByText("3 things to check · all checked")).toBeVisible();
    await save.click();
    await expect.poll(() => saved.size).toBe(1);
    const plan = [...saved.values()][0];
    expect(plan.name).toBe("Fight camp block");
    expect(plan.sessions).toHaveLength(8);
    // Pads went on the day the person picked, every week; the session note was saved with Strength.
    expect(plan.sessions.filter(s => s.name === "Pads").map(s => s.scheduled_date))
      .toEqual(["2026-11-25", "2026-12-02", "2026-12-09", "2026-12-16"]);
    expect(plan.sessions.find(s => s.name === "Strength")?.notes).toBe("Brace hard, own every rep.");
    expect(errors).toEqual([]);
  });
}

test("a typed PDF is read as text; a scan, an old .doc or another file type is refused before anything is sent", async ({ page, baseURL }) => {
  test.skip(Boolean(process.env.CUSTOM_PLAN_TEST_API), "Uses the offline fixture.");
  await page.setViewportSize({ width: 390, height: 900 });
  await setup(page, baseURL!);
  const bodies = await routeImport(page);
  await page.goto("/plans/import");
  const input = page.getByLabel("Plan file (PDF, Word or screenshots)");
  await input.setInputFiles({ name: "old.doc", mimeType: "application/msword", buffer: Buffer.from("not really") });
  await expect(page.getByRole("main").getByRole("alert")).toHaveText("Older .doc files can't be read. Save it as .docx or PDF first.");
  await input.setInputFiles({ name: "anim.gif", mimeType: "image/gif", buffer: Buffer.from("GIF89a") });
  await expect(page.getByRole("main").getByRole("alert")).toHaveText("Choose a PDF, Word (.docx) file or screenshots.");
  await input.setInputFiles({ name: "scan.pdf", mimeType: "application/pdf", buffer: textPdf([]) });
  await expect(page.getByRole("main").getByRole("alert")).toContainText("no readable text");
  expect(bodies).toEqual([]);
  await input.setInputFiles({ name: "Week plan.pdf", mimeType: "application/pdf",
    buffer: textPdf(["Strength block", "Monday: Back squat 5x5 @ 100kg", "Thursday: 5 km easy run"]) });
  await expect(page.getByText("Found in")).toBeVisible();
  expect(bodies).toHaveLength(1);
  expect(bodies[0].text).toContain("Monday: Back squat 5x5 @ 100kg");
  expect(bodies[0].text).toContain("Thursday: 5 km easy run");
});

// A 2 x 2 PNG, standing in for a phone screenshot of a programme.
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg==", "base64");

test("screenshots are redrawn as plain JPEGs and sent once; pasted text works too", async ({ page, baseURL }, info) => {
  test.skip(Boolean(process.env.CUSTOM_PLAN_TEST_API), "Uses the offline fixture.");
  await page.setViewportSize({ width: 390, height: 900 });
  await setup(page, baseURL!);
  const bodies = await routeImport(page);
  await page.goto("/plans/import");
  await page.getByLabel("Plan file (PDF, Word or screenshots)").setInputFiles([
    { name: "IMG_1.png", mimeType: "image/png", buffer: PNG }, { name: "IMG_2.png", mimeType: "image/png", buffer: PNG }]);
  await expect(page.getByText("Found in")).toBeVisible();
  await expect(page.getByRole("main").getByRole("status")).toContainText("2 screenshots");
  expect(bodies).toHaveLength(1);
  expect(bodies[0].text).toBe("");
  expect(bodies[0].images).toHaveLength(2);
  for (const image of bodies[0].images) {
    expect(image.startsWith("data:image/jpeg;base64,/9j/")).toBe(true);
    // Redrawn pixels only: no PNG bytes or metadata carried over.
    expect(Buffer.from(image.split(",")[1], "base64").includes(Buffer.from("PNG"))).toBe(false);
  }

  await page.getByRole("button", { name: "Choose another file" }).click();
  await page.getByRole("button", { name: "Paste text instead" }).click();
  const box = page.getByLabel("Programme text");
  await box.fill("Mon: Strength\nTrap bar deadlift 3x5 @ 140kg\nWed: Pads 6 x 3 min rounds");
  await page.screenshot({ path: info.outputPath("390-5-paste.png") });
  await page.getByRole("button", { name: "Import text" }).click();
  await expect(page.getByText("Found in")).toBeVisible();
  expect(bodies[1]).toMatchObject({ text: "Mon: Strength\nTrap bar deadlift 3x5 @ 140kg\nWed: Pads 6 x 3 min rounds", images: [], file_name: "pasted text" });
  await page.getByLabel("Weeks 1-4 · Pads").selectOption("Thursday");
  await page.getByRole("button", { name: "Review and edit" }).click();
  await expect(page.getByText("Imported from pasted text. Tick each thing to check once it's fixed or checked, then save.")).toBeVisible();
});
