# Hetzner backend deployment

This is the live production backend. It runs the FastAPI API, persistent generation worker, and Caddy on one Hetzner CX23 from `/opt/unlxck` on the `Main` branch.

## Architecture

- `api`: serves normal application traffic and only creates generation jobs.
- `worker`: polls Supabase and processes one generation job at a time.
- `caddy`: exposes HTTPS and proxies requests to the API.

The API must not run plan generation in-process. spaCy is disabled only in the API container and remains enabled in the worker.

## Required files on the server

Production uses `/opt/unlxck/.env.production`. Create it during first setup, protect it with `chmod 600`, and never commit it.

Required values include:

```env
API_DOMAIN=api.example.com
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_ANON_KEY=
OPENAI_API_KEY=
UNLXCK_ADMIN_EMAILS=
APP_CORS_ORIGINS=https://your-frontend.example.com
APP_ENV=production
UNLXCK_ENV=production
SENTRY_DSN=

# Web Push notifications. Both keys must come from the same VAPID key pair.
UNLXCK_VAPID_PRIVATE_KEY=
UNLXCK_VAPID_PUBLIC_KEY=
UNLXCK_VAPID_SUBJECT=mailto:ops@example.com
UNLXCK_PUSH_SITE_URL=https://your-frontend.example.com
UNLXCK_MORNING_PUSH_ENABLED=1
UNLXCK_MORNING_PUSH_LOCAL_HOUR=7
UNLXCK_MORNING_PUSH_CUTOFF_LOCAL_HOUR=11
UNLXCK_MORNING_PUSH_SWEEP_INTERVAL_SECONDS=600

# Transactional plan-ready email via Resend (server-only).
RESEND_API_KEY=
PLAN_READY_FROM_EMAIL="UNLXCK <notifications@unlxck.com>"

# Exercise demo videos: daily YouTube availability / Made for Kids check.
YOUTUBE_DATA_API_KEY=
```

Retain the generation timeout, rate-limit, feedback, and Stage 2 variables from `.env.example` that are used in production.

Protect the file:

```bash
chmod 600 .env.production
```

## Activate Web Push notifications

The Web Push implementation is already part of the API, worker, PWA service worker, and Supabase schema. It remains silently disabled until both VAPID keys are present in `.env.production`.

Generate one VAPID key pair on a trusted machine or directly on the server:

```bash
npx web-push generate-vapid-keys
```

Copy the matching public and private keys into `.env.production`:

```env
UNLXCK_VAPID_PRIVATE_KEY=<private-key>
UNLXCK_VAPID_PUBLIC_KEY=<public-key>
UNLXCK_VAPID_SUBJECT=mailto:ops@example.com
UNLXCK_PUSH_SITE_URL=https://your-production-pwa-origin.example.com
```

Rules:

- Keep the private key server-only. Never commit it, expose it through a `NEXT_PUBLIC_*` variable, or place it in the frontend deployment.
- Keep the same key pair across deployments. Replacing the pair invalidates existing browser subscriptions and requires users to opt in again.
- Set `UNLXCK_PUSH_SITE_URL` to the canonical origin from which users install the PWA.
- The current deployment workflow preserves `.env.production`; with the present architecture the VAPID keys belong on Hetzner, not in the repository.

Recreate the API and worker containers so they read the new environment values:

```bash
cd /opt/unlxck
docker compose up -d --force-recreate api worker
docker compose ps
```

Verify the backend sees a complete key pair without printing either key:

```bash
docker compose exec api python -c "from api.services.push_notifications import push_notifications_configured; assert push_notifications_configured(); print('web push configured')"
docker compose exec worker python -c "from api.services.push_notifications import push_notifications_configured; assert push_notifications_configured(); print('web push configured')"
```

Then validate the full flow:

1. Sign in to the production PWA and open **Settings → Notifications**. The control must no longer say that notifications are disabled on the server.
2. Install the PWA first on iPhone, then turn notifications on and grant browser permission.
3. Confirm a row appears in Supabase `push_subscriptions` for that device.
4. Complete a real plan-ready flow and confirm the notification opens the correct plan.
5. Confirm the morning check-in nudge arrives once during the device-local configured window and does not repeat that day.

