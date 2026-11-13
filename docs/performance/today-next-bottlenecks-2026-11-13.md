# Today: resolve, the full-plan request and `today_core` outliers

Production report after #3267:
- `/api/today` backend: 492–763ms.
- Stages: `today_resolve` 127–349ms, `today_reads` 132–247ms, `today_core` 126–378ms.
- Full `/api/plans/:id`: 1,575ms backend.

Production evidence comes from Supabase edge logs (23:35:55–23:36:07, the
reported run) and read-only SQL on the account's plan. Local numbers are
labelled and use the production-shaped fixture with spaCy disabled, as in
production.

## 1. `today_resolve`: two reads in sequence, nothing else in flight

- **What it does.** Resolve reads the active-plan pointer, then the plan row with
  its runtime brief.
- **Where the time goes.** In-process work is 3.5ms (decode, copy, eligibility),
  so the stage is the two round trips.
  - Pointer: 11–44ms origin.
  - Plan row: 52–245ms origin. It is about 346KB: a 241KB runtime brief and an
    85KB structured plan.
  - In Postgres the query takes 24ms (`EXPLAIN ANALYZE`). The rest is building
    and sending the response.
- **Why it varies.** The slow plan reads cluster where several plan reads
  overlap. At 23:36:03 three runtime reads (245, 170 and 119ms) ran alongside
  the plan detail's 1.9MB `select *` (284ms).
- **The brief stays.** Today's view uses it, so the row can't be narrowed
  further without changing what the view computes.
- **The sequence after resolve.** Next came the sync's wave, then the view's
  first wave, then the episode and readiness wave: up to five round trips one
  after another.

**Change.** Three groups of reads can't be issued earlier as-is:
- The sync's flags and the latest intake need only the athlete.
- The view's reads need only the athlete, the day and the plan.
- The second wave needs the open flags and whether there is a check-in today.

What blocked them was the sync's possible write. Any write clears the build's
cache, so rows read before it can't be served after it. Now:
- **While the plan resolves**, the build reads the sync's flags, the latest
  intake and the day's check-ins.
- **The sync writes only for injuries its intake lists.** When the latest intake
  is the plan's intake (same table, `select *`, never written by Today) and
  lists none, the sync provably writes nothing.
- **Only then** do the view's first-wave, episode and readiness reads join the
  sync's intake read, in one wave.
- **Otherwise** the build keeps the previous order, so the view still reads after
  the write. In production the sync RPC (`sync_intake_injury_flag`) ran 322
  times in 24h, roughly a quarter of the builds in busy hours. Sending view reads
  early for those builds would only have repeated them.
- **Readiness reads** are gated on any check-in for the day. That is a superset
  of the plan's check-in, so they are never missed.
- **A write during an overlapped wave** discards the whole wave (a write
  counter in `_BuildReadCache`). An auto-activation during resolve therefore
  can't leave pre-write rows cached.

Local, identical data, all builds give the same view:

| Case | Latency | Main | Branch | Reads |
| --- | --- | --- | --- | --- |
| Checked in | prod-shaped | 304ms (cold 330) | 254ms (cold 287) | same |
| Not checked in | prod-shaped | 280ms (cold 312) | 251ms (cold 283) | same |
| No write, open injuries | 60ms every read | 446ms | 263ms | 22 → 22 |
| Sync writes | 60ms every read | 511ms | 512ms | 28 → 30 |

Prod-shaped means a 30ms pointer read, a 150ms plan row and 25ms for each other
read. Production p95 was taken from the edge logs, not measured here.

`today_resolve` itself is unchanged (it is the plan row). The saving is the
waves after it: `today_reads` drops to 0ms when the sync can't write.

## 2. The full-plan request after `/api/today`

- **Caller.** `useTodayCommand` (`web/components/today/use-today-command.ts`)
  calls `getPlan` after `/api/today` returns. That request is presentation-only:
  Today's state, readiness and safety come from `/api/today` and never wait for
  it.
- **Fields Today uses:**
  - `outputs.structured_plan` (the blocks), or `plan_text` as the fallback;
  - `schedule_context` and `created_at`;
  - `rehab_label_policy`;
  - `outputs.exercise_media`.
- **Is it required? Yes.**
  - `/calendar` returns only the week and day spine, with no sessions or
    blocks.
  - `/api/plans/active` returns a summary.
  - `/api/today` carries today's prescription, not the full week's blocks.
  - None of them supplies these fields, so the request stays.
- **Why it costs 1.5s for this account: it is an admin.** An admin's plan read
  is `select *`:
  - The row is 1.92MB: the brief is 930KB, `stage2_payload` 654KB and
    `stage2_handoff_text` 156KB.
  - The response is 1.78MB, because `admin_outputs` repeats the brief and the
    Stage-2 payload.
  - An athlete reads the athlete projection (1.08MB) and gets a 109KB response
    for the same plan.
  - Locally, CPU is the same for both roles (~88ms, mostly `plan_map`). The
    admin cost is the extra bytes from Supabase and to the phone.
  - Today reads none of `admin_outputs`.
- **Not changed.** Serving admins a lean variant needs a new request parameter,
  and only the admin test account benefits. Athletes already get the lean
  response. It is an option if admin-measured numbers should match athletes'.
- **Smaller finding, not changed.** The detail route decodes the brief 4 times
  (~23ms locally), as the calendar did before #3266.

## 3. `today_core` outliers

The 378ms build is the run's first, at 23:35:55. Its last Supabase read (the
readiness wave) finished around 23:35:56.6, and no further read belongs to it.
So the core time is CPU, not I/O.

#3267 merged at 23:30:44, so this was one of the first builds in a freshly
deployed process. The first build compiles about 2,600 injury-text patterns and
composes clinical policies: a one-time cost of 246–287ms locally
(`docs/event-loop-queue-investigation-2026-11-12.md`). Later builds in the run
show no reads inside core and match warm times (~126ms).

No change: there is no evidence of a per-request cause.

## Tests

`tests/test_today_resolve_overlap.py` checks:
- The view is identical to reading everything in sequence, for:
  - a check-in with open injuries;
  - no check-in;
  - no intake on the plan;
  - a sync that writes;
  - a check-in for another plan only.
- When the sync can't write, only the pointer and plan reads are sequential,
  and nothing is read twice.
- When the sync may write, the view's reads still follow the write.
- The write predictor counts every uncertain case as a write.
- A write during a wave discards it.
- Auto-activation during resolve leaves the view unchanged.

The related suites (195 files) fail the same 96 tests on Main and on this
branch, all environmental (for example `aiohttp` missing here).
