# Coach workspace design QA

final result: passed

## Source and comparison state

Source visual truth: the two inline screenshots attached by the user in this
chat (coach dashboard and fighter overview). They are available as conversation
images, not repository files. Their visible widths are approximately 483 and
476 pixels. They depict illustrative data, including 12 fighters; those values
are not production data and were not copied into the implementation.

Implementation evidence (browser-rendered Chromium, deviceScaleFactor 1):

- `web/test-results/coach-workspace-coach-rost-43bef-d-fighter-overview-at-390px-chromium/coach-roster-390.png`
- `web/test-results/coach-workspace-coach-rost-43bef-d-fighter-overview-at-390px-chromium/coach-fighter-compact-390.png`
- Equivalent captures at 320 and 1440 CSS-pixel widths are in `web/test-results`.

Comparison: source dashboard/profile screenshots and the rendered dashboard /
compact profile views were visually inspected. Mobile captures use 390 x 1000
CSS-pixel viewports and full-page screenshots; source images are scaled chat
references, so this is a composition and product-quality comparison, not an
exact pixel-difference claim. Existing app navigation and safety footer are
preserved, and are outside the new screen content. The compact profile capture
has session/sharing details closed, matching the reference's resting state.

## Comparison history and fixes

1. The original dashboard had large cards, a wide table and long explanatory
   copy. It did not have the reference's priority and upcoming-fight sections.
   Replaced the main roster table with compact linked rows, added the attention
   list and countdown cards, reduced metric/card spacing and moved count
   explanations and connection management into details disclosures.
2. The first compact profile capture had a narrow intrinsic content width and
   navigation buttons wrapping. Set the workspace width to 100%, removed the
   inherited minimum width on breadcrumb actions and adjusted mobile spacing.
   The revised 390px screenshot uses the full content width, with all four
   performance tiles in a balanced two-column grid.
3. Small dark red headings originally failed contrast checks. Coach-only text
   colours now use a readable pink accent, without changing athlete styles.
   WCAG checks include colour contrast on both dashboard and profile.

## Required fidelity surfaces

- Typography: retained UNLXCK's existing sans-serif system, with smaller
  headings, compact numeric metrics and muted metadata. No decorative fonts
  or new font dependencies. The dashboard title no longer wraps at 390px.
- Spacing/layout: the reference hierarchy is present: metrics, priority,
  upcoming fights, fighter roster; profile identity/camp, weekly performance,
  training, recovery and sparring/S&C. Focused inspection of the header,
  metric grids and recovery tiles confirmed readable labels and no clipping.
  The screen adapts to the existing desktop shell and to 320/390px phones.
- Colours/tokens: existing dark panels, subtle borders, red/pink concern
  accents, muted metadata and green S&C progress. No new gradients or charts.
- Assets: existing verified avatar handling and initials are retained. The
  app's existing line-icon components are reused. No generated imagery or
  fabricated fighter portraits are needed for these references.
- Copy: visible explanations are short; detailed definitions, check-ins and
  recent records are available on demand. Consent wording is retained before
  sharing. Values come from existing records; unknown minutes, readiness and
  injury access are explicitly represented rather than invented.

## Verification

Primary browser interactions: roster search, fighter navigation, weekly
controls, connection disclosure, invitations, acceptance, optional consent,
revocation, refresh after loss of access and RPC error recovery. Tests inspect
page errors and run WCAG A/AA scans, including contrast, on dashboard and
profile. No document-width overflow at 320, 390 or 1440 pixels.

Synthetic records are confined to intercepted browser responses and isolated
PostgreSQL tests. No production fixtures or live athlete writes were made.

## Findings and accepted differences

No actionable P0/P1/P2 findings remain. The existing UNLXCK navigation and safety
footer are intentionally preserved. Record counts, unknown health values and
extra permission disclosures intentionally differ from the illustrative mock.

P3: existing app icons are slightly different from the reference. This avoids
introducing another icon library for a small additive workspace.

## Implementation checklist

- Compact dashboard and profile implemented inside existing routes.
- Reference hierarchy and short copy checked in rendered screenshots.
- Mobile layout, keyboard controls and accessibility checked.
- Permission boundary and read-only performance access retained.
