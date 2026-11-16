# Coach Plans dashboard design QA

Source visual: the user's inline illustrative mobile concept in the redesign
request (Fighter Plans / Coach Technical Week / 2 of 2 sessions / 100%). The
source is visible in the chat; it is not a separate workspace image file.

Implementation captures:
- `web/test-results/coach-plan-dashboard-390.png` (390 × 1000 CSS/pixel viewport).
- `web/test-results/coach-plan-dashboard-card-390.png` (dashboard content crop).
- `web/test-results/coach-plan-dashboard-1440.png` (1440 × 1000 CSS/pixel viewport).
- `web/test-results/coach-plan-dashboard-card-1440.png` (dashboard content crop).

State: selected Test Fighter, active Coach technical week, assigned 15 Nov 2026,
2/2 done, 100%, zero modified/skipped, latest 15 Nov / Done / RPE 6. Density 1x.
The source is an illustrative compact mobile composition, not a full app viewport;
compare the dashboard content hierarchy rather than the existing navigation/footer.
No claim of pixel-exact scaling or a side-by-side composite is made.

## Findings and comparison history

- Initial screenshot review: [P2] existing workspace progress styles overrode the
  new bar and rendered it red. Scoped the dashboard progress selector above the
  generic workspace selector. Rebuilt, reran both browser journeys and inspected
  the new mobile content capture: the bar is now green.
- Full-view comparison against the supplied concept: fighter selection, current
  plan, status, progress and bottom creation action follow the requested order.
  The active card has two balanced Preview Plan / Progress actions. Refresh is an
  icon button; the active-plan instructional paragraph is removed. Filters omitted.
- Focused mobile-card review: count/percentage align, progress spans the card,
  modified/skipped tiles have equal weight, and the completed-week note contains
  the short latest result. All main controls have at least 44px targets.
- Typography: existing UNLXCK font retained; small uppercase context labels,
  stronger plan title, and a large tabular completion count provide hierarchy.
- Spacing/layout: one bordered card replaces nested form panels. Native app
  navigation/footer retained. Desktop uses a compact 660px dashboard column.
- Color/tokens: dark surfaces, green Active/complete state, amber Pending and grey
  Declined/Superseded/Draft; muted red creation action retains the UNLXCK language.
- Assets: real MIT Heroicons SVG files, license included, plus the existing shared
  completion icon. No new dependency or generated imagery is needed for this UI.
- Copy: all-complete message requires a nonempty schedule with every session done
  or modified. Skipped, unrecorded and zero-schedule weeks cannot claim completion.

## Validation and remaining gaps

Both mobile/desktop create-save-assign-preview-activate-progress journeys passed;
Axe checks passed for fighter assignments and the coach dashboard, page-error lists
were empty, and horizontal overflow checks passed. All six existing role/navigation
cases passed. Focused tests cover complete, modified, skipped, empty and unrelated
plan records. TypeScript, changed-file ESLint and the production build passed.
No actionable P0/P1/P2 findings remain. Existing app chrome is intentionally outside
this redesign; the supplied concept does not define it.

final result: passed
