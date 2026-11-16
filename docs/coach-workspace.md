# Coach workspace

A read-only coach view inside the existing Next.js app. It reuses the existing
AuthProvider, Supabase project and user access tokens, and adds no write path
to athlete training data.

## Routes

- `/coach`: roster summary, fighters needing attention, upcoming fights and the
  searchable roster.
- `/coach/fighters/[athleteId]`: one fighter: fight countdown, weekly
  performance, sparring reports, fight weight, full training history with the
  recorded exercises of each session, and recovery.
- `/settings/coach-access`: coaches invite and remove athletes; athletes accept,
  decline or disconnect. Athletes reach it from Settings.

Coach is an existing protected `profiles.role`. There is no self-registration:
an operator provisions approved coach accounts. Approved administrators can
also coach through accepted connections without changing role.

## Sharing and consent

An invitation shows the coach's name, one sentence saying what is shared, and
Accept / Decline. Accepting shares training, weight, recovery and injury status
together (`share_recovery` and `share_injuries` are both set). The athlete can
disconnect at any time, which ends access on the coach's next read.

Safeguards that remain:

- Nothing about an athlete is returned before they accept. Invitations do not
  reveal whether an account exists.
- Acceptance requires the athlete's verified `auth.users.email`; editable
  profile fields are never trusted.
- Weight, recovery and injury data are returned only while the athlete's global
  `health_data_consent` is on. Weight follows `share_recovery`; pain scores,
  rehab work, head contact and rocked reports follow `share_injuries`. Without
  it, rehab and recovery sessions appear as "Programme session".
- Free-text notes, takeaways, medical narratives, raw plans, intake health
  details, nutrition settings and birth dates are never returned.
- Coach reads are GET-only. Nothing a coach opens can be edited.
- `accepted_at` and the sharing flags record what was agreed and when.

## Database

`supabase/migrations/20261115232832_coach_workspace.sql` adds
`coach_connections` (participant-only SELECT, no browser writes) and three
security-invoker RPCs that delegate to `coach_private` functions:

- `coach_connections_list()`: the actor's pending and accepted connections.
- `coach_connection_command(...)`: invite, accept, decline, revoke, remove.
  Each transition is validated under a row lock. The `consent` action and the
  two per-category parameters remain for compatibility; the app sends both as
  true on accept and does not call `consent`.
- `coach_fighters(p_athlete_id)`: the consent-aware projection of accepted
  fighters.

`coach_private` must stay outside PostgREST's exposed schemas.

`20261116001001_coach_signup_invitations.sql` adds the service-only
`coach_invitation_rules` table. An operator can list email domains for a coach;
matching new signups receive a pending invitation. It never accepts on the
athlete's behalf.

## Training history and weight

The fighter page reuses the athlete's own History and weight tracker rather
than a second implementation. Three read-only API routes in
`api/routes/coach.py` resolve the caller's accepted connection on every
request, then call the same services the athlete's screens use:

- `GET /api/coach/fighters/{id}/training/history`: the History page
  (`history_sessions`), paged by `before_day`.
- `GET /api/coach/fighters/{id}/training/history/{source}/{entry}`: one
  session's record (`session_details`): exercises, sets, reps, load, RPE,
  duration, runs and the sparring report.
- `GET /api/coach/fighters/{id}/weight`: the athlete's weigh-ins, current and
  target weight, 7-day average and weekly pace from the nutrition workspace.
  Descriptive only; no cut guidance. Under-18s have no target.

Without an accepted connection every route answers 404, so a coach cannot
probe for accounts. In the browser the rows and the session sheet are the
athlete History components (`SessionRow`, `SessionDetails`) and the chart is
`WeightTrendChart`. These routes need the backend deployed with the frontend;
they add no migration.

## Definitions

History covers the last 56 athlete-local training days (04:00 rollover, UTC for
unknown timezones). Weeks start on Monday.

| Display | Meaning |
| --- | --- |
| Sessions | `done` and `modified` completions plus external sessions; `day_credit` excluded |
| Active this week | A completed session or a sparring report with rounds since Monday |
| Needs attention | An open injury flag, heavy head contact or a rocked report in the last 7 days, or a recent check-in with poor sleep, flat body, readiness ≤ 2, fatigue ≥ 4 or sleep quality ≤ 2 |
| Fights in 30 days | Connected fighters with a fight date in the next 30 days |
| S&C completed | Scheduled strength/conditioning sessions with a matching completion |
| Camp timeline | Share of the active camp elapsed; not training adherence |

Missing, unshared or stale check-ins never create a concern.

## Verification

- `python tools/test_coach_workspace.py --container <name>` runs both migrations
  and the permission assertions in a disposable PostgreSQL 17 container, then
  rolls back.
- `python -m pytest tests/test_coach_fighter_access.py`: connection checks,
  revocation, private-note stripping, unshared health detail and read-only
  behaviour of the coach API.
- `cd web && npm run typecheck && npm run lint && npm run test:unit`
- `cd web && npm run build && npx playwright test e2e/coach-workspace.spec.ts`
  against a build with the placeholder public env values. The browser tests
  intercept every external request.

## Limits

Invitations are in-app only; there is no email or push notification. A coach
can hold 250 pending or accepted connections. Programme editing, messaging and
long-term analytics are out of scope. Coach screens use English copy.
