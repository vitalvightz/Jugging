# Supabase auth emails and redirect URLs

Password reset, email sign-in links, and signup confirmations are all sent by
Supabase Auth. Two things decide whether an athlete lands in UNLXCK or on a
stranger's error page: the **origin baked into the link** and the **Supabase URL
configuration**. Both must be right — fixing one without the other still breaks.

## Why reset links used to open a Vercel page

The frontend builds the `redirect_to` for every auth email from
`web/lib/site-url.ts`. It used to fall back to `window.location.origin` whenever
`NEXT_PUBLIC_SITE_URL` was unset, so a reset requested from a
`unlxck-<hash>-<team>.vercel.app` deployment host emailed that host back to the
athlete. Those hostnames are per-deployment and sit behind Vercel's deployment
protection, so the athlete opened the link and got Vercel's own branded gate
instead of UNLXCK.

An emailed link is opened minutes or hours later, from another device, so an
origin that merely works in the current tab is not good enough.
`buildAuthRedirectUrl()` therefore accepts an origin only from:

1. **`NEXT_PUBLIC_SITE_URL`** — an explicit operator decision, always honoured
   (including a `*.vercel.app` value, since the stable production alias is not
   deployment protected).
2. **`NEXT_PUBLIC_VERCEL_PROJECT_PRODUCTION_URL`** — used only when it is a
   custom domain. A `*.vercel.app` value here is rejected, because it is
   auto-detected rather than chosen. Treat this as a bonus, never the plan: it
   exists only when the project has *Automatically expose System Environment
   Variables* enabled, so it may simply be absent.
3. **`window.location.origin`** — local development hosts only (`localhost`,
   `127.0.0.1`, `[::1]`, `*.localhost`).

Anything else yields `undefined`, and supabase-js falls back to the project's
Site URL. **Landing on production is always better than landing on a protected
preview host, so refusing to answer is the safe failure.** A preview deployment
with no `NEXT_PUBLIC_SITE_URL` now sends athletes to production rather than to a
dead preview URL, and logs a console warning naming the missing variable.

Values are also scheme-checked. A configured value that already carries a scheme
must use `http` or `https`; only genuine bare hostnames get `https://` added.
Blindly prefixing turned `ftp://app.unlxck.com` into `https://ftp` and
`mailto:ops@unlxck.com` into `https://unlxck.com` — silently wrong hosts in a
link that had already been emailed.

There is a second, independent cause with the same symptom: **Supabase silently
rewrites any `redirect_to` that is not on the Redirect URLs allow list to the
project Site URL.** If Site URL is still a `*.vercel.app` value, every link lands
there no matter what the frontend sends. Check both.

## The app recovers from a wrong landing route

Supabase, not the app, picks where a recovery link lands: any `redirect_to` that
is not on the Redirect URLs allow list is rewritten to the project Site URL. When
that happened in production the tokens were consumed on the homepage, supabase-js
signed the athlete in, and the reset silently never happened — they just found
themselves logged in.

`web/components/password-recovery-redirect.tsx` now listens for
`PASSWORD_RECOVERY` app-wide, records a tab-scoped marker
(`web/lib/password-recovery.ts`) and sends the athlete to `/reset-password`,
which accepts that marker as proof. So the flow completes from any route in the
app even when the configuration below is wrong.

Two properties keep that safe rather than a repeat of the `?code=arbitrary`
bypass: the marker is written **only** from the Supabase event, never from URL
contents, and it is bound to one user id with a 15-minute TTL.

Fix the configuration anyway — the safety net costs a redirect, and it cannot
help a link that never reaches the app at all.

## Required configuration

### Vercel project environment variables

| Variable | Value |
| --- | --- |
| `NEXT_PUBLIC_SITE_URL` | The production frontend origin, no trailing slash |

Set it for **Production, Preview, and Development**. This is the only reliable
input — everything else is a fallback that may legitimately refuse to answer.

### Supabase dashboard — Authentication → URL Configuration

- **Site URL**: the production frontend origin.
- **Redirect URLs** must include every path the app passes to `redirectTo`:
  - `<origin>/login` — magic link and signup confirmation
  - `<origin>/reset-password` — password recovery
  - `http://localhost:3000/**` for local development

