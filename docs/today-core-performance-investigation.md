# Today core and request-overhead investigation

Follow-up to the iPhone diagnostics report (Today first visit 1.06s, revisit
1.35s; `today_core` 256–308ms of a ~660ms backend build; 200–370ms per API
request outside the measured `app` span).

## `today_core` is CPU, not reads

With every Data API read delayed by 50ms, a Today build for an athlete with
three open injuries issued **no uncached read inside `today_core`** until the
final consent read. Every input the core uses is already served by the
concurrent prefetch. The time inside `today_core` was Python CPU.

cProfile of the same build (per build, before this change):

| Work | Calls per build | Note |
| --- | --- | --- |
| spaCy Negex (`remove_negated_phrases`) | ~210, over **27 distinct strings** | ~50% of core time |
| `score_injury_phrase` | 40 | regex sweep over every synonym, per call |
| `load_clinical_policies` | 2+ | re-reads and re-validates two JSON files each time |

All three are pure functions of their input and module constants, so the
change memoizes them:

- `injury_negation._remove_negex_phrases`: keyed on (pipeline, text); returns a
  string. When spaCy is unavailable or disabled, the regex fallback still runs uncached.
- `injury_scoring._score_default_maps`: keyed on the negation-stripped text,
  only for the default maps; every caller gets its own dict and `flags` list.
- `rehab_clinical.load_clinical_policies()`: reused while both files' path,
  mtime and size are unchanged; policies are frozen models and nothing mutates
  them. An explicit `path` or `bank` composes fresh.

No safety decision changes: each cached value equals a fresh computation
(pinned in `tests/test_today_core_cpu_caches.py`). In the full suite, 565
tests passed before the run was stopped; the failures it hit
(`test_achilles_load_activation`, `test_api_types_generated`,
`test_custom_open_plans`, `test_computed_support`) fail identically on the
base commit.

Measured locally on the same fixture, with production settings
(`UNLXCK_DISABLE_SPACY=1`, as `compose.yaml` sets for the API):
`today_core` ~111ms → ~21ms warm, ~169ms → ~50ms on the first build in a
process. The spaCy cache only matters where spaCy is enabled (the generation
worker, local runs); in the API the gains come from phrase scoring and policy
loading. Production times differ by hardware and injury count; check the
next iPhone report.

The largest remaining core item is structured-plan projection
(`resolve_effective_structured_plan`, ~40ms here). It shapes session
decisions, so it was left alone.

## Request overhead: where the 200–370ms can be

The path is iPhone → Vercel (Next.js `/api` rewrite) → Hetzner Caddy → uvicorn.
`app` starts at the first FastAPI middleware, so the gap includes both network
hops, Caddy, and any wait before the event loop picks the request up.

The API runs a **single uvicorn worker**. A CPU-bound Today build holds the
GIL, so other requests issued at the same time (Home and Today fire several)
wait before their `app` span starts. Reducing core CPU should therefore also
shrink the "outside app" gap; the new measurements below will show by how much.

New measurements, all diagnostic and free of personal data:

- **Server-Timing `queue`**: Caddy stamps `X-Request-Start: t=<unix ms>`
  (overwriting any client value); the app reports the gap to its own start.
  Stamps in the future or over 60s old are dropped.
- **Per-request `phases`** in the perf report (from PerformanceResourceTiming):
  `setup` (stall/DNS/TCP/TLS), `wait` (sent → first byte), `download`.
- **Per-request `requestId`**: the API's random 8-character `X-Request-ID`, to
  find a slow request in the API and Caddy logs.

Reading the next report: `wait − queue − app` is the phone ↔ web host ↔ Caddy
network time. A large `queue` points at event-loop or worker saturation. A
large `setup` points at connection reuse.

## Not covered here

- The occasional ~315ms blocking Today route payload (`_rsc`) request.
- History's main request (327ms app / 578ms total).
- Cold start (HTML first byte and the first API request).
