# Coach plan building and assignment (Phase 3)

Coaches select one accepted fighter connection in `/coach/plans`, open the existing
custom builder, save a draft, return to the list, and assign it. Sessions,
exercises, doses and validation use the athlete builder and custom-plan services.
A sent draft is immutable. Create a new draft for another prescription.
Private author-owned videos/community submissions cannot be transferred; the coach
builder offers catalogue exercises, public community revisions and manual work.

Assignments appear in the fighter's existing `/plans` area with the coach's name,
assignment date and complete prescription. Preview exposes Activate and Decline.
The existing plan-switch dialog asks whether to pause or replace an overlapping
active plan. Sending a plan never publishes it or changes the active pointer.

## Storage and consent

`20261116145410_coach_plan_assignments.sql` extends `custom_training_plans`:
`athlete_id` stays the draft author, `fighter_id` is the intended recipient, and
assignment/consent timestamps, coach attribution and `published_plan_id` record
consent. Existing personal drafts have null assignment fields and retain their
existing API behavior. No tracking tables, new planning engine or dependencies.

The two service-role-only, security-invoker RPCs lock and recheck approved roles,
recipient ownership and accepted connections. Coach writes serialize with
connection revocation. Create uses a stable request UUID and assignment retries
return the same sent draft. No browser write grants are added.

Activation calls the existing `set_active_plan` rules through a small store adapter.
Only after those rules pass does it publish the ordinary custom open plan, initially
ineligible (`review_required`). The consent transaction releases it to `ready`,
records acceptance, switches the profile's single pointer, and optionally archives
the previous plan. It compares the pointer observed before validation. Competing
activations must refresh; a failed archive rolls back the entire consent switch.
An interrupted request can leave an inert compiled row; retries reuse its deterministic
ID, and it cannot become active via the ordinary endpoint or sole-plan fallback.
It is excluded from the fighter's ordinary saved-plan list.

Pending / Active / Declined / Superseded are derived from consent and the existing
active pointer. Drafts are shown only to their author. Both parties see sent plans;
only the recipient can accept or decline. An acceptance retry never restores a
subsequently superseded plan. Revocation blocks new coach actions and acceptance
of pending assignments, while accepted plans and History remain athlete-owned.
Normal athlete/admin plan deletion remains available: the assignment retains its
consent history, loses its plan link and becomes Superseded.

Coach progress uses the existing projected calendar and coach-safe training records:
completed versus scheduled sessions this week, modified/skipped sessions, upcoming
work and the latest recorded result. Modified sessions count as completed. Existing
health-sharing choices still filter the schedule/details. The progress link opens
the existing paginated History, calendar, exercise results and coach feedback.
Roster refreshes update statuses; Refresh plans retries reads. No new completion
or analytics records are written. The summary is deliberately weekly; full History
remains available in the fighter overview.

## Deployment order (not performed by this PR)

1. Apply the existing Coach Workspace, signup, weight-consent, session-logging and
   feedback migrations first, through `20261116024150_coach_feedback.sql`.
2. Apply `20261116145410_coach_plan_assignments.sql` through the normal reviewed
   migration process. It is additive, has no data backfill, and changes no existing
   personal drafts, plans or training records.
3. Deploy the backend with the new endpoints and scoped personal-draft queries.
4. Deploy the frontend. Do not expose the coach controls before the backend/schema.

Rollback application code before removing schema. Do not drop assignment columns
once coaches have created drafts or fighters have accepted plans. Production
migrations and deployment were not run for this change.

## Validation

Use test-only environment markers for Python commands (`APP_ENV=test`,
`UNLXCK_ENV=test`, `ENVIRONMENT=test`, `NODE_ENV=test`).

- `python -m pytest tests/test_coach_plans.py tests/test_coach_fighter_access.py tests/test_custom_plans.py tests/test_active_plan.py`
- `COACH_PLAN_TEST_DATABASE_URL=<disposable localhost PostgreSQL DSN> python -m pytest tests/test_coach_plan_concurrency.py`
- `python tools/test_coach_workspace.py --container <disposable PostgreSQL container>`
  loads the full source schema and coach migration stack, checks privileges,
  revocation, ownership, acceptance, decline and deletion, and rolls back.
- `python -m pytest tests/test_custom_open_plans.py tests/test_custom_plan_publication_postgres.py`
  (`CUSTOM_PLAN_TEST_DATABASE_URL` enables real publication persistence checks).
- `cd web && npm run typecheck && npm run build` with the existing placeholder
  Supabase/API settings used by browser tests; generated API types are checked
  with `python tools/generate_api_types.py --check`.
- Focused frontend unit tests: `components/plan-assignments.test.tsx`,
  `components/plan-switch-dialog.test.tsx`, `components/plans/plans-page.test.tsx`,
  `components/custom-plan-builder.test.tsx` and `lib/coach.test.ts`.
- `npx playwright test e2e/coach-workspace.spec.ts` includes coach creation → save →
  assign → fighter preview/activation → coach progress at 390px and 1440px, plus
  role navigation checks. Axe checks the assigned-plan UI.

