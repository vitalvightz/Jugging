# Production speed investigation, 2026-11-12

Inputs: the iPhone report (requests `2eccf887`, `43f21d61`, `bd5f2d0f`,
`d21b8e9f`). The API's own logs (where request ids live) are on the Hetzner
host and were not reachable; production evidence below comes from Supabase
edge logs (`response.origin_time` = time inside Supabase), Supabase Postgres
statistics, and read-only `EXPLAIN ANALYZE`. Local numbers are labelled.

## 1. `today_resolve` > 1,100ms (`2eccf887`): Supabase-side stalls, not our query

`today_resolve` is two sequential reads: `profiles.active_plan_id`, then the
active plan row with `planning_brief_runtime`.

Production (edge logs, last 24h):

- The active-plan read: 1,444 calls, origin p50 72ms, p90 215ms, p99 765ms,
  max 1,623ms. At 17:17:45.561 (beside the report's Today requests) it took
  1,018ms **with no other request in flight**; the identical read 2s before
  took 50ms and 2s after 136ms.
- Spikes hit every table, including tiny indexed reads: `injury_flags`
  p50 20ms / max 1,497ms, `today_checkins` p50 26ms / max 1,281ms,
  `profiles` max 1,449ms. About 1% of requests on each shape exceed 500ms.
- In Postgres (`pg_stat_statements`): `injury_flags` reads mean 1.3ms over
  39,048 calls, max 766ms; `today_checkins` mean 1.1ms, max 636ms, both with
  almost no disk reads. Tiny queries stalling without I/O points at CPU
  scheduling on the database host.
- Slow requests are both clustered (07:50:50: 57 slow requests across 16
  tables in 10s) and scattered (664 requests > 500ms over 404 separate 10s
  windows).
- Settings (`shared_buffers` 224MB, `max_connections` 60) match Supabase's
  Micro compute (shared CPU).

The query itself: `EXPLAIN ANALYZE` on the largest active plan is 31ms, all
from cache. `planning_brief_runtime` costs ~11ms per plan on average (234ms
for all 19 active plans vs 13ms reading the raw columns).

Conclusion: the 1.1s spike is an intermittent stall inside Supabase that this
read happened to hit. No code change here removes it, so none was made. One
Today build makes ~25 Data API calls; at ~1% each over 500ms, roughly one in
five Today loads meets at least one. Options outside this change: larger
compute, or fewer Data API calls per screen.

## 2. App-launch contention: duplicate profile reads (fixed)

Production, 17:16:58 (one athlete opening the app): **six identical
`profiles select=*` reads within 150ms** (243–442ms each), plus three
concurrent heavy plan reads (774, 753, 579ms; 50–70ms alone). On Micro
compute, one user's launch burst slows itself.

`ensure_profile` checked the cache, missed, and read, independently per
request. It now coalesces concurrent reads per athlete: requests arriving
while a read is in flight wait for it. Freshness is unchanged (each would
have started the same read at the same moment). A failed read or a write
during the read (cache invalidation) is never handed to waiters; they read
for themselves. `PROFILE_CACHE_TTL_SECONDS=0` still disables all reuse.

Local: six concurrent cold-cache requests made 6 reads on Main, 1 now.
(Wall time locally is the same, ~124ms, as the fake reads run in parallel; the
gain is database load, which is what slowed the production burst.)

## 3. Home's 1.74MB plan request (`bd5f2d0f`): fixed

Home read the full plan detail only to feed the fight countdown, which uses
the weeks' dates, phases and countdown labels. The stored plan is 85KB
structured + 14KB text; the API's detail response was 1.74MB.

New `GET /api/plans/{id}/calendar` runs the same read, mapping and guards as
the plan detail (so dates and phases are identical: calendar-spine, locked
content and contact reconciles all apply), then returns only the week and day
calendar fields. It skips plan-source, rehab-label and exercise-media work.
Home's existing `getWeeks` logic runs on it unchanged.

Local (test fixture, one week): detail 18,245 B vs calendar 308 B. The
production saving needs the next iPhone report to measure.

## 4. `today_sync` 528ms (`43f21d61`): not traced to one call

The sync is up to three sequential reads plus the `sync_intake_injury_flag`
write RPC when the intake changed (219 calls/day, p50 55ms, max 450ms; one
at 14:04:12 took 417ms). For this athlete, the 17:16–17:18 sync reads were
7–72ms. Without the request's time, the most likely explanation is the same
Supabase stalls as §1, or a sync that ran the write RPC. No change made.

## 5. History 585ms (`d21b8e9f`): reads under contention

Locally the History request takes 13ms, so it is not CPU. In production it
issues sequential reads (completions, external sessions, then the full active
plan row for session titles, then manual-exercise counts). At 17:16:58 these
ran during the launch burst at 298ms, 328ms and 774ms. The fixes in §2 and §3
reduce that burst; no History-specific change was made.

## 6. Missing `queue` timing: stale Caddy config (fixed)

The deploy runs `git reset --hard` (replacing `Caddyfile` with a new file),
then `docker compose up -d` (which leaves Caddy running because its compose
definition is unchanged), then `caddy reload`. `compose.yaml` bind-mounted
the single file; Docker pins a single-file bind mount to the original inode,
so the reload re-read the old Caddyfile and reported success. The
`X-Request-Start` header from #3260 never took effect.

The config now lives in `caddy/Caddyfile`, and `./caddy` is mounted as a
directory. The changed mount makes the next deploy recreate Caddy once; from
then on reloads see deployed changes. Verify on the server after deploying:

```bash
docker compose exec caddy grep -c X-Request-Start /etc/caddy/Caddyfile   # expect 1
```

## Tests

- New: `tests/test_plan_calendar.py` (calendar equals the detail's spine,
  same 404s, no card → null), profile coalescing tests in
  `tests/test_request_amplification.py`, and a mount regression test in
  `tests/test_caddy_security_headers.py`.
- `web/lib/api-schema.generated.ts` regenerated. That also fixes
  `test_generated_types_match_the_api_schema`, which failed on Main from an
  earlier `session_id` drift.
- 355 related Python tests pass. `test_push_sweep_builds_each_athletes_today_once`
  fails identically on Main.
- Web: typecheck, lint (2 warnings, both already on Main), and the
  `home-readiness`, `frontend-requests` and `navigation-revisit` e2e specs
  pass against a production build.
