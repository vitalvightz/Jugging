# Request queue spikes and the post-#3263 Today slowdown

iPhone report after #3263:
- `queue`: 891ms (`/api/plans`), 916ms (`/api/plans/active`) and 730ms
  (`/api/analytics/events`), against under 15ms before.
- Today stages: `today_core` 393–659ms, `today_reads` 223–311ms,
  `today_sync` 184–204ms.
- Today's response: 12,478 bytes, against 3,983.

The API's own logs, where request ids c7f53e27, 16065bb7 and 1f57dab1 are
recorded, are on the Hetzner host and were not reachable. Production evidence
below comes from Supabase edge logs and GitHub's deploy history; local numbers
are labelled.

## 1. Queue spikes: Supabase reads on the event loop (fixed)

`queue` is the time from Caddy stamping a request to the app's first
middleware running. With one process, it only grows when the asyncio event
loop is busy.

`get_active_generation_job`, `get_latest_generation_job` and
`get_generation_job` (`api/routes/generation_jobs.py`) were `async def`. They
moved the job read to a thread, then called `_job_response`
(`api/generation_job_helpers.py`) on the loop. For a completed job,
`_job_response` calls `store.get_plan`, which resolves to a `plans` status
read, and can also call `store.get_latest_plan`. Both are synchronous
Supabase round trips, so every request arriving meanwhile waited.

- Production, 19:04:50–51: `get_latest_generation_job_status_v2` was followed
  by `plans?select=id,status,stage2_status,intake_id`, the on-loop read. The
  same pair repeated at 19:04:52.
- Local, Home's launch requests fired concurrently with asyncio slow-callback
  detection: `get_plan_status` and `get_plan` ran on the loop, giving two
  106ms loop blocks at 50ms per read.
- Local, with a slow 300ms Supabase read (as during a stall): requests to
  `/api/plans` and `/api/plans/active` arriving while the job route ran
  queued for **555ms on Main and 4ms with this change**.

The same pattern was on:
- `delivery_webhook` (`api/routes/reactivation.py`): two Supabase reads and a
  write per provider callback, arriving at any time.
- `list_admin_review_plans` and `generate_admin_athlete_plan_from_latest_intake`
  (`api/routes/admin.py`): admin-only, but they stalled everyone's requests.

Fix:
- The three generation-job handlers are plain `def`, so FastAPI runs each
  whole handler on its threadpool. This deletes the `asyncio.to_thread`
  wrappers.
- `list_admin_review_plans` is plain `def` too.
- The webhook keeps its async body read and signature check, then hands its
  database work to one `asyncio.to_thread` call.
- The admin generate route moves its three synchronous calls onto threads.

Responses are unchanged.

## 2. Today slowdown: cold process and a slow Supabase minute, not #3263

- **Deploy timing.** The #3263 deploy finished at 19:01:14; the report's
  requests ran at 19:04–19:05, in a process about 3 minutes old.
- **No data change.** The account's data was the same as in the fast 18:29
  run: no new check-ins, completions or injury changes, and the same local
  day (Europe/London).
- **Supabase was slower that minute.** Server-side GET p50 was 66ms in the
  19:04 minute, against 24–41ms in the surrounding minutes and in the 18:29
  run. That alone inflates the pure-I/O stages `today_reads` and
  `today_sync`.
- **Cold first builds are slow.** Locally, a fresh process's first Today
  build spends 246–287ms in `today_core`, against 30–57ms warm. The cost is
  one-time: regular-expression compilation for injury-text matching (about
  2,600 patterns) and clinical-policy composition (~50ms). Warm builds hit the
  regex caches (3ms for 3,054 lookups).
- **#3263 is not the cause.** Equivalent workload (realistic 6-week camp, an
  open injury, spaCy off as in production):

  | Code | First build `today_core` | Warm `today_core` | Response |
  | --- | --- | --- | --- |
  | Before #3263 (`f155dd4`) | 347ms | 32–54ms | 5,985B |
  | After #3263 (`c7d2f62`) | 246ms | 30–43ms | 5,985B |

- **The larger response is unexplained.** The account's data did not change,
  and Today has no time-of-day switch. Without the API logs I could not
  establish which state produced 12,478 bytes. Capturing the next report away
  from a deploy, with the request ids, would settle it.

Not changed: a startup warm-up (policies, rehab bank, one injury-phrase score)
cut the first build from 269ms to 158ms locally, recovering only part of the
cold cost, and only for the first requests after each deploy. It is an option
if post-deploy first loads matter.

## Background jobs

The worker shares the 2-vCPU host. Its polling RPCs ran normally through the
test window (`list_claimable_generation_jobs_v2` p50 40–62ms). The first-login
writes at 19:04 (`award_athlete_xp`, `record_athlete_daily_activity`,
`validate_xp_abuse_hardening`) ran as part of a sign-in, inside those
requests' own time.

## Tests

`tests/test_event_loop_blocking.py` checks two things:
- The generation-job routes, including a completed job that resolves its
  plan, never call the store while an event loop is running in that thread.
- The webhook's database calls run off the loop, and its 200, 400 and 503
  responses are unchanged.

Both tests fail on Main. 1,269 tests pass across the related suites; 55 fail
identically on Main in this environment.