The existing `test_readiness_and_severe_injury_still_gate_execution` failure was
independently reproduced on unchanged Main (`f96926ca`): a started session returns
201 instead of 409. It needs a separate injury-execution investigation. Phase 3.5
retains the existing guards. Two stale builder assertions and the athlete browser
smoke's dose-label ordering assertion now match the existing separate weight label;
these tests pass with the advanced builder.

## Compact coach dashboard

Coach Plans uses a current-plan card with a weekly progress bar/percentage,
modified/skipped tiles, compact latest result and paired Preview/Progress actions.
Active is green, Pending amber, and other states neutral. Refresh is an accessible
icon button. Desktop uses the full workspace width, a top creation toolbar,
side-by-side progress/stats and three actions; mobile keeps creation below the cards. A nonempty, fully completed
week shows “All sessions complete this week”; skipped or unrecorded sessions do
not. Desktop offers All Plans, Active, Drafts and Past filters with live counts;
mobile shows these only when multiple plans make them useful. Pending assignments
remain accessible under All Plans. View Schedule expands the existing coach-safe
projected training sessions without introducing new scheduling logic. Icons are MIT Heroicons
assets with their license retained under `web/public/icons/coach/`.

## Phase 3.5: dated custom plans

The same builder now defaults new coach and athlete drafts to a four-week dated
calendar, starting on the next Monday (today when Monday) in the athlete's timezone.
Start date and duration are editable, from one to twelve weeks. The API also permits
partial weeks within an 84-day range. Existing drafts default to `weekly` and keep
their repeating behavior. Switching modes creates a copy rather than updating the
saved original. Shortening a range requires confirmation before removing sessions;
shifting the start date confirms moving sessions by the same offset.

Week/month views, date selection and the existing session/exercise sheets share one
draft. Desktop shows calendar and selected-day editor side by side. Mobile stacks
them; select a date and use the selected day's Add session action. Drag/drop moves
sessions on desktop; the session Date field is the keyboard/touch alternative.
Copy session and Duplicate selected week create fresh occurrence IDs. Each date
retains its own exercise doses. Quick dose editing and exercise ordering reuse the
existing dose model. Undo/redo keeps up to 100 draft changes in memory and resets
after saving; it is not a server version history.

`20261116162904_dated_custom_plans.sql` adds only `schedule_mode`, `start_date` and
`end_date` to the existing draft table and extends the service-only coach command
to persist them. Session JSON gains `scheduled_date`; API validation derives weekday,
checks range/unique occurrence IDs, and limits each calendar week to 28 sessions.
No new plan or tracking tables are introduced. Legacy weekly publication identity
ignores the new null date field, preserving deterministic retries for existing drafts.

Dated publication uses the existing custom exercise/session compiler and ordinary
structured-plan store. It writes actual dated weeks without a repeating Open Plan
specification. Today, Plan Detail, completion, History and coach progress use those
same dated records; nothing repeats beyond the authored range. The end date is also
checked by the shared activation eligibility rules. Weekly publication remains on
the existing Open Plan projection path.

Create revised draft copies a sent prescription into a new coach draft. Saving or
assigning it never mutates the accepted plan or existing results. The fighter must
preview and activate the new assignment through the existing consent and overlap
rules. Connection and recipient checks, immutable sent drafts, transactional consent
and concurrent-activation checks remain in the existing services/RPCs. Supersets,
per-set prescriptions, AI generation and automatic progression remain out of scope.

### Phase 3.5 deployment order

1. Confirm the Phase 3 assignment schema is present. Its production application was
   explicitly requested separately; do not replay the old migration under a second
   version when the hosted migration history already records it.
2. Apply `20261116162904_dated_custom_plans.sql` using the reviewed migration process.
   It is additive for existing drafts, with default weekly mode and no data backfill.
3. Deploy backend, then frontend. The old application remains compatible during the
   schema-first rollout. Do not roll back to an application version that interprets
   dated drafts as weekly once dated drafts exist; retain the columns and records.

The Phase 3.5 migration has only been exercised against disposable local PostgreSQL.
It has not been applied to production; no merge or deployment was performed.

### Phase 3.5 validation

- `tests/test_dated_custom_plans.py`: contract/range validation, distinct doses and
  session identity across weeks, publication retry, legacy identity, expiry, Today,
  completion History and fighter-approved coach revision.
- Coach/fighter permissions, custom plan, activation, publication and real PostgreSQL
  race suites, plus the existing full-schema SQL permission harness.
- Calendar/date helper tests and shared builder, assignment, plan-switch, coach
  progress and Plans component tests.
- `web/e2e/dated-custom-builder.spec.ts`: coach and athlete at 390px/1440px; copy,
  undo/redo, moving dates, duplicate week, independent doses, month view, save/reopen,
  coach revision copying, Axe, runtime errors and horizontal overflow.
- Existing weekly-builder and coach → fighter assignment browser regressions.
- TypeScript, generated API types, changed-file lint and production build.

Local browser tests use intercepted test fixtures and placeholder public settings.
The requested logged-in production browser plugin was unavailable in this session;
these checks do not claim to inspect or modify the live account.