Adding a new `buildAuthRedirectUrl()` path in the frontend without adding it here
reintroduces the bug, because the unlisted path falls back to Site URL.

## Applying the branded email templates

`supabase/templates/` holds the UNLXCK-branded HTML. They replace Supabase's
unstyled defaults.

| File | Dashboard template |
| --- | --- |
| `confirm-signup.html` | Confirm signup |
| `magic-link.html` | Magic Link |
| `recovery.html` | Reset Password |
| `email-change.html` | Change Email Address |
| `invite.html` | Invite user |

To apply: **Authentication → Emails → Templates**, pick the template, paste the
file contents into the message body, and save. Re-paste after editing a file
here — the dashboard holds the live copy, so the repo is the source of truth
only if it is kept in sync.

Suggested subject lines:

| Template | Subject |
| --- | --- |
| Confirm signup | Confirm your UNLXCK account |
| Magic Link | Your UNLXCK sign-in link |
| Reset Password | Reset your UNLXCK password |
| Change Email Address | Confirm your new UNLXCK email |
| Invite user | You have been invited to UNLXCK |

### Template constraints

- **No remote images.** The wordmark is letter-spaced text so it renders even
  when a client blocks remote content. Do not swap in a hosted logo.
- **Inline styles only.** Gmail strips `<style>` blocks in several clients.
- **Table layout.** `flex` and `grid` are unreliable across email clients.
- Link-based templates use `{{ .ConfirmationURL }}` for the button and
  visible fallback URL. Signup confirmation is deliberately code-only.

### Signup confirmation code

Signup confirms the email with a one-time code typed into the signup screen,
so a new athlete never has to leave the page, follow a link and log in again.
When `signUp` returns no session (email confirmation is on), `AuthForm`
switches to a code step and calls `verifyOtp({ email, token, type: "email" })`;
the returned session records the signup consents and opens the first-plan flow,
exactly like a signup that returns a session straight away.

- `confirm-signup.html` shows only `{{ .Token }}`. The link option was removed:
  opening it in another browser loses the private-beta invite cookie and sends
  newly confirmed users back to the access-code gate. Athletes enter the OTP
  on the signup screen in the browser where they entered their invite code.
  **Paste the updated template into the Supabase dashboard** to apply this.
- The code input accepts 6–10 digits to match **Authentication → Providers →
  Email → Email OTP length**.
- "Resend code" calls `auth.resend({ type: "signup" })` with a fresh Turnstile
  token and a 60 s cooldown, matching Supabase's per-address email limit.
- Keep **Confirm email** on. Turning it off would let anyone register an
  address they do not own; the owner signing in later by email link would then
  share an account whose password the other person knows, on a product that
  stores health data.

## Verifying a change

1. Request a reset from production and confirm the link host is the production
   domain, not `*.vercel.app`.
2. Open the link and confirm the UNLXCK reset form renders.
3. Open the same link a second time. It must show "This link has expired or has
   already been used" on the UNLXCK page rather than a blank form.
4. Repeat 1–3 for the email sign-in link, which lands on `/login`, and for the
   signup confirmation link. Then sign up with a fresh address, enter the
   emailed code on the signup screen, and confirm it lands on the first-plan
   flow without a separate log in.
5. While signed in, open `/reset-password` directly, and again with
   `?code=arbitrary`. Both must refuse the form and offer Settings.

`/reset-password` opens only on proof that Supabase verified a recovery link: a
`PASSWORD_RECOVERY` event, a stored session carrying the access token the URL
supplied, or a code Supabase accepts in exchange for a session. A
credential-shaped parameter is not proof — supabase-js keeps an existing session
when it fails to consume a URL, so pairing one with `?code=anything` would
otherwise open a form that never asks for the current password. `/settings` owns
ordinary password changes and does ask.

Steps 2–5 are covered automatically by `web/e2e/auth-links.spec.ts`
(`npm run test:e2e`), including both bypass vectors. Only the real email
delivery in step 1 needs a human.
