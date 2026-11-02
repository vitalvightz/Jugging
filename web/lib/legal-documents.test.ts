import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { PRIVACY_NOTICE_VERSION, TERMS_VERSION } from "@/lib/compliance";
import {
  GOOGLE_PRIVACY_POLICY_LINK,
  LEGAL_DOCUMENTS,
  PRIVACY_HREF,
  PRIVACY_NOTICE,
  TERMS_HREF,
  TERMS_OF_USE,
  YOUTUBE_TERMS_LINK,
  buildDataRequestMailto,
  getPrivacyContactEmail,
  type LegalDocument,
} from "@/lib/legal-documents";

/**
 * Anti-drift checks for the in-app legal copy.
 *
 * The app ships its own rendering of the Terms and Privacy Notice so an athlete
 * can read what they are agreeing to at the moment they agree, which means two
 * copies exist and can diverge silently. These tests pin every substantive fact
 * — service-provider safeguards, the remaining placeholder, the operator, the
 * dates, the versions — so a change to one has to be a deliberate change to the
 * other.
 *
 * The canonical markdown now lives in this repo under docs/, so the final test
 * compares the two copies directly instead of relying on the pinned facts alone.
 * It holds the Service providers and International transfers paragraphs to a
 * verbatim match, because those are the ones where a silent divergence would
 * misdescribe who receives athlete data.
 */

const REPO_ROOT = path.resolve(process.cwd(), "..");

function allText(document: LegalDocument): string {
  return [
    document.title,
    document.status,
    document.intro,
    ...document.sections.flatMap((section) => [
      section.heading,
      ...(section.paragraphs ?? []),
      ...(section.bullets ?? []),
    ]),
  ].join("\n");
}

function everyDocumentText(): string {
  return LEGAL_DOCUMENTS.map(allText).join("\n");
}

// --- what may be published at all -------------------------------------------

test("only the Terms and the Privacy Notice are exposed in-app", () => {
  assert.deepEqual(
    LEGAL_DOCUMENTS.map((document) => document.slug).sort(),
    ["privacy-notice", "terms-of-use"],
  );
  assert.equal(TERMS_HREF, "/legal/terms-of-use");
  assert.equal(PRIVACY_HREF, "/legal/privacy-notice");
});

test("athlete-facing legal documents do not contain em dashes", () => {
  assert.ok(!everyDocumentText().includes("—"));
});

test("internal compliance documents never leak into athlete-facing copy", () => {
  // The DPIA, processor/DPA register, breach procedure, intended-purpose memo,
  // internal retention procedure and storage/tracking assessment are internal
  // controls. They are not written for athletes and must not be published.
  const published = everyDocumentText().toLowerCase();
  const internalOnly = [
    "dpia",
    "data protection impact assessment",
    "processor register",
    "dpa register",
    // "breach" alone is legitimate contract language in the Terms ("serious
    // breach of these Terms"); it is the internal *procedure* that must not be
    // published.
    "breach procedure",
    "breach-notification",
    "intended purpose",
    "intended-purpose",
    "regulatory boundary",
    "launch gate",
    "storage-access",
    "tracking assessment",
    "residual risk",
    "article 30",
  ];
  for (const phrase of internalOnly) {
    assert.ok(!published.includes(phrase), `athlete-facing copy must not mention "${phrase}"`);
  }
});

// --- service providers -------------------------------------------------------

/**
 * Every processor in the register is named in the public notice.
 *
 * This used to assert the opposite — that the notice described categories only
 * and named nobody. Categories do satisfy Article 13(1)(e), so the old rule was
 * not unlawful, but it meant an athlete could not find out who actually receives
 * their health data, and it gave a passing test to a notice that omitted Sentry
 * while Sentry was live in both the frontend and the backend.
 *
 * The list is deliberately checked in both directions: a provider added to the
 * register without reaching the notice is an undisclosed recipient, and a
 * provider dropped from production without leaving the notice is a false one.
 */
const REGISTERED_PROCESSORS = [
  "Supabase",
  "OpenAI",
  "Vercel",
  "Hetzner",
  "Resend",
  "Cloudflare Turnstile",
  "Sentry",
] as const;

test("the public notice names every processor in the internal register", () => {
  const section = PRIVACY_NOTICE.sections.find((entry) => entry.heading === "Service providers");
  assert.equal(section?.bullets?.length, REGISTERED_PROCESSORS.length, "one bullet per processor");

  const publicNotice = allText(PRIVACY_NOTICE);
  const internalRegister = readFileSync(
    path.join(REPO_ROOT, "docs", "processor-dpa-international-transfer-verification.md"),
    "utf8",
  );

  for (const provider of REGISTERED_PROCESSORS) {
    assert.ok(publicNotice.includes(provider), `public notice must name ${provider}`);
    assert.ok(internalRegister.includes(provider), `internal register must retain ${provider}`);
  }
});