There is currently no dedicated admin test-send endpoint, so production validation uses an opted-in device and the real plan-ready or morning flow.

## Resend: plan-ready email delivery

UNLXCK uses **Resend** for transactional plan-completion emails. This is separate from Supabase Authentication email templates. The HTML lives in `api/services/plan_ready_email.html`; `api/services/plan_ready_email.py` sends a direct `https://unlxck.com/plans/{plan_id}` link after a plan is published.

- Sending domain: `unlxck.com`, verified in Resend via **Cloudflare DNS** (DKIM and sending records). Do not alter existing website or mail records when checking verification.
- Secrets: `RESEND_API_KEY` and `PLAN_READY_FROM_EMAIL="UNLXCK <notifications@unlxck.com>"` in `/opt/unlxck/.env.production`. Use a key with email-sending permission and a verified sender domain; never commit or print the key. `FEEDBACK_FROM_EMAIL` is a supported sender fallback, primarily used by beta feedback notifications.
- Data: `supabase/migrations/20261031170000_plan_ready_email_deliveries.sql` creates the private `plan_ready_email_deliveries` queue, completion trigger, and retry claim RPC. **Apply this before deploying worker code** to a fresh environment. It does not email historical plans.
- Processing: completion inserts a queue row, and the Hetzner `worker` sends through Resend independently of plan generation. Failures retry with backoff (up to six attempts); failed deliveries remain recorded. This does not affect Supabase Auth signup/password-reset email delivery.

To configure or rotate credentials on Hetzner, edit `/opt/unlxck/.env.production` and recreate **only** the worker:

```bash
cd /opt/unlxck
nano .env.production
chmod 600 .env.production
docker compose up -d --no-deps --force-recreate worker
docker compose exec -T worker python -c "import os; assert os.getenv('RESEND_API_KEY') and os.getenv('PLAN_READY_FROM_EMAIL'); print('Resend worker configured')"
```

The check confirms only that the values are loaded, **not** that Resend accepted an email. Verify end-to-end by generating a fresh test plan, checking its message in **Resend → Emails**, and inspecting Supabase:

```sql
select status, attempts, sent_at, last_error_code
from public.plan_ready_email_deliveries
order by next_attempt_at desc
limit 10;
```

For troubleshooting, inspect only delivery-related log lines, without sharing secrets:

```bash
docker compose logs --since=15m worker | grep -E 'plan_ready_email|claim_plan_ready_email_deliveries|api.resend.com'
```

An HTTP 200 from `claim_plan_ready_email_deliveries` proves the queue RPC is available; it **does not** prove an email was sent. Check for a `sent` delivery row and a corresponding Resend message. If email settings are missing, the worker cannot send; if the domain loses verification, check Resend/Cloudflare before retrying.

## Instagram outreach automation (operator workstation)

UNLXCK outbound DMs and cold follow-ups are **local Power Automate Desktop flows on the operator's Windows PC**, **not** Hetzner services. Deploying this repository does not install, schedule, or update those desktop flows. Notion is the CRM; the Hetzner follow-up sync calculates eligibility and due dates, but **does not send Instagram messages**. See [Notion follow-up sync](notion-followup-server-sync.md).

### Local files and flows

| Item | Local location / purpose |
| --- | --- |
| `UNLXCK Outbound` | Power Automate Desktop flow for initial outreach |
| `UNLXCK Follow-Ups` | Separate duplicate of the flow for cold follow-ups |
| `prospects.csv` | `C:\\Users\\Micha\\Downloads\\prospects.csv`; fresh initial-outreach export |
| `followups.csv` | `C:\\Users\\Micha\\Downloads\\followups.csv`; generated on each follow-up run |
| `unlxck_mark_contacted.py` | Local outbound eligibility, send-attempt journal, Contacted/date update, and `--hold` |
| `unlxck_followups.py` | Local due-follow-up export, eligibility, send-attempt journal, follow-up count/date update, and `hold` |
| `NOTION_TOKEN` | Windows **User** environment variable; never commit or log its value |

