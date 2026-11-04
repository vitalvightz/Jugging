# Where Home's seconds go — Today read waves (2026-11-04)

Base: `9f98b4b` (#3194–#3197 merged). Raw before/after runs: [today-read-waves-2026-11-04.json](today-read-waves-2026-11-04.json).

## Production evidence (Supabase edge logs, read-only)

The backend runs on Hetzner (Nuremberg) and reads Supabase (eu-west-3, via Cloudflare CDG) through the Data API. Edge logs record every backend → Supabase request with `response.origin_time`, so the backend's own waterfall can be reconstructed for a real user action without production API logs.

**Screen recording 22:02:16–22:02:27 UTC** (History → Home → Today, iPhone). This ran on code *before* #3194/#3195: the backend restarted with #3194 at 22:02:39 (`runtime_schema_introspection`).

| Time (UTC) | Backend → Supabase | Note |
| --- | --- | --- |
| ≈17.6 | — | Home tapped; skeleton shown |
| 18.70–19.77 | `plans.*` + `generation_jobs` for plan `ff22…` | Home's warm-up of `me.latest_plan`, which is **not** the active plan |
| 18.85 → 22.66 | `/api/today` build: ~25 sequential Data API calls | `injury_flags` ×3, `today_checkins` ×4, `session_completions` ×3, `profiles` ×2, `athlete_intakes` ×2; in-process gaps of 0.89 s and 0.64 s while the concurrent plan request held the single process |
| ≈22.6 | — | Home content visible (video frame) |
| 23.17 | `plans.*` for the active plan `7363…` | Only now the plan the countdown needs |
| 25.08 → 26.37 | Next `/api/today` (Today tab) | Same build, uncontended: ~1.3 s |

**After all fixes were deployed** (boot 22:47:46), Today builds were still slow:

- An uncontended worker-sweep build (22:36:42.8 → 22:36:45.9) took **3.1 s for 31 sequential calls**: ~1.2 s Supabase origin time, ~0.45 s network, ~1.4 s in-process gaps. One build read `injury_episode_events` ×8, `today_checkins` ×5, `session_completions` ×5 and `injury_flags` ×4.
- The first real user session after that deploy (22:55, iPhone) issued **5 full Today builds** in 20 s from one app open. Only 2 of them served `/api/today` (`beta_feedback` markers); XP progress rebuilt the rest. The first `/api/today` ran from 22:55:01.5 to 22:55:10.66: **~9 s**, which matches the reported 8–10 s cold start.
- `pg_stat_statements`: full `plans.*` row reads by id average 40–150 ms but reach **5.6–7.6 s maximums** (28,620 calls for one shape). Active plan rows are 270–375 KB.

## Root causes

1. **Every Today build is a chain of ~31–40 strictly sequential Data API round trips.** At ~40–60 ms per call in production, that alone is 1.3–2.2 s before any CPU work, and it applies to Home, Today, XP progress and the notification sweeps.
2. **One app open runs several full builds at once in a single-process API** (`uvicorn --workers 1`). XP progress is requested alongside `/api/today` and could only reuse a *finished* build, so it built its own. Every analytics page-view POST also discarded the reusable view.
3. **Check-in saves re-read the same rows**: `submit_today_checkin` was the only Today mutation not using the per-operation read cache, and it made 16 sequential reads before its write.

Earlier audits used fixtures pinned to June 2026, whose camps have ended by today's clock. Those fixtures make 11–16 calls in ~25 ms, so the multi-second path was never reproduced.

## Changes

- `_BuildReadCache.prefetch`: after the intake sync's writes, the reads the view will make are issued in **two concurrent waves**, keyed on athlete, plan and day, then on the open flags. Results land in the same per-operation cache the view already uses. Rules are unchanged: never across requests, successful reads only, cleared by any write. Episode events and rehab exposures join the shared read set, so each flag's history is read once.
- `/api/today` announces its build. XP progress waits for and reuses that build instead of starting another. A write by the athlete during the build moves a per-athlete generation on, so that view is neither remembered nor handed out.
- `/api/analytics/events` is Today-neutral: it only inserts `product_events`, which no Today input reads.
- `submit_today_checkin` uses the per-operation read cache, like the session-completion and injury check-in writes, and prefetches its pre-write reads. The write, its order and the backend-confirmed response are unchanged.

Not changed: Home's plan warm-up. Deferring it until Today names the active plan measured no Today speedup (742 vs 762 ms) and delivered the plan ~500 ms later. Its wasted cost in the recording came mainly from the rehab-label CPU that #3195 already removed.

## Before / after (same harness, same host, back-to-back, 7 runs, median)

`scripts/replay_latency_server.py` runs the real FastAPI app under uvicorn with one worker. Its in-memory athlete is shaped like the production aggregates: an active 5-week camp, an intake injury, open injuries with episodes, resolved history, recent check-ins, completions and manual sessions. Each outermost store call sleeps the given latency, calibrated against production edge logs. One `/api/today` makes 33 store calls here, against 31–40 Data API calls in production.

| Measurement | Before 40 ms | After 40 ms | Before 60 ms | After 60 ms |
| --- | ---: | ---: | ---: | ---: |
| Repeat Home/Today navigation (`GET /api/today`) | 1,507 | **640** | 2,160 | **832** |
| App open: Home usable (`/api/today` returns) | 1,712 | **785** | 2,384 | **1,055** |
| App open: all requests done | 2,053 | 1,037 | 2,860 | 1,424 |
| App open: backend store calls | 92 | **56** | 92 | 56 |
| XP progress, nothing to reuse | 1,879 | 981 | 2,715 | 1,388 |
| Check-in save | 853 | **327** | 1,255 | **470** |

All values are milliseconds except the call counts. These are server-side request times. They exclude browser JS/hydration, client↔server network and Supabase origin outliers.

## Tests

- New `tests/test_today_read_prefetch.py`:
  - the full Today view is identical (`model_dump`) with and without prefetch;
  - each flag's episode history is read once;
  - a failed prefetch read is not kept, so the consumer reads again;
  - prefetch never writes;
  - the check-in save result is unchanged;
  - XP joins a running build;
  - a write during a build blocks its reuse;
  - another timezone never joins;
  - analytics keeps, and a real write drops, the reusable view.
- Targeted suites: Today read amplification, source-neutral readiness refresh, external training, XP, readiness fail-safe, plan-free readiness, surface/sparring readiness, check-in answers, rehab experience, intake sync, injury replay, Today API, product analytics, XP progress. Results: 365 + 94 passed. 3 failed, and those also fail on untouched `9f98b4b` in this environment (no spaCy): `test_free_text_restriction_names_the_area_and_keeps_the_athletes_words` and two `test_persisted_readiness_preserves_app_budget_with_coached_days` cases.

## Not measured / remaining

- **No production after-measurement yet.** Once deployed, re-run the edge-log query: a build should show ~9 sequential calls followed by two bursts of near-simultaneous requests, and one app open should produce one full build instead of 3–5.
- Cold start: JWKS is fetched on the first request and `/auth/v1/user` (~350 ms) is still called twice concurrently after a restart. The first requests also pay the `plans.*` TOAST tails above. Projecting plan detail columns is the next backend target.
- Remaining sequential head: profile, active plan, plan, intake sync reads and its RPC writes, about 9 round trips.
- XP progress still has ~7 sequential reads of its own.
- Browser cost (JS/hydration ~1–2.7 s on throttled mobile, per the earlier Home startup report) is separate and unchanged.
- Session-completion save was not measured: the harness day is a rest day.
