# Coach workspace V1

The coach workspace lives inside the existing Next.js app, using the existing
AuthProvider, Supabase project and verified user access tokens. No performance
write endpoint is added. Athlete tracking, generation and owner-only policies
are unchanged.

## Routes and navigation

- `/coach`: coach metrics, searchable roster and invitations.
- `/coach/fighters/[athleteId]`: read-only fighter overview.
- `/settings/coach-access`: athlete invitation acceptance, optional sharing,
  consent changes, decline and revocation. Linked from Settings so it is
  reachable from the phone account button. Also available in side navigation.

Coach is an existing protected `profiles.role`. V1 does not enable coach
self-registration or self-promotion. An operator must provision an approved
coach account through the trusted service-role/database administration path;
`tools/manage_admin.py promote` grants **admin**, not coach, and must not be used
for this purpose. No live roles are changed by this implementation.

## Migration and security

Apply `supabase/migrations/20261115232832_coach_workspace.sql` through the
repository's normal Supabase migration release process before deploying the UI.
It adds one table, `coach_connections`, with participant-only SELECT RLS and no
browser INSERT/UPDATE/DELETE grants. Three public, security-invoker RPCs delegate
to narrow private-schema functions with fixed search paths:

- `coach_connections_list()` exposes the current actor's live connections.
- `coach_connection_command(...)` validates invitations and each transition.
- `coach_fighters(p_athlete_id)` projects accepted, authorised performance data.

Private functions deliberately use SECURITY DEFINER to read cross-athlete
records without broadening their RLS. Every entry point checks the authenticated
actor, approved profile, coach role or athlete identity as appropriate. The
`coach_private` schema must stay outside PostgREST's exposed schemas. Anonymous
execution is revoked. The frontend never receives service-role credentials.

Invitations use a normalised email, do not check whether an account exists and
return the same result for duplicate live invitations. Acceptance requires the
recipient's current, verified `auth.users.email`, not editable profile fields.
The athlete identity, coach identity and consent cannot be changed by direct
browser writes. Mutations lock the connection row. A coach can cancel a pending
invite or remove a fighter, but cannot accept or grant consent for them.

Training sharing is explicit in the acceptance button. Recovery and injury /
contact safety are independent unchecked options. Global health consent must
also remain enabled before health summaries can be returned. Free text notes,
medical narratives, raw plans, intake health details, nutrition, birth dates,
exercise blocks and credentials are never returned. Revocation/removal blocks
the next database read immediately, independently of JWT refresh. Existing
screens refresh on focus, manually and every 30 seconds while visible; previously
received information cannot be recalled from a coach's device.

## Reused data and definitions

| Display | Existing sources / meaning |
| --- | --- |
| Identity | `profiles` name and avatar only |
| Sport / fight | Active `plans`, falling back to latest `athlete_intakes` |
| Training / S&C | `session_completions` and non-deleted `external_training_sessions`; minimal schedule metadata from active plan |
| Sparring | Existing training occurrences plus `sparring_logs` round reports; reports do not create an additional session count |
| Recovery | Optional `today_checkins` sleep/body and `daily_checkins` readiness/fatigue/sleep |
| Injury flags | Optional open/monitoring `injury_flags`, limited to body area, severity, status and update date |
| Camp progress | Percentage of the active camp timeline elapsed, explicitly labelled; never presented as training adherence |

History covers the most recent 56 athlete-local training days with the existing
04:00 rollover, falling back to UTC for unknown timezones. Weeks start Monday.
Completed counts include `done` and `modified`, exclude `day_credit`, and retain
legacy NULL origins in line with existing training history. Explicit `skipped`
rows display as skipped; absent logs are never classified as missed training.
S&C progress requires actual scheduled strength/conditioning identities and
matching plan/day completions. No inferred sessions or denominators are added.

Active fighters have a completed training occurrence or positive-round sparring
report in the inclusive current-day / previous-six-day window. Needs attention
uses a recent explicit poor sleep/flat body, readiness <=2, fatigue >=4 or sleep
quality <=2, open shared injury flags, or recent shared heavy-contact/rocked
reports. Missing, unshared or stale check-ins do not create concern flags.
Upcoming fights counts connected fighters with a date on or after their current
training day. No production fixture data is included.

## Verification

- `python tools/test_coach_workspace.py --container unlxck-coach-test` executes
  the migration against real source table DDL in isolated PostgreSQL 17, then
  rolls back all synthetic users and rows. The container must be disposable,
  with a clean database and no pre-existing `anon`/`authenticated` roles.
- `cd web && npm run typecheck && npm run build`
- `cd web && npm run lint`
- `cd web && npm run test:unit`
- `cd web && npx playwright test e2e/coach-workspace.spec.ts` against a production
  build with matching public env variables; Chromium must be installed. The
  browser suite intercepts all external services and never writes live data.

Browser tests cover desktop/mobile sizing, search, profile navigation, empty
roster, invitations, acceptance with opt-in health sharing, consent withdrawal,
revocation, loss of access, failures and WCAG accessibility (including contrast).
Connected database catalog metadata was checked read-only for required columns
and existing RLS. The new migration is tested locally, not applied remotely.

## V1 limits

Invitations are in-app only: recipients sign in using the invited verified email
and open Settings → Coach access. No email or push system is added. Coach roles
are operator-provisioned. The roster permits 250 live invitations/connections per
coach. Long-term history, custom-programme editing, messaging, analytics and
programme generation are outside V1. Screens use English copy while retaining
the existing app's locale and theme shell.

## Files changed

- `web/app/coach/page.tsx`, `web/app/coach/fighters/[athleteId]/page.tsx`,
  `web/app/settings/coach-access/page.tsx`: entry routes.
- `web/components/coach-workspace.tsx` and `.module.css`: compact coach dashboard,
  attention list, upcoming fights, searchable roster, weekly performance,
  recovery tiles, session details and connection controls.
- `web/components/app-nav.tsx`, `web/app/settings/page.tsx`,
  `web/lib/beta-navigation.ts`, `web/lib/auth-routing.ts`: existing navigation
  integration and coach destination.
- `web/lib/coach.ts`, `web/lib/coach.test.ts`: typed RPC adapter and count rules.
- `supabase/migrations/20261115232832_coach_workspace.sql`: connection table,
  indexes, RLS, permission transitions and consent-aware projections.
- `tests/coach_workspace_permissions.sql`, `tools/test_coach_workspace.py`:
  isolated PostgreSQL migration/permission tests.
- `web/e2e/coach-workspace.spec.ts`: isolated browser flow and accessibility checks.
- `docs/coach-workspace.md`, `design-qa.md`: rollout, limits and verification notes.

## Validation results

- Production build and TypeScript passed.
- New coach components, helpers and browser tests pass ESLint with zero warnings.
  Existing navigation/settings retain their pre-existing lint warnings.
- PostgreSQL 17: 32 permission/projection assertions passed and rolled back.
- Frontend affected-area regressions: 25 passed.
- Existing Python authentication, authorization, training history and consent
  checks: 44 passed using test-only environment markers.
- Chromium: seven flows passed across 1440, 390 and 320 CSS-pixel widths.
- The full frontend unit run was interrupted after stalling in the existing
  timer-provider tests: 458 passed, zero failed, 148 cancelled. This is not a
  claim that the complete suite passed.

The final UI follows the user's supplied dashboard/profile references. Optional
explanations and connection management are collapsed; full consent wording
remains available before sharing. The supplied screenshots' illustrative
fighters and metric values are not copied into production.