test("Sentry is disclosed as a processor, and session recording is ruled out", () => {
  // Sentry ran undisclosed in production while three compliance documents and
  // this test recorded that it was not used. Naming it does not close the DPA
  // or transfer work — see docs/data-map-processor-register.md — but a notice
  // that omits a live processor is a separate failure stacked on top of those.
  const notice = allText(PRIVACY_NOTICE);
  assert.ok(notice.includes("Sentry"));

  // Session Replay was removed rather than put behind consent, so the notice
  // now states the stronger fact. If replay is ever reintroduced this assertion
  // fails, which is the point: the claim and the code have to move together.
  assert.match(notice, /does not record your screen or session/i);
});

test("Session Replay stays out of the client Sentry configuration", () => {
  // The compliance position published to athletes is "we do not record your
  // screen or session". That promise lives in a document; this is what keeps it
  // true in the code. Replay also re-engages PECR reg. 6 consent, which UNLXCK
  // has no mechanism for.
  const client = readFileSync(path.join(process.cwd(), "instrumentation-client.ts"), "utf8");
  const active = client
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("//") && !line.trimStart().startsWith("*"))
    .join("\n");

  for (const forbidden of ["replayIntegration", "replaysSessionSampleRate", "replaysOnErrorSampleRate"]) {
    assert.ok(!active.includes(forbidden), `${forbidden} must not be configured`);
  }
});

test("international-transfer safeguards remain concise and specific", () => {
  const transfers = PRIVACY_NOTICE.sections.find(
    (entry) => entry.heading === "International transfers",
  );
  const text = (transfers?.paragraphs ?? []).join(" ");
  assert.equal(transfers?.paragraphs?.length, 1);
  assert.match(text, /outside the UK/);
  assert.match(text, /restricted transfer/);
  assert.ok(/Standard Contractual Clauses|UK Addendum/.test(text), "safeguard should be named");
});

// --- stale pre-verification wording -----------------------------------------

test("wording that the completed verification made untrue is gone", () => {
  const published = everyDocumentText();
  const stale = [
    "will complete and document",
    "will be finalised before public launch",
    "must be finalised before this notice is used for public launch",
    "Launch status",
    "transfer safeguards and unresolved retention periods",
  ];
  for (const phrase of stale) {
    assert.ok(!published.includes(phrase), `stale wording still present: "${phrase}"`);
  }
});

test("the notice publishes only retention periods UNLXCK actually enforces", () => {
  const retention = PRIVACY_NOTICE.sections.find(
    (entry) => entry.heading === "How long we keep data",
  );
  const text = (retention?.paragraphs ?? []).join(" ");

  assert.ok(/irreversibly anonymise/i.test(text));

  // The two periods that are met: screenshots are enforced in
  // api/services/feedback_service.py, and erasure is a request-driven process
  // with a named owner rather than a background job.
  assert.ok(text.includes("90 days"), "the screenshot period should be stated");
  assert.match(text, /within one month/i, "the erasure deadline should be stated");

  // Everything else is criteria-based on purpose. Article 13(2)(a) permits
  // criteria where a period cannot be given, and a published period that is
  // missed is a specific evidenced failure — worse than the vagueness it
  // replaced. Dormancy, post-closure deletion and log expiry have no scheduled
  // job yet (docs/data-retention-deletion-user-rights.md), so their periods
  // stay internal targets until the automation exists.
  for (const unenforced of ["24 months", "30 days"]) {
    assert.ok(
      !text.includes(unenforced),
      `"${unenforced}" is an internal target, not an enforced period — do not publish it`,
    );
  }
  assert.match(text, /only while it is still needed/i, "state the criteria for the unautomated cases");
});

// --- placeholders ------------------------------------------------------------

/**
 * The outstanding launch blocker, tracked as data.
 *
 * UNLXCK trades as a sole trader, so the proprietor's own name and a geographic
 * address have to appear in both documents — reg. 6 of the Electronic Commerce
 * (EC Directive) Regulations 2002, Sch. 2 of the Consumer Contracts Regulations
 * 2013, and Article 13(1)(a) UK GDPR for the controller's identity. None of
 * those is satisfied by the trading name alone.
 *
 * The remaining outstanding value stays a visible placeholder until the real
 * value is inserted. Inventing one, or quietly omitting the field, would turn a
 * known gap into a silent defect — and the placeholder is what makes it
 * impossible to publish these documents without noticing.
 */
const OUTSTANDING_PLACEHOLDERS = ["[TRADING ADDRESS]"] as const;

