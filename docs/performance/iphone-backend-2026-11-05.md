# iPhone backend investigation — partial results, 5 November 2026

Base: `3481b0f3` (Main, including #3198–#3204). No navigation changes.

## Evidence and limits

The supplied iPhone report measures Today at 637–996ms application time and plan detail at 1,070ms. It does not contain request IDs, detailed resource timing, or individual server spans. Exact correlation has been requested. No production deployment or iPhone after-capture has been performed. The latency targets are **not demonstrated** by this change.

Read-only Supabase unified log query, 03:55–03:56 UTC:

| Path | Calls | Mean origin ms | Max origin ms |
|---|---:|---:|---:|
| plans | 46 | 60.8 | 128 |
| generation_jobs | 2 | 210.5 | 221 |
| profiles | 46 | 22.3 | 107 |
| injury_flags | 60 | 16.4 | 112 |
| exercise_media | 456 | 8.9 | 43 |

These are **whole-window observations**, not counts for a particular request. Origin time includes Supabase gateway work and must not be labelled SQL execution time. Media requests occurred about every 32ms at the start of the minute; investigate their caller separately before attributing API contention to them. Concurrent workers and users prevent reliable reconstruction of the phone's exact critical path from these aggregates.

## Confirmed avoidable work

1. Plan detail reads the owner-scoped plan, then rehab-label synchronization resolves the active pointer and fetches the same plan again. The fix passes the already-read row into that resolution. The live pointer, ownership, status, fight-date eligibility, fallback/auto-activation, intake sync, and fresh injury-label reads remain in force. There is no cross-request cache. A mismatching owner or pointer still fetches its own scoped row.
2. Mapping a live production active-plan row takes material Python CPU. Its planning brief was 842,491 bytes; its mapped response was 115,408 bytes (not established as the phone's exact plan). Four read-only local probes produced:

| Probe | Data API row read ms | Mapping wall ms | Mapping thread CPU ms | JSON serialization ms |
|---|---:|---:|---:|---:|
| First | 2618 | 185 | 184 | 0.80 |
| Warm 1 | 2354 | 59 | 59 | 0.70 |
| Warm 2 | 1968 | 119 | 119 | 0.73 |
| Warm 3 | 2166 | 65 | 65 | 0.68 |

A separate cProfile run (165ms, profiling overhead included) attributed 60ms to effective-structured-plan resolution, 50ms cumulative to recursive locked-role traversal, 49ms cumulative to copying, and 35ms to coaching recovery; these overlap and must not be summed. JSON encoding is not the dominant cost. Reconciliation owns exercise identity, contact, calendar and overrides; none has been removed on the basis of this sample.

Paired live active-plan resolution probe, same row/day/client, three alternating repetitions:

| Run | Before wall ms | After wall ms | Before CPU ms | After CPU ms | Data API calls before → after |
|---|---:|---:|---:|---:|---:|
| 1 | 2484 | 937 | 10.05 | 1.81 | 2 → 1 |
| 2 | 3465 | 1107 | 9.80 | 1.72 | 2 → 1 |
| 3 | 2478 | 421 | 12.52 | 1.60 | 2 → 1 |

This isolates the duplicate-read elimination; it is **not endpoint application time**. It excludes the initial plan read, authentication, mapping and injury sync. The workspace proxy path is much slower than the production origin samples. The first resolution follows an initial row read, so this paired table is warm, not a cold-start result. No SQL timing was collected. `scripts/measure_plan_read_reuse.py` provides a read-only reproducible probe including the initial read and mapping; pass an eligible active plan, its owner and the comparison training day. Do not publish raw patient data.

## Execution paths and Today dependency

Today: authentication → profile/admission → live active-plan pointer and training projection → intake injury synchronization → readiness boundary → request-local/prefetched reads and Python prescription assembly → response validation/serialization. Existing prefetch and write invalidation remain intact. No further Today optimization is justified by request-specific measurements yet.

Plan detail: authentication → profile/admission → owner-scoped plan → generation source lookup → active-plan validation/intake sync/fresh rehab policy → mapping (effective card, locked-content merge, exercise/rehab identity, calendar reconciliation, projection, overrides) → minor/safety guards → media attachment → response validation/serialization.

`use-today-command.ts` publishes the authoritative Today state before presentation arrives. It then needs structured weeks (or plan-text fallback), schedule context, creation date, rehab labels and exercise media. It does not need admin outputs or source banners. A presentation projection could save work, but skipping reconciliation would change exercises and calendar truth. No new endpoint or stale safety reuse is introduced here. The plan ID depends on live Today resolution; speculative concurrent plan loading was not added.

## Unaccounted Home time and follow-up capture

The first Home request has 883ms outside the reported application span (1703−820); revisit has 116ms (1112−996). Neither number identifies mobile networking. Existing app timing starts when ASGI middleware runs and ends when response headers are available; it excludes upstream queuing, connection setup, response-body transfer and browser scheduling. Without per-request browser/proxy/backend correlation those components cannot be separated. A cold worker/thread-pool queue can also lie on either side of spans depending on where it occurs.

New Server-Timing spans separate plan_read, plan_source, plan_rehab and plan_map, plus today_build (including build coordination). Existing auth/profile/app spans and opt-in Data API diagnostics remain. Capture request IDs, browser DNS/connect/TLS/request/TTFB/body timing, upstream proxy queue/response timing, backend worker config and diagnostic attempts on the same requests. Compare cold worker/connection/media index and warm repeats under the same concurrent workload.

Still unmeasured: complete endpoint before/after application time, SQL execution time, total endpoint database request count, endpoint CPU under production worker contention, authenticated end-to-end cold/warm requests, Today first usable content and final placeholder disappearance after this change. The original 856ms/2067ms are the only supplied browser values. Production credentials here permit Data API reads but no authenticated phone session or backend deployment access. Do not claim <500ms/<300ms/<1s results from these probes.

## Validation

180 passed across `test_active_plan`, `test_api_plan_access`, `test_slim_plan_reads`, `test_today_read_amplification`, `test_today_read_prefetch`, and `test_api_today`. Run with APP_ENV, UNLXCK_ENV, ENVIRONMENT and NODE_ENV all set to test; the workspace otherwise enables production CORS guards and prevents test-client startup. Web `npm run typecheck` passes. New regression cases cover matching-row read elimination, owner/pointer mismatch and expired/missing pointers. Existing suites exercise access control, plan mapping, injury synchronization and Today writes.

## Review follow-up

The isolated read savings must not be extrapolated to Today end-to-end latency. Today itself has not been optimized by this branch. Added nested spans `today_resolve`, `today_sync`, `today_reads` and `today_core` to split its live build into active-plan resolution, injury synchronization, prefetch (both waves), and readiness/prescription assembly. These measure wall time, not CPU; they overlap the enclosing `today_build` and `app` and must not be summed with those totals.

An alternating same-live-row mapping probe (12 before/after pairs) compared runtime `typing.Mapping` checks with `collections.abc.Mapping` in the locked-content module. After excluding the first two pairs, median mapping thread CPU was 52.4ms versus 47.8ms, with identical serialized output on all 24 runs. That small gain does not explain the outstanding latency; no implementation change was made. Larger 110–118ms outliers in both groups need allocation/GC attribution before claiming a mapping optimization.

Follow-up validation: 88 Today API/read-amplification/prefetch tests passed after the nested timing changes; `git diff --check` passed. No frontend code changed in the follow-up.
