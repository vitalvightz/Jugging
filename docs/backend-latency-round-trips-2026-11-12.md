# Backend latency: round trips, plan payloads and Supabase limits

Follow-up to #3260 and #3262. iPhone report: `today_resolve` 160–1,076ms,
`/api/plans/{id}/calendar` 839–1,113ms for a 2.4KB response, `today_core`
87–124ms, History 613–663ms, Caddy `queue` 2–13ms.

Production evidence comes from Supabase edge logs (`response.origin_time`),
`pg_stat_statements`, read-only `EXPLAIN ANALYZE` and column sizes. The API's
own logs (on Hetzner) were not reachable. Local benchmarks are labelled as such.

## Root causes, ranked by impact

1. **Intermittent Supabase stalls (infrastructure; not fixed in code).** About
   1% of requests on every table exceed 500ms. Tiny indexed reads (1–3ms in
   Postgres) stall up to ~1.5s without disk reads. `today_resolve`'s 1,076ms
   is this. See §5.
2. **Sequential round trips (fixed for Today's sync and History).**
   - Today: the intake sync read `injury_flags` → `athlete_intakes` →
     `injury_flags` one after another (`sync_intake_injuries_for_plan` in
     `api/services/intake_injury_sync.py`), before any view read began.
   - History: completions → external sessions → each title plan → manual
     exercise counts, all one after another (`training_history_page` in
     `api/services/training_history.py`, `list_training_history` in
     `api/routes/today.py`).
3. **The calendar read the whole plan row (fixed).** `_read_plan_for_viewer`
   (`api/routes/plans.py`) read every athlete column including the full
   `planning_brief` with `candidate_pools`. For admins, `store.get_plan` read
   `select *`, including the admin-only Stage-2 blobs. Production sizes for
   the test account's active plan: `select *` 1,873KB (brief 829KB, of which
   the runtime brief is 236KB; Stage-2 blobs 784KB). The countdown uses none
   of this.
4. **One API process.** Home's concurrent requests share one Python process,
   so CPU in one request (Today's core, the calendar's plan mapping) delays
   the others. Less CPU per request (item 3) reduces this.

## Changes

### Today: the intake sync's reads go out in one concurrent wave
`_prefetch_sync_reads` (`api/services/today_readiness_boundary.py`) issues the
sync's open-flag, open+resolved-flag and intake reads together through the
existing per-build `_BuildReadCache.prefetch`. The sync then reads exactly as
before, from that build's cache. Nothing about the sync changes:
- the `adopt_or_create` write RPC is untouched;
- a write still clears the cache, so the final flag read and every view read
  after it go back to the database;
- a failed prefetch is not kept, so the sync reads again and handles its own
  failure.

The view's wave still runs after the sync, so no extra reads are issued when
the sync writes.

### History: independent reads run together
`run_reads_concurrently` (`api/services/today_reads.py`) runs leaf reads on
the existing prefetch pool. Results come back in order, and the first error in
argument order is raised, as if the reads had run sequentially. History uses
it for completions with external sessions, then for the title plans with the
manual-exercise counts. Ownership scoping (`get_training_plan_for_athlete`) is
unchanged.

### Calendar: the runtime brief, without admin blobs
`get_plan_runtime_for_athlete` (`api/store.py`) reads every athlete column
with `planning_brief_runtime` (no `candidate_pools`). The calendar reads the
viewer's own plan this way. An admin reading another athlete's plan still
falls back to `get_plan`, and the admin-hidden archive rule is unchanged. The
candidate pools only stamp exercise identity inside blocks, and tests pin
that the calendar spine still equals the plan detail's.

## Benchmarks

Local, with every store call delayed 50ms to stand in for Supabase:

| Path | Main | This change |
| --- | --- | --- |
| Today build, sync reads only | 454ms (6 sequential calls before the core) | 356ms (3) |
| Today build, sync writes | 545ms | 445ms |
| History, two waves vs four sequential reads | 212ms | 111ms |

Local, the realistic generated 6-week camp (brief 957KB, structured plan 50KB):

| Calendar | Main | This change |
| --- | --- | --- |
| Plan row read, athlete | 1,086KB | 407KB |
| Plan row read, admin (Stage-2 blobs sized as in production) | 1,853KB | 407KB |
| Route time, athlete / admin | 130 / 102ms | 81 / 68ms |
| Response | 4,107B | 4,107B (identical) |

Production improvements are not yet measured. The next iPhone report should
show `today_sync` near one round trip instead of three, a smaller calendar
time, and History's total time.

## 5. Supabase: query cost versus database-side stalls

- Athlete reads are cheap in Postgres. `injury_flags` averages 1.3ms and
  `today_checkins` 1.1ms over tens of thousands of calls. The active-plan read
  is 31ms on the largest plan.
- Request overhead is large relative to query cost. The worker's
  `list_claimable_generation_jobs_v2` runs in 3.3ms in Postgres (0 queued
  jobs) but takes 66ms p50 per request at Supabase's edge.
- Background pollers use more database time than athletes. Over 24h:
  - job claim poll: 5,646 calls, 569s;
  - reactivation email candidates: 245s;
  - plan-ready email claims: 231s;
  - notification evaluation: 148s;
  - recovery scan: 120s.

  Together that is ~22 minutes a day, against 269s for all `plans` reads.
- Settings (`shared_buffers` 224MB, `max_connections` 60) match Micro
  (shared CPU). Stalls that hit 1ms queries without I/O fit CPU contention on
  a shared host. They do not prove a larger tier fixes them.

Recommendation, not implemented:
- Measure before changing compute: compare the edge-log p99 per table for a
  week on the next compute size.
- Separately, lengthen the idle generation-job poll interval. That trades job
  pickup latency for database load, which is a product decision.

## Not done

- Reading the active-plan pointer and row in one PostgREST request (an
  embedded resource over `profiles_active_plan_id_fkey`) would save one more
  Today round trip. It depends on PostgREST embedding the
  `planning_brief_runtime` computed field, which could not be verified
  against the production API from here. Shipped unverified, a failure would
  add a failed request to every Today build.
- The calendar still runs the full plan-detail mapping (~50ms locally) so
  its dates and phases stay identical to the plan page.

## Tests

- New: `tests/test_today_read_prefetch.py` (sync reads leave the sequential
  path, are read once, the view is identical; a sync write still reads flags
  fresh), `tests/test_concurrent_history_reads.py` (order, error precedence,
  identical History output, overlapping reads), and
  `tests/test_plan_calendar.py` (runtime read matches the detail spine, admin
  own plan skips Stage-2 blobs, admin still reads another athlete's
  calendar).
- 976 tests pass across the Today, injury-sync, plan, History, profile and
  XP suites. 54 fail identically on Main in this environment (clinician
  clearance, safety matrix, Today service, push sweep).