test("the outstanding trading-address blocker is still visible", () => {
  const published = everyDocumentText();
  for (const placeholder of OUTSTANDING_PLACEHOLDERS) {
    assert.ok(published.includes(placeholder), `${placeholder} should still be marked outstanding`);
  }

  // No placeholder may exist that is not on the register above: an unlisted one
  // is a gap nobody is tracking.
  const bracketed = published.match(/\[[^\]]+\]/g) ?? [];
  assert.deepEqual(
    [...new Set(bracketed)].sort(),
    [...OUTSTANDING_PLACEHOLDERS].sort(),
    "an untracked placeholder is in the published copy",
  );

  assert.ok(published.includes("support@unlxck.com"), "the real support email should be published");
  assert.ok(!published.includes("[ADD PRIVACY EMAIL BEFORE PUBLIC LAUNCH]"));
  assert.ok(!published.includes("[LEGAL/CONTACT EMAIL]"));
});

test("both documents declare themselves not ready for publication", () => {
  // While the trading address is outstanding, neither document may present
  // itself as final. The status line is what an athlete and a reviewer see.
  for (const document of LEGAL_DOCUMENTS) {
    assert.match(
      document.status,
      /not ready for publication/i,
      `${document.slug} must not read as publishable while the trading address is outstanding`,
    );
  }

  assert.ok(TERMS_OF_USE.intro.includes("Michael Okafor"));
  assert.ok(TERMS_OF_USE.intro.includes("[TRADING ADDRESS]"));
  assert.ok(TERMS_OF_USE.intro.includes("sole trader trading as Unlxck"));
});

test("data-request route defaults to the public support email", () => {
  const previous = process.env.NEXT_PUBLIC_PRIVACY_CONTACT_EMAIL;
  delete process.env.NEXT_PUBLIC_PRIVACY_CONTACT_EMAIL;
  try {
    assert.equal(getPrivacyContactEmail(), "support@unlxck.com");
    assert.equal(
      buildDataRequestMailto("subject", "body"),
      "mailto:support@unlxck.com?subject=subject&body=body",
    );
  } finally {
    if (previous === undefined) {
      delete process.env.NEXT_PUBLIC_PRIVACY_CONTACT_EMAIL;
    } else {
      process.env.NEXT_PUBLIC_PRIVACY_CONTACT_EMAIL = previous;
    }
  }
});

// --- versions and dates ------------------------------------------------------

test("each document carries its own version, and the Terms track acceptance", () => {
  // The Terms version gates acceptance: bumping it re-collects agreement from
  // every athlete, so the displayed version and the recorded one must not drift.
  assert.equal(TERMS_OF_USE.version, TERMS_VERSION);

  // The notice tracks its own revision rather than HEALTH_CONSENT_VERSION. They
  // were the same constant, which meant correcting the notice re-collected
  // Article 9(2)(a) consent from every athlete and took their health-dependent
  // features offline until they answered — a cost that argued for leaving the
  // notice wrong. Keeping them apart is what lets the notice be fixed.
  assert.equal(PRIVACY_NOTICE.version, PRIVACY_NOTICE_VERSION);
  assert.notEqual(PRIVACY_NOTICE_VERSION, TERMS_VERSION);
});

test("the Terms carry an effective date and the notice a revision date", () => {
  assert.equal(TERMS_OF_USE.effectiveDate, "19 August 2026");
  assert.equal(PRIVACY_NOTICE.lastUpdated, "2 November 2026");
  assert.equal(TERMS_OF_USE.lastUpdated, undefined);
});

// --- athlete-facing substance ------------------------------------------------

test("both documents keep the promises the product enforces", () => {
  const terms = allText(TERMS_OF_USE);
  // The under-18 weight-cut rule is enforced in code; the Terms state it.
  assert.ok(/under-18|under 18/.test(terms));
  assert.ok(/dehydration|water-cut/.test(terms));
  assert.ok(terms.includes("13"), "the 13+ eligibility floor should be stated");

  const privacy = allText(PRIVACY_NOTICE);
  assert.ok(privacy.includes("Article 9(2)(a)"), "the health-data lawful basis should be stated");
  assert.ok(/withdraw/i.test(privacy), "withdrawal should be explained");
  assert.ok(/Settings/.test(privacy), "the in-app route should be pointed to");
});

