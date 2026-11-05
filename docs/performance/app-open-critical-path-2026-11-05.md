# App open critical path: 23:46 iPhone session (2026-11-05)

Base: `6fde133` (#3192–#3198 merged). Raw numbers: [app-open-2026-11-05.json](app-open-2026-11-05.json).

**Verified** means read from production logs or the production database, or measured here on the real code.
**Inferred** means reasoned from that evidence. The two are labelled throughout.

## 1. Root causes, ranked by time on the app-open path

| # | Cause | Where | Cost before | Status |
|---|---|---|---|---|
| 1 | **supabase-py re-validates every PostgREST response through a recursive pydantic `JSON` union.** The production active plan row is 1.29 MB (`planning_brief` 455 KB, `stage2_payload` 566 KB). Parsing it takes **140–160 ms of CPU vs 6 ms for `json.loads`**. The 550 KB training projection is read 3–5 times per app open. This CPU runs inside the single API process (`uvicorn --workers 1`), so it holds the GIL that every concurrent request needs. | backend, every Data API read | **804 ms of the 1,145 ms CPU** in a single app open (replay, CPU only). Home's data **+0.52 s** at 40 ms/call | **fixed** |
| 2 | **Home warmed the full plan detail for a plan with no fight date.** The calendar only feeds the fight countdown, and the countdown needs a fight date. The 23:46 athlete's active plan has `fight_date = null` (verified), so nothing on screen used this read. It cost two 1.29 MB row reads (route + `resolve_active_plan`) and the plan-detail mapping, all competing with Today and XP. | `web/app/page.tsx` | Home's data **+0.40 s** (replay, read alone) | **fixed** |
| 3 | **Admission runs strictly before any API read.** `getSession` → `profiles?select=access_status` (phone → Supabase; origin 132 ms at 23:45:58.731) → only then `/api/me` and `/api/today`. | `AuthProvider` | **~0.3 s** in the phone benchmark (one Supabase round trip) | **remaining**: needs server-side admission first (§7) |
| 4 | Today's build is still a chain of ~11 sequential Data API stages. That gives ~0.9 s at 40 ms/call and ~1.6 s at 80 ms. Every Home/Today visit pays it. | backend `/api/today` | 0.9–1.6 s per visit | **remaining** (§7) |
| 5 | JavaScript start-up before the first request. | browser | ~1.06 s at 4× CPU (bench) | **remaining** |
| 6 | The XP card waits for `POST /api/xp/activity`, then `/api/xp/progress`. Progress waits for the running Today build, then does ~7 reads of its own. | `XpProvider` / backend | XP card ~0.9 s after Home's data | **reduced** by #1/#2, otherwise remaining |

Causes 1 and 2 also explain the **idle gaps** in the production log (inferred, see §2). At 23:46:01.825 → 02.680 and 02.742 → 03.642, the backend made **no** database request for 0.85 s and 0.90 s while four of the athlete's requests were open. Supabase origin times stayed low (8–230 ms). The time went to Python CPU in one process, not to the database. This is why the database request count did not explain the delay.

## 2. Timeline of the production session (4 Nov 2026, UTC)

Source: Supabase `edge_logs` (verified). Every backend request is `python-httpx` from Hetzner (Nuremberg) via Cloudflare CDG. The browser's admission request is `Mozilla/5.0` from GB. No backend request log or browser trace exists for this window, so the request each read belongs to is **inferred** from query shapes in the code.

| Time | Verified event | Inferred owner |
|---|---|---|
| 45:54.7 | worker RPCs (`list_active_generation_jobs_for_recovery_v1`, `claim_plan_ready_email_deliveries`) | generation worker (separate process), unrelated |
| ≈45:56–57 | — | app launched (from recording: Home content 4–5 s later) |
| **45:58.731** | browser → `profiles?select=access_status` (origin 132 ms) | admission. **First network evidence of the app: ≈2 s after launch were spent before it (HTML, JS start-up, auth from storage)** |
| 45:58.967 / .998 | two `profiles?select=*` (94 / 121 ms) | `require_profile` for `/api/me` and `/api/today`, ~240 ms after admission started |
| 45:59.089–.842 | intake, `active_plan_id`, plan projection (109 ms), flags, intake, flags, `sync_intake_injury_flag`, flags ×2 | Today's sequential head (9 stages) |
| 45:59.858–.867 | 13 concurrent reads (origin up to 233 ms, cold) | Today prefetch wave 1 (#3198) |
| 46:00.020 | `record_athlete_daily_activity` (199 ms), `ingest_product_event` ×2 | `POST /api/xp/activity`, analytics |
| 46:00.173–.239 | episode events, exposures | Today prefetch wave 2 |
| 46:00.476–.870 | plan status, sparring, check-ins, completions, external sessions (sequential) | Today tail, plus XP progress starting (`xp_accounts` .869, `xp_awards` .907) |
| ≈46:01.0 | — | `/api/today` returns → **Home primary content** (≈4.5 s after launch, matching the recording) |
| 46:01.092–.176 | plan status, plan projection | XP progress |
| **46:01.623 / .702** | **two `plans?select=*` (179 / 114 ms), each 1.29 MB** | Home's calendar warm-up `GET /api/plans/{id}`: route read + `resolve_active_plan` re-read |
| 46:01.659–.825 | completions, sparring, external sessions, XP awards, generation status | XP progress `current_week` / opportunities |
| **46:01.825 → 02.680** | **no database traffic, 855 ms** | CPU: parsing two 1.29 MB rows the library way + plan-detail mapping + XP work, under one GIL |
| 46:02.680–.742 | milestones, focus inferences, `generation_jobs` (159 ms), XP awards, plan projection | XP progress; plan detail `plan_source` |
| **46:02.742 → 03.642** | **no database traffic, 900 ms** | CPU: projection parse + plan detail + XP |
| 46:03.887 | `athlete_streaks` | XP progress's last read → **XP card fills** (≈7.5 s) |
| 46:03.894–.935 | analytics event, `beta_feedback`, latest job status, plan projection | user opens **Today** (`beta_feedback` is Today's contextual feedback) |
| 46:04.497–06.309 | second full Today build ("the 22-request burst" at 05.3–05.4 is its wave 1) + one `plans?select=*` | Today screen's fresh `/api/today` and plan read |
| 46:08.7, 11.3, 12.4, 16.5, 19.3, 21.4, 23.0 | plans list, history, progress pages, another Today build | further navigation |

The recording reported ≈9–10 s for the remaining sections. The log places the XP card's data at ≈03.9, about 7–8 s after an estimated launch at 56–57. Launch time is not in any log, and the gap may also include the round trip and render. The video's frame times are needed to close that 1–2 s difference, so it is not claimed as explained.

The 23:46:05 burst is not a duplicate Home build. It is the Today screen's own fresh build after the athlete navigated (inferred from the `beta_feedback` read that only Today's contextual feedback makes). It ran as designed.

## 3. Home skeleton → blocking dependency

| Skeleton | Visible until | Blocking chain (before) |
|---|---|---|
| `Unlxcking…` card | `isReady` | `getSession` → admission (`profiles.access_status`) |
| Whole `WorkspaceOverviewSkeleton` | `isMeHydrated` **and** Today loaded | `/api/me` ‖ `/api/today`, both started only after admission. Today ≈2 s in production under contention |
| `XpProgressCardSkeleton` (primary slot when no countdown; secondary when countdown) | `/api/xp/progress` | `/api/me` → `POST /api/xp/activity` → `GET /api/xp/progress` → waits for the running Today build → its own reads |
| `FightCountdown` placeholders (dated camps only) | `/api/plans/{id}` | Today (or `me.latest_plan`) → full plan detail (2 × 1.29 MB reads + mapping) |
| `WeighInShortcut` status | its own weigh-in read | final-weeks dated camps only |

For the 23:46 athlete (open plan) the countdown never rendered. The plan read was pure cost.

## 4. Fixes

1. **`api/postgrest_parsing.py`.** Supabase-py's response parse is replaced by `json.loads` for JSON-array bodies. It is installed by `api/store.py` and `api/auth.py`. A JSON array decodes to exactly the values the validator returns. Tests compare values, `repr` and types for nested, unicode, big-int and float payloads. Every other body (object, scalar, invalid JSON, empty) still goes through the library's own method, including its errors. **Effect:** 1.29 MB row 140–160 ms → 6–7 ms; per app open 804 → 19 ms of parse CPU. It applies to every endpoint and every save.
2. **Home reads the plan calendar only when the countdown can use it.** That means the active plan has a fight date, or before Today arrives, `me.latest_plan` has one. Dated camps keep the warm-up and the same calendar. There is a new browser test for that. The `calendarPending` placeholder follows the same condition.
Considered and **not shipped**: starting `/api/me` and `/api/today` alongside the admission check for browsers that had seen the athlete admitted. The backend's `require_profile` verifies the user but does not enforce `access_status`, so a revoked account would receive private API responses before the browser found out. Hiding the screen afterwards does not undo that. See §7.

Not changed: Home and Today still read Today fresh; there is no caching of readiness/injury decisions. Write invalidation, confirmed-save behaviour and the XP activity → progress order (which keeps the login streak correct) are unchanged.

## 5. Before / after

### Backend replay: real API, production-sized plan, real parse path

`scripts/replay_app_open.py` runs the real FastAPI app under uvicorn with one worker, pinned to one core (`taskset -c 0`). It uses the production active-plan row, exported to a local file and **not committed**, plus fixture injuries, check-ins and sessions. Every store result is serialized with the real store's column projection and parsed through supabase-py. Each store call sleeps the given latency. The app-open burst is fired as the browser does. Medians of 7.

| Measurement | Before 40 ms | After 40 ms | Before 80 ms | After 80 ms |
|---|---:|---:|---:|---:|
| Home data ready (`/api/me` + `/api/today`) | 1,474 | **933** | 2,141 | **1,601** |
| XP progress returned | 1,847 | **1,307** | 2,825 | **2,246** |
| Store calls per app open | 64 | 52 | 64 | 52 |
| PostgREST parse CPU per app open | 599 | **17** | 622 | **20** |
| Repeat `GET /api/today` (uncontended) | 979 | 917 | 1,619 | 1,587 |
| Check-in save | 406 | 395 | 699 | 683 |
| Log training save | 127 | 129 | 249 | 248 |

CPU only (0 ms latency): app-open CPU **1,145 → 382 ms**, and Today under contention **913 → 312 ms**.

Attribution at 40 ms: parse fix alone gives 1,474 → 952; dropping the undated warm-up alone gives 1,474 → 1,079.

### Phone benchmark: real browser, production build, mocked API

`web/e2e/app-open-bench.spec.ts`: 390×844 touch viewport, 4× CPU throttling, reduced motion. Server time per endpoint is taken from the backend replay above (40 ms), plus 120 ms phone ↔ API round trip. Admission takes 250 ms. The base and changed builds ran from the same machine with identical settings. Medians of 7.

| Milestone (ms from navigation) | Before | After |
|---|---:|---:|
| First API request | 1,480 | 1,369 |
| Home primary content usable | 3,374 | **2,786** |
| XP card filled / no skeletons | 4,283 | **3,644** |

**Not a production measurement.** Server times are fixed per endpoint rather than emerging from live contention, and the local CPU is faster than the production vCPU. Assets are served locally. The production gaps (0.85 s / 0.90 s with no database traffic) suggest the production CPU is slower than this machine, which would make fixes 1–2 worth **more** there. That is unverified until a post-deploy log is read (§7).

### Targets

- **Cold Home < 2 s: not met in the benchmark (2.79 s).** About 1.1 s is JavaScript start-up before the admission request at 4× CPU, ~0.3 s is admission, and ~1.1 s is Today's server time plus the round trip.
- **Repeat navigation < 1 s: not met.** Home → Home re-reads Today fresh (≈0.92 s server at 40 ms, plus the round trip). That is unchanged by design; see §7.
- **Simple saves < 500 ms:** log training 129 ms and check-in 395 ms at 40 ms/call, server side (unchanged). At 80 ms/call the check-in is 683 ms.
- **Prolonged skeletons:** the undated-plan calendar read is gone, and XP fills ~0.6 s earlier.

## 6. Tests

- `tests/test_postgrest_parsing.py` (new): values, types and `repr` are identical to supabase-py's for nested/unicode/big-int/float rows. The count header is preserved. Object, scalar and invalid bodies raise the same `ValidationError`. An empty body still returns `[]`. Installation is idempotent.
- `web/e2e/home-startup.spec.ts` (new case): a dated camp still reads its calendar and renders the countdown. The existing "stays gated until admission succeeds" test still passes.
- `web/e2e/frontend-requests.spec.ts`: Login → Home no longer reads the undated plan's detail. Home → Today may now read it, since Home did not.
- Results:
  - Backend: 408 targeted tests passed. These cover the store, request amplification, Today read amplification/prefetch, projections, XP RPC/progress, streaks, the Today API and app core.
  - Web unit: 111 passed (countdown, Home account scope, XP provider, shared reads, auth routing).
  - E2E: home-startup, request budgets and home-readiness passed (14/14 after removing the early reads). Also passed: weight-cut final weeks, training-plan presentation, google-auth, beta analytics, and starting-path/signup-code apart from the 10 cases below.
  - **10 starting-path / signup-code cases fail identically on the unmodified base build** in this environment, so they are pre-existing and unrelated.
  - Typecheck clean; no new lint warnings.

## 7. Remaining bottlenecks and the evidence still needed

0. **Admission before reads (~0.3 s).** To overlap it safely, the backend must enforce admission itself. It can't simply require `access_status = approved` in `require_profile`, because admission also passes for non-approved accounts that hold the signup-access cookie. That path needs a server-side record first. Only then can the browser start reads before its own check.
1. **Today's sequential chain (≈0.9–1.6 s per visit).** `ensure_profile` → `active_plan_id` → plan projection → flags → intake → flags → adopt/sync RPC → flags, then two waves. In production a five-read tail (plan status, sparring, check-ins, completions, external sessions) also ran sequentially *after* the waves. The fixture does not reproduce that tail, so it needs the athlete-shaped state traced before it is moved into a wave. This is the largest remaining server cost on every Home/Today visit.
2. **Repeat navigation.** Home re-reads Today fresh on every visit by design. Rendering the held view from this navigation, with a fresh read replacing it, would make returns instant. That is a product/safety decision about readiness decisions shown for <1 s; it is not taken here.
3. **JavaScript start-up (~1 s at 4× CPU)** before auth: the shared bootstrap, Sentry and Supabase SDK evaluation. The `force-dynamic` root layout means every launch also waits for a server-rendered HTML response. Neither was measured on a real phone.
4. **The PWA starts at `/dashboard`** (`manifest.start_url`), which renders Today, not Home. Which screen the recording opened on should be confirmed.
5. **`/api/plans/{id}` reads the full plan row twice** (route + `resolve_active_plan`). It is now much cheaper per read, but still 2 × 1.29 MB over the network. Dated camps and the plan viewer still pay it.
6. **XP card:** activity → progress is serial on purpose (the streak). Progress still makes ~7 reads after reusing Today's build.
7. **Single-process API.** One app open now costs ~0.38 s of CPU instead of ~1.15 s, but every user shares one core. A second worker would not share the in-process Today/profile caches.

**To verify in production after deploy**, re-run the edge-log query for an app open:

- the two 0.85–0.9 s windows with no database traffic should disappear;
- an undated plan should show no `plans?select=*` on Home;

For the browser side, a Safari Web Inspector timeline from the phone (or Sentry browser tracing with the production DSN) is needed. That is the only way to split the ≈2 s before the admission request into HTML, JavaScript and auth. No production browser trace exists for 23:45:56–58.

### Reproduce

```sh
# backend (plan row exported locally; never commit it)
taskset -c 0 python scripts/replay_app_open.py plan_row.json before.json --library-parse --plan-warmup
taskset -c 0 python scripts/replay_app_open.py plan_row.json after.json
# browser (build each tree with CI placeholder NEXT_PUBLIC_* values; serve base and change on two ports)
E2E_PORT=<port> APP_OPEN_BENCH_OUT=out.json APP_OPEN_BENCH_SERVER='{"me":138,"today":961,"xp_activity":91,"xp_progress":1208,"other":60}' \
  npx playwright test e2e/app-open-bench.spec.ts --workers=1
```