The PowerShell actions currently invoke the Windows Python 3.12 interpreter at `$env:LOCALAPPDATA\Programs\Python\Python312\python.exe`. If Python moves, update all actions consistently.

### Outbound workflow

1. Read `prospects.csv`, attach to an already-authenticated Instagram Chrome tab, set `SendCount = 0`, and iterate rows with `Stage (AI Fills First) = Ready to Send`.
2. Before navigation, query **live Notion** with `unlxck_mark_contacted.py --check PROFILE_URL`. If not READY, skip; never rely on an old CSV alone.
3. After navigation, wait for the profile to load. If Instagram shows `Sorry, this page isn't available.`, call `--hold PROFILE_URL` to set `Review Hold` in Notion, then continue to the next row. A missing Message button also causes a skip, but does **not** automatically set Review Hold.
4. If messaging is possible, populate `Draft DM`, journal `--attempt PROFILE_URL`, click Send, increment `SendCount`, and update Notion with the final script (sets `Contacted` and `Date Contacted`).
5. `SendCount >= 50` exits the loop. This is **50 button attempts per run**, not a persistent calendar-day allowance. Do not restart blindly.

### Cold follow-up workflow

1. The first action runs `unlxck_followups.py export`, creating a fresh `followups.csv` from live Notion; the CSV reader must use this path with headers enabled.
2. Only `Contacted` athletes whose follow-up is due are exported. The normal export includes **follow-up 1 only**. Second follow-ups require `export --include-second` after checking the message's named-athlete claim.
3. Each row carries `Verified Profile URL`, `Copy Follow-Up`, `Step`, and `Guard` (message hash). Before sending, `check PROFILE_URL STEP GUARD` revalidates the live stage, scheduled time, counter, dates and copy; a non-READY result skips the row.
4. Use the same unavailable-page check; `hold PROFILE_URL STEP GUARD` sets `Review Hold` and skips. Otherwise populate `Copy Follow-Up`, record `attempt PROFILE_URL STEP GUARD`, click Send, wait, then call `mark PROFILE_URL STEP GUARD` to increment `Follow-Ups Sent` and record the correct `Follow-Up 1/2 Sent At` and `Last Follow-Up Sent`.
5. The copied flow retains the 50-attempts-per-run cap. It does not use `Draft DM` or mark a follow-up as a new `Contacted` event. The Notion worker computes the next safe due date; never advance follow-up counters to force eligibility.

### Operational checks and limitations

- **No automatic Instagram reply detection.** Check the conversation and update Notion to `Replied` or the appropriate opt-out/rejected stage before any follow-up. A `Contacted` CRM label alone is not evidence of no reply.
- A successful Send-button action and a two-second wait **do not prove delivery**. The present flows can update Notion without independent delivery confirmation. Supervise initial runs and reconcile sent messages with CRM updates; do not claim unattended delivery verification.
- The local attempt journals prevent automatic retries when a send is uncertain. Preserve them when replacing helper scripts; never remove a journal to resend without manual reconciliation.
- Configure browser and Notion action errors to **stop**, not retry or silently continue. Confirm that `Review Hold` writes work against a test record. Instagram blocks or login changes require manual intervention.
- Signup/onboarding follow-ups for `Replied`/`Accepted` are a **separate warm sequence** in Notion. `Details Sent At` must reflect an actual acknowledged send date; do not infer it from the record's update timestamp.
- Keep development repositories outside OneDrive-backed Documents (for example `C:\\GitHub`) to avoid filling OneDrive storage; this does not change the deployment path on Hetzner.

## Server preparation

1. Create a Hetzner CX23 using Ubuntu 24.04 and an SSH key.
2. Allow inbound TCP 22, 80, and 443 only. Restrict SSH by source IP where practical.
3. Install Docker Engine and the Compose plugin from Docker's official Ubuntu repository.
4. Enable unattended security updates.
5. Add 2 GB swap as emergency protection; swap is not a substitute for sufficient RAM.

Example swap setup:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## First deployment

1. Clone the repository and check out the deployment branch:

```bash
git clone https://github.com/vitalvightz/unlxck-gpt-webhook.git
cd unlxck-gpt-webhook
git checkout Main
```