test("YouTube demo videos are disclosed as the YouTube API Services require", () => {
  // The YouTube API Services Terms require the Terms of Use to bind users to the
  // YouTube Terms of Service, and the privacy notice to say the app uses YouTube
  // API Services, link the Google Privacy Policy and describe device storage.
  const terms = TERMS_OF_USE.sections.find((entry) => entry.heading === "Exercise demonstration videos");
  assert.ok(terms, "the Terms should cover demonstration videos");
  assert.match(allText(TERMS_OF_USE), /agree to be bound by the YouTube Terms of Service/);
  assert.deepEqual(terms.links, [YOUTUBE_TERMS_LINK]);

  const notice = PRIVACY_NOTICE.sections.find((entry) => entry.heading === "YouTube demonstration videos");
  assert.ok(notice, "the notice should cover YouTube demonstration videos");
  const text = (notice.paragraphs ?? []).join(" ");
  assert.match(text, /YouTube API Services/);
  assert.match(text, /IP address/);
  assert.match(text, /cookies or local storage/);
  assert.match(text, /only when you tap play/);
  assert.match(text, /separate controller/);
  assert.deepEqual(notice.links, [GOOGLE_PRIVACY_POLICY_LINK, YOUTUBE_TERMS_LINK]);
  assert.equal(GOOGLE_PRIVACY_POLICY_LINK.href, "https://policies.google.com/privacy");
  assert.equal(YOUTUBE_TERMS_LINK.href, "https://www.youtube.com/t/terms");

  // YouTube is an independent controller, not a processor acting for UNLXCK,
  // so it stays out of the Service providers list.
  const providers = PRIVACY_NOTICE.sections.find((entry) => entry.heading === "Service providers");
  assert.ok(!(providers?.bullets ?? []).some((bullet) => bullet.includes("YouTube")));
});

// --- direct comparison, once the canonical docs are in this repo --------------

test("in-app copy matches the canonical docs when they are present", () => {
  const canonical: Array<[LegalDocument, string]> = [
    [TERMS_OF_USE, path.join(REPO_ROOT, "docs", "terms-of-use.md")],
    [PRIVACY_NOTICE, path.join(REPO_ROOT, "docs", "privacy-notice.md")],
  ];

  const present = canonical.filter(([, file]) => existsSync(file));
  if (present.length === 0) {
    // Expected today: the canonical markdown lives on the docs branch and has
    // not merged into this one. The pinned-fact tests above are the check until
    // it does; this upgrades to a real comparison the moment the files land.
    return;
  }

  for (const [document, file] of present) {
    // Markdown links read as their label, which is how the in-app copy words
    // them (the URLs are rendered as separate links there).
    const markdown = readFileSync(file, "utf8").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
    // The canonical Terms number their sections ("## 2. Eligibility"); the
    // in-app rendering does not, because the numbering carries no meaning once
    // the sections are cards on a page. Compare on the text alone.
    const headings = [...markdown.matchAll(/^##\s+(.+)$/gm)].map((match) =>
      match[1].trim().replace(/^\d+\.\s*/, ""),
    );
    const rendered = document.sections.map((section) => section.heading);

    for (const heading of headings) {
      // Internal-only trailing sections are deliberately not published.
      if (["Launch status", "Status", "About these Terms"].includes(heading)) {
        continue;
      }
      assert.ok(
        rendered.includes(heading),
        `${document.slug}: canonical section "${heading}" is missing from the in-app copy`,
      );
    }

    // The YouTube disclosures are what the YouTube API Services Terms require,
    // so both copies must say exactly the same thing.
    const youtubeHeading =
      document.slug === "privacy-notice" ? "YouTube demonstration videos" : "Exercise demonstration videos";
    const youtube = document.sections.find((entry) => entry.heading === youtubeHeading);
    assert.ok(youtube, `in-app ${document.slug} should contain ${youtubeHeading}`);
    for (const paragraph of youtube.paragraphs ?? []) {
      assert.ok(
        markdown.includes(paragraph.replace(/’/g, "'")) || markdown.includes(paragraph),
        `canonical ${document.slug} is out of sync with the in-app ${youtubeHeading} copy`,
      );
    }

    // A processor named in the canonical notice must be named in-app, and
    // vice versa — this is the drift that matters most.
    if (document.slug === "privacy-notice") {
      for (const heading of ["Service providers", "International transfers"]) {
        const section = document.sections.find((entry) => entry.heading === heading);
        assert.ok(section, `in-app notice should contain ${heading}`);
        for (const paragraph of section.paragraphs ?? []) {
          assert.ok(
            markdown.includes(paragraph),
            `canonical notice is out of sync with the in-app ${heading} copy`,
          );
        }
      }
      for (const provider of ["Supabase", "OpenAI", "Vercel", "Hetzner", "Resend", "Cloudflare"]) {
        assert.equal(
          markdown.includes(provider),
          allText(document).includes(provider),
          `${provider} appears in only one of the canonical notice and the in-app copy`,
        );
      }
      assert.equal(
        /sentry/i.test(markdown),
        /sentry/i.test(allText(document)),
        "Sentry appears in only one of the canonical notice and the in-app copy",
      );
    }
  }
});