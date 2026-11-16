# Coach self-signup

New members can choose **Athlete** or **Coach** before continuing with email or
Google. Athlete remains the default. The existing access-code gate, email
verification, minimum-age check and Terms acceptance remain in place.

Email signup sends the choice with the existing authenticated compliance
request. Google and email-link callbacks preserve a whitelisted
`account_type=athlete|coach` intent through `/login` and `/consent`. The callback
parameter is not a role claim: only the authenticated backend may select the
stored profile role. Auth user metadata is not used to grant privileges.

The service-only `select_signup_account_type` RPC serializes selection under a
profile row lock. It accepts only athlete/coach for an approved profile created
within 24 hours that has no athlete plans, intake, tracking or injury records.
The selection is immutable and retries of the same choice are idempotent.
Existing administrators keep their role. Existing athlete and coach accounts
are not silently converted at login. Browser clients cannot execute the RPC or
change the selection marker. Athlete performance access still requires an
accepted coach connection and separate optional health-sharing consent.

A new Tuta member may receive a pending athlete invitation while the initial
profile is being created. Choosing Coach closes that pending invitation using
the authenticated email from `auth.users`, rather than an editable profile email.

## Changed areas

- `web/components/auth-form.tsx` / `.module.css`: compact two-option picker,
  email choice and callback intent; coach signup omits own-health consent.
- `web/components/compliance-gate-screen.tsx`: complete the Google/link choice.
- `api/models.py`, `api/routes/profile.py`, `api/store.py`: validate the explicit
  account type, save age/Terms first, execute the restricted RPC, refresh role.
- `supabase/migrations/20261116003447_coach_self_signup.sql`: server-owned
  selection timestamp, guarded transition and service-only RPC.
- Type declarations, isolated PostgreSQL assertions, backend tests and signup
  browser fixtures are updated with the contract.

The migration has been applied to the connected Supabase project. Deploy the
backend and frontend together; older backend versions ignore the new optional
request field and cannot complete coach self-enrolment.

## Verification

- Production build and TypeScript passed; Python import checks passed.
- Backend profile/bootstrap/compliance suites: 163 passed. The focused signup
  suite, including a subsequent metadata-spoofing test, passed all six tests.
- 55 isolated PostgreSQL assertions passed across coach access, invitations and
  enrolment. No production users were created or altered for tests.
- 49 existing frontend auth/compliance regression tests passed.
- 22 Chromium checks passed: email OTP and immediate sessions, Google callback,
  existing-account login, invite admission, rejected codes, mobile layout and
  WCAG accessibility. The mobile signup screenshot is recorded under
  `web/test-results/coach-signup-layout-*/coach-signup-mobile.png`.
- Targeted ESLint found zero errors; the auth form retains existing effect
  warnings. Database advisors reported no findings for the new signup functions.
