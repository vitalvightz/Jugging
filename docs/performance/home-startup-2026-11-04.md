# Home startup follow-up — 2026-11-04

Base: `b898cbce` (#3194), independently reviewable from #3195. Frontend only; fresh `/api/today` reads and all mutation invalidation rules remain unchanged.

## What the profile found

The earlier mobile audit was enough to establish a problem, not its cause. A new Home-specific production-build capture used system Chromium at 390×844, 4× CPU throttling, reduced motion and mocked API responses delayed by 300 ms. CPU profiling, script coverage and actual browser request events were collected before editing application code.

In three profiled contexts, Home's first admission request started 1.9–2.7 seconds after navigation. This waiting preceded backend API work. The profile attributed about 520 ms to the Turbopack runtime/module evaluation and 444 ms to the Sentry-containing chunk in the first context; profiler overhead is included. The fixture omits a real Supabase auth network round trip and the admission response is instant, so it does not establish real auth latency.

Two concrete sources of avoidable work were observed:

1. `AuthProvider` handles both `getSession()` and `onAuthStateChange(INITIAL_SESSION)`. The event path already deduplicates by access token; the `getSession()` path did not. It restarted admission/hydration for the token the event was already handling. Every measured cold context made **two** Supabase profile-admission requests. The fix applies the same token guard to the getter result. Anonymous initialization, explicit profile refreshes and new-token/account events still run. No admission check is bypassed for a new session.
2. The root `RoundTimerProvider` eagerly imported the timer view, including its settings and set-logging UI. Script coverage showed this code loaded on Home with no active timer. The view is now loaded with React's existing `lazy`/`Suspense` only when opening or restoring a timer. Audio unlock still occurs synchronously in the original tap handler; the provider and saved-run ownership rules remain eager. An accessible opening status covers the first chunk load. An already-saved timer still restores automatically.

## Before/after evidence

Raw summary: [home-startup-2026-11-04.json](home-startup-2026-11-04.json).

| Measurement | Before | After |
| --- | ---: | ---: |
| Admission requests on initial Home, each of 3 contexts | 2 | 1 |
| Loaded static JavaScript source bytes before timer use, CDP coverage | 1,251,053 | 1,210,211 |
| Initial Home ready, median of 3 | 2,365 ms | 2,297 ms |
| Initial Home ready, range | 2,333–2,480 ms | 2,242–2,697 ms |
| History → Home ready, median of 6 | 653.5 ms | 679.5 ms |

The deterministic improvements are **one removed request** and **40,842 fewer loaded source bytes (3.3%)**. Source bytes are UTF-8 script sizes reported by CDP, not compressed network transfer bytes. The timing ranges overlap; repeat-navigation timing did not improve. This small sample does **not** establish a reliable wall-clock speedup or attainment of the 1.5-second Home target. A benchmark run concurrent with regression work was discarded; the reported before/after samples ran separately. The host remains shared, with no statistical confidence claim.

The before/after benchmark uses fresh browser contexts but a warm local server, local unthrottled assets, fixed-date synthetic athlete data and reduced motion. It is not a real mobile bandwidth test, a server cold-start measurement, or a measurement of production backend/SQL execution. CDP coverage/profile captures are separate from the unprofiled latency samples. The fixture's fixed clock makes native browser PerformanceResourceTiming unsuitable here; request-event timestamps, CPU profiles and script source sizes are used instead.

## Verification

- Production build and typecheck passed.
- Targeted ESLint: zero errors, one existing `set-state-in-effect` warning in `AuthProvider`.
- 13 timer-provider unit tests passed. Node's `--test-force-exit` was used after the original assertion-complete run retained environment/client handles.
- Six browser regressions passed: existing central journey/request budgets, rename invalidation, failed-read retry; new delayed-admission gating, cross-tab account switch/new admission, and Home → Timer → Home → reload/resume.
- Three additional browser checks passed: Home/countdown behaviour at mobile 390px and desktop 1280px, plus the simulated Safari native-media resume workflow in Chromium. This is not a real Safari device claim.
- The first timer open, minimisation, stored-run restoration and audio gesture path are exercised; no writes are acknowledged early and no data freshness window was widened.

Reproduce the fixture timing sample after building with the repository's CI-safe public environment values:

```sh
HOME_BENCH_OUT=/tmp/home.json HOME_BENCH_RUNS=3 npx playwright test e2e/home-startup.spec.ts --grep 'measure Home' --workers=1
```

Run it separately on the base and final builds, with the same browser, machine and public environment values. This environment used a temporary Playwright config pointing at `/usr/bin/chromium`; browser downloads were blocked. Temporary profiles/screenshots/configs are excluded from the PR.

## Remaining Home work

The largest initial cost still sits in the shared bootstrap: SDK/module evaluation and hydration precede the first data request. Loading 40 KB less code helps that work, but most of the loaded source remains. A real-device trace with production monitoring configuration is needed before changing auth SDK or error-monitoring initialization.

Home deliberately retains a fresh Today read because the command view contains readiness/injury decisions. The existing shared cache still reuses plan presentation data, and every non-neutral write invalidates it. This change does not mask stale readiness with an old cached command view. The separate backend profile also identifies repeated static clinical-policy assembly inside fresh Today builds; that work is being investigated separately from these frontend changes.
