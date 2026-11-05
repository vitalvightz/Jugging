# Repeat navigation and loading placeholders (2026-11-05)

Base: `6a3b299` (#3198–#3200 merged). Raw numbers: [navigation-2026-11-05.json](navigation-2026-11-05.json).

**Verified** means measured here, read from production logs, or requested from production. **Estimated** or **hypothesis** means not yet measured on a real iPhone. The diagnostic mode in §6 exists to close that gap.

## 1. What blocks a screen after a tap, ranked

Every tab tap went through three steps, one after another:

1. **Route payload (verified).** The root layout is dynamic: it sets a per-request CSP nonce and calls `headers()`. Because of that, Next's client router fetched `GET /<route>?_rsc=…` from the server on **every** tap, including revisits, before the new screen could mount. Production serves these payloads from a Vercel function in **iad1** (Washington, D.C.): every response carried `x-vercel-id: iad1::iad1::…`. TTFB was 0.18–0.40 s per request from this US-based container (12 samples), ~22 KB each. From a UK phone that adds a transatlantic hop plus cellular round trips (estimated 0.4–0.8 s; not yet measured on the device).
2. **The screen mounts empty (verified).** App Router pages unmount on navigation, so Home's and History's state (`useState`) is lost on every visit. Home rendered the full-page `WorkspaceOverviewSkeleton` until a fresh `GET /api/today` returned. History rendered `ListSkeleton` until `GET /api/training/history` returned. Nothing on screen depended on that read being complete except Today's decision.
3. **Fresh Today build (verified, unchanged on purpose).** Production edge logs for the 00:52 recording show one full Today build per Home/Today visit: 00:52:12.46→13.27, 16.54→17.28, 25.61→26.59, 28.71→29.46, 37.38→38.22. That is **0.75–1.0 s server time each**, with individual reads of 7–60 ms (`origin_time`). The time is the build's sequential chain, not slow queries.

Time wasted per visit, phone benchmark (4× CPU, 450 ms route-payload delay; §5):

| Cause | Home | History | Today / Plan / Timer |
|---|---:|---:|---:|
| Route payload round trip before mount | 0.45–0.55 s | 0.45–0.55 s | 0.45–0.55 s |
| Whole screen held for one fresh read | 1.0–1.2 s | 0.5–0.6 s | — |
| Fresh Today build (still waited for by Today's decision) | ~1.05 s incl. RTT | — | ~1.05 s (Today) |

**Desktop vs iPhone (verified in part).**
- Cause 1 is one blocking round trip per tap. It grows with the network's round-trip time, which is much longer on mobile than on desktop Wi-Fi.
- At 4× CPU throttling the route change alone took 100–240 ms even with a local server.
- **Hypothesis, unmeasured:** Safari main-thread cost and real cellular latency explain the rest of the recording's 2–2.5 s (the benchmark reached 1.83 s). The diagnostic report records both per tap.

## 2. Why Home and History reloaded every time

- **Home** (`web/app/page.tsx`) kept the command view in component state, initialised to `null`.
  - Each mount started with `commandState === null`, which renders `WorkspaceOverviewSkeleton`.
  - `getToday` is a shared read that only reuses an *in-flight* request, never a finished one. It deliberately never reuses a past result, because the view carries readiness, injury and training-permission decisions.
  - So every return to Home showed a skeleton for the full fresh build plus the round trip. Timer → Home, History → Home and Today → Home were all the same path.
- **History** kept its rows in component state, with `rows: null` on mount. `getTrainingHistory` is shared only while in flight ("History edits must show on the next read"), so every visit showed `ListSkeleton` until the read returned.
- **Before either fetch could start,** the route payload round trip in §1 had to finish, because the page component does not exist until then.

## 3. Fixes

1. **Reuse route payloads** (`web/next.config.ts`: `experimental.staleTimes { dynamic: 300, static: 300 }`, plus full `prefetch` on the five bottom tabs).
   - Workspace pages are client components that fetch their own data from `/api` on every visit, so their payload is a shell with no user data. Reusing it changes no data freshness.
   - The only server page that reads cookies is `/signup`, and server actions and `router.refresh()` still clear this cache.
   - Result: no blocking `?_rsc=` request on any tab tap (regression test). The five tab payloads (~22 KB each) are prefetched once at idle.
2. **Remembered views, display only** (`web/lib/shared-reads.ts`: `remember` option + `recallView`). The last successful `/api/today` and History first page are kept in memory per token, under these rules:
   - **Any** write (and every in-flight write) drops them, using the same write epochs that already govern shared reads.
   - Sign-out and account switches drop them.
   - A read that began before a write is never remembered.
   - They expire after 10 minutes.
   - Nothing is persisted to disk.
3. **Home shows safe presentation at once and still reads Today fresh on every visit.**
   - **Shown immediately from the remembered view:** plan name, camp day, phase, fight date, countdown, XP card, weigh-in link and the plan link.
   - **Never shown from it:** today's decision (headline, copy, safety line, tone) and the primary action (check-in / train / injury), which render as a section-level placeholder ("Checking today's training decision…"); and training alerts, which render as a placeholder card.
   - If the fresh read fails, the decision area says it couldn't confirm today's status and offers Retry, with no stale decision and no action.
   - Tracking-first athletes are excluded: their Home is derived from readiness throughout, so they keep the old behaviour.
4. **History shows its remembered first page at once and re-reads it.** The fresh page replaces it when it lands. On failure, the shown rows stay. Older pages and the other tabs are unchanged.
5. **Plans list and plan viewer use the same pattern.**
   - **Plans** remembers its whole screen (plan list, active plan id, custom drafts) through `rememberView`. It records the write epoch before its three reads start, so a write landing mid-load is never remembered. A revisit shows the list with no placeholder card and refreshes it in place.
   - **The plan viewer** shows the plan document from the last read while reading it again. Previously it only reused a read for 30 s, then showed "Unlxcking your plan…". Completions, activation and Today's decisions are still read fresh inside the viewer.
   - Activating, renaming, deleting or publishing is a write, so it drops both remembered views. The existing "a rename invalidates every reused plan read" test still passes.
6. **Desktop sidebar links are fully prefetched,** like the phone tabs.
7. **Progress needed nothing:** it renders from the app-wide XP state, which already survives navigation.
8. **Today is unchanged.** Its decisions, actions and session permission all come from the fresh build, so it keeps waiting for it.

## 4. Safety and behaviour preserved

- **Today command view:** still fetched fresh on every Home and Today visit (regression tests count it). Decisions, actions and alerts render only from it.
- **Writes:** a write anywhere drops remembered views. A test renames a plan and then shows Home without the earlier view.
- **Untouched:** authentication, admission, write invalidation, confirmed saves and XP.

## 5. Before / after

`web/e2e/navigation-bench.spec.ts`. Setup:
- **Builds and device:** production builds of base and branch, 390×844 touch viewport, 4× CPU, reduced motion.
- **Sequence:** the recording's order — Today (cold) → Home → Today → Plan → History → Timer → Home → History → Home.
- **API timings (mocked):** Today 900 ms, plan 400, plans 250, history 350, XP 500, others 100–150, plus 150 ms RTT.
- **Route payloads:** 0 ms (local server) or 450 ms (the production TTFB measured above, plus phone round trips).
- Medians of 5. Milestones are measured in the page from the tap.

With 450 ms route payloads (ms: route changed / first real content / usable / no placeholders):

| Navigation | Recording | Before | After |
|---|---|---|---|
| Today → Home | 2–2.5 s | 667 / 1834 / 1834 / 1834 | **157 / 157** / 1325 / 1325 |
| Home → Today | ~1 s | 569 / 1720 / 1720 / 1720 | 124 / 1248 / 1248 / 1248 |
| Today → Plan | < 1 s | 648 / 1126 / 1126 / 1126 | 169 / **627 / 627 / 627** |
| Plan → History (first visit) | 1–1.5 s | 616 / 1303 / 1303 / 1303 | 162 / **783 / 783 / 783** |
| History → Timer | — | 593 / 593 / 931 / 931 | 142 / 142 / 461 / 461 |
| Timer → Home | 1.5–2 s | 586 / 1700 / 1700 / 1700 | **136 / 136** / 1213 / 1213 |
| Home → History (revisit) | — | 567 / 1176 / 1176 / 1176 | **164 / 164 / 164 / 164** |
| History → Home | — | 575 / 1695 / 1695 / 1695 | **127 / 127** / 1220 / 1220 |

- Home's "usable" means the fresh decision's primary action is on screen. Until then the plan link, nav, countdown and XP card work, and the decision area shows its own placeholder.
- **Request counts per step:** API reads are unchanged, because every visit still reads fresh. Route-payload requests went from one per tap to none (they are prefetched once at load).
- With the local route server (0 ms): Home content 1.2–1.4 s → 0.12–0.20 s; History revisit 0.72 s → 0.15 s.

**Targets:**
- Previously visited Home under 500 ms to safe content: **met** in the benchmark (0.13–0.16 s).
- Repeat navigation under 1 s to usable content: **met** for History (0.16 s), Plan (0.63 s) and Timer (0.46 s). **Not met** for the decision on Home (1.21–1.33 s) and for Today (1.25 s): both wait for the fresh Today build by design (§7).
- No full-screen skeletons for background-only refreshes: **met** for Home and History. Today was out of scope and is unchanged.
- **Not measured:** cold Home, saves (unchanged code paths).

## 6. Real-iPhone diagnostic mode (temporary, opt-in)

- **Turn on:**
  - In Safari, open `https://app.unlxck.com/?perf=1`.
  - In the installed app, which has its own storage and no address bar, use **Settings → "Speed report: off"** to switch it on.
- **Use it:** a small `⏱ n` button appears above the tab bar. Every in-app tap is recorded from tap to settled screen:
  - route change, first frame, first real (non-placeholder) content and last placeholder gone;
  - frames longer than 50 ms ("blocked" main-thread time and the longest frame);
  - every API and route-payload request with start, time to first byte, end, size, status and `Server-Timing`;
  - app-start timings (HTML first byte, DOM interactive, first API request).
- **Report:** **Share report** uses the iOS share sheet; there is also copy and an inline "Show report". The JSON has per-route medians and up to 40 navigations, each with a UTC timestamp so it can be matched against Supabase edge logs.
- **Privacy:** paths have every id, number and query value removed. No tokens, headers other than `Server-Timing`, bodies, screen text or typed input are recorded. Nothing is uploaded.
- **Turn off:** `?perf=0`, **Turn off** in the panel, or the Settings switch. Each deletes the log.
- **Cost:** off by default. The recorder and its `fetch` wrapper are a separate chunk that loads only when switched on. Removing it means deleting `components/perf-diagnostics*.tsx` and `lib/perf-diagnostics*.ts`, plus one line each in `app/layout.tsx` and `app/settings/page.tsx`.

## 7. Remaining bottlenecks and the evidence needed

1. **Fresh Today build (~0.75–1.0 s server, every Home decision and every Today visit).** This is now the main wait between a tap and a usable decision. Its sequential Data API chain is backend work, covered in the earlier reports. It is not changed here, because showing a stale decision is not acceptable.
2. **Vercel function region `iad1` (minor, cold start only).** `x-vercel-id` shows two kinds of request:
   - **`/api/*` rewrites** (`iad1::…`, single segment) are proxied straight from the nearest Vercel edge to the backend. For a UK phone that is London edge → Frankfurt/Nuremberg backend. They never reach `iad1`, and the region does not affect them.
   - **HTML and route payloads** (`iad1::iad1::…`) are rendered by a function in Washington, D.C., which costs a UK user one transatlantic round trip per request.

   After this PR, taps no longer wait on route payloads, so a Frankfurt function region would only shave the cold-start HTML and the one-off tab prefetch. That is an estimated 0.1–0.3 s, not measured. Pages fetch no data server-side, so proximity to the backend is irrelevant for them.
3. **Unmeasured on the device:** Safari main-thread time per navigation, real cellular round trips, and whether `/api` rewrites add a hop. **Evidence needed:** one diagnostic report covering the recording's sequence (Today → Home → Today → Plan → History → Timer → Home), from both the installed app and Safari, plus the same sequence on desktop for comparison.
4. **Today itself** still shows its full loading state for ~1.2 s per visit. Session blocks are presentation and could be shown from the plan read while the decision loads; that needs its own safety review.

## 8. Tests

- **Web unit** (`lib/remembered-views.test.ts`, `lib/perf-diagnostics.test.ts`):
  - recall is per token and path;
  - writes, in-flight writes, pre-write reads, failures, sign-out and expiry all drop the view;
  - the path sanitiser strips ids, numbers and query values;
  - the summary is correct and the log is bounded.
- **E2E** (`e2e/navigation-revisit.spec.ts`, 390 px, 6 cases, passed twice):
  - a revisited Home shows plan context at once with no decision, action or alerts until the fresh read lands, which is still made;
  - a failed fresh read gives "status unavailable" plus Retry and no action;
  - after a write, Home shows no earlier view;
  - a revisited History shows rows at once and still re-reads;
  - tab taps make no blocking route-payload request;
  - diagnostics are off by default, record when on, contain no token, athlete id or plan id, and delete their log when turned off.
- **Existing suites passed:** History, Home and shared-read units; home-startup, request budgets, home-readiness, history details, weight-cut, plan presentation, analytics and injury logging.
- **8 cases in log-training / manual-exercise-logging / rehab-experience fail identically on the unmodified base build** in this environment, so they are pre-existing.
- `history-screen.test.tsx` now clears shared reads between tests, since each test is a fresh session.
