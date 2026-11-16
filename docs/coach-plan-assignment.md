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

Three existing failures were independently reproduced on unchanged Main
(`f96926ca`): `test_readiness_and_severe_injury_still_gate_execution` (started
session returns 201 instead of 409), and two builder assertions expecting the
old “Suggested 100 kg” copy. They are not fixed in this focused assignment PR.
The injury-execution failure needs a separate investigation; the assignment
change continues to use the existing execution and injury guards.

An additional existing athlete browser smoke (`Saved links, mixed exercises and
compact doses (390px)`) also fails at its dose-label assertion: it expects
`4 × 8 · 80 kg · rest 120s`, while the unchanged session renderer displays
`4 × 8 · rest 120s` and a separate `80 kg` label. The new coach/fighter browser
journeys and all six coach/admin/athlete navigation cases pass.