2. Create and populate `.env.production` in the repository root before starting the containers (see **Required files on the server** above). The API, worker, and Caddy services all read this file, so `docker compose` fails to start without it.

3. Build and start the services:

```bash
sudo docker compose build
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs -f api worker caddy
```

Point the API domain's A record to the server IPv4 address before starting Caddy.

## Validation

```bash
curl -fsS https://$API_DOMAIN/health
sudo docker stats
sudo docker compose logs --tail=200 api worker
```

Before production cutover:

1. Test at least 15 users navigating concurrently.
2. Submit three generation jobs close together.
3. Confirm exactly one job runs while the others remain queued.
4. Confirm navigation remains responsive during generation.
5. Record API, worker, child-process, total RAM, and swap peaks.
6. Reject the cutover if an OOM kill or unexplained restart occurs.

## Automated deployment (GitHub Actions)

`.github/workflows/deploy-hetzner.yml` deploys automatically on every push to
`Main` (including merged pull requests) and can be run on demand with
**workflow_dispatch**. Concurrent runs are serialised so two deployments can
never touch `/opt/unlxck` at the same time.

### How the exact commit reaches the server

The server does **not** fetch from GitHub. Instead:

1. The runner checks out the exact `github.sha` with full history.
2. It packages that commit as a self-contained **git bundle**.
3. The bundle is streamed to the server over the existing SSH connection.
4. The server imports the commit from the local bundle and `git reset --hard`
   to it, then verifies `HEAD == github.sha`.

This means deployment does not depend on the server's git URL/credential
configuration and needs no GitHub token or deploy key on the server. If GitHub
is unreachable, the runner's checkout fails before anything on the server
changes, so a failed deploy is always fail-safe.

### Required GitHub Actions secrets

| Secret | Purpose |
| --- | --- |
| `HETZNER_HOST` | Server hostname or IP for SSH. |
| `HETZNER_USER` | SSH user (must be able to run `docker compose`). |
| `HETZNER_SSH_PRIVATE_KEY` | Private key for that user. |
| `HETZNER_KNOWN_HOSTS` | `ssh-keyscan` output pinning the server host key (strict host-key checking is enforced). |

### One-time server requirements

- `/opt/unlxck` is a git checkout of this repository on `Main`.
- `/opt/unlxck/.env.production` exists and is `chmod 600`. It is untracked, so
  `git reset --hard` never overwrites or deletes it; the workflow aborts if it
  is missing.
- The SSH user is in the `docker` group (the workflow calls `docker compose`
  without `sudo`).
- Docker Engine and the Compose plugin are installed.

### Deploy, validation, and rollback behaviour

Each run validates `docker compose config --quiet`, rebuilds and restarts
`api`, `worker`, and `caddy` with `docker compose up -d --build` (never
`docker compose down`, and volumes are preserved), then requires **both** the
public API health endpoint and a running `worker` container before declaring
success. On any failure it saves diagnostics to
`/opt/unlxck/deployment-logs/`, restores the previous commit, rebuilds, and
re-verifies the API and worker. A run that had to roll back still reports
failure.

## Manual update (fallback)

Use this only for a hands-on update from a shell on the server:

```bash
git fetch origin
git reset --hard origin/Main
sudo docker compose config --quiet
sudo docker compose up -d --build
sudo docker compose ps
curl -fsS https://$API_DOMAIN/health
```

## Rollback

### Roll back to a previous Hetzner commit

Stop the worker first so only one queue consumer can run:

```bash
cd /opt/unlxck
docker compose stop worker
git fetch origin Main
git reset --hard PREVIOUS_COMMIT_SHA
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl -fsS https://$API_DOMAIN/health
```

Preserve the failed deployment logs for diagnosis.

### Emergency Render fallback

Render is not part of the live system. Only resume its suspended web and worker services as a deliberate full rollback:

1. Stop the Hetzner worker.
2. Resume the Render worker and web service.
3. Change Vercel `NEXT_PUBLIC_API_BASE_URL` to the Render API URL.
4. Redeploy Vercel and verify the public flow.

Never run the Render and Hetzner workers against the production queue at the same time.
