# Slim plan reads and auth cold path (2026-11-05)

Base: `6fde133` (#3198). All production figures come from read-only queries against the connected `unlxckpdf` project. The migrations are already applied there.

## Why

Earlier statistics showed `plans.*` reads by id reaching **5.6–7.6 s** at worst (28,620 calls for the `id + athlete_id` shape). Recent `plans` rows are large:

| Column | Avg | Max |
| --- | ---: | ---: |
| `planning_brief` | 536 KB | 1.01 MB |
| └ `candidate_pools` inside it | ~400 KB | 653 KB |
| └ `weekly_role_map` inside it | ~214 KB | 429 KB |
| `stage2_payload` | 461 KB | 768 KB |
| `stage2_handoff_text` | 130 KB | 352 KB |

Who reads what:

- **`candidate_pools`:** outside generation, only `exercise_identity._pool_keys` reads it. That runs only inside plan detail (`_map_plan_detail` → `reconcile_exercise_keys`).
- **Stage-2 blobs:** read only by admin outputs and generation.
- **Today/XP:** every build read `TRAINING_PLAN_SELECT`, which included the whole brief.
- **Athlete whole-plan reads:** active-plan resolution and the plan viewer read `select=*`.

Auth after a restart: the first user session after a deploy made two concurrent `/auth/v1/user` calls (~350 ms each). `SupabaseJwksVerifier._refresh` stamped the fetch time and released its lock *before* fetching. Concurrent requests therefore saw a "recent" attempt with no keys and fell back upstream. Nothing loaded the keys at startup.

## Changes

- **`public.planning_brief_runtime(plans)`** (migrations `20261104235733`, `20261105001500`) is a PostgREST computed field. It returns the brief without `candidate_pools`, keeping the same key and the same text type.
  - It parses once, and not at all when the text has no `candidate_pools` key.
  - Invalid JSON and non-object briefs are returned unchanged.
  - Only `service_role` may execute it. It is registered in `REQUIRED_FUNCTIONS`, so the deploy gate blocks code that ships without it.
  - The first version parsed up to 4 times (33 ms/row) and was replaced before any code used it.
- **`get_training_plan_for_athlete`** selects `planning_brief:planning_brief_runtime` (`TRAINING_PLAN_REMOTE_SELECT`). This read serves Today, XP, streaks, week progress, exercise customisation and logs, and focus inference.
- **`get_plan_for_athlete`** selects `ATHLETE_PLAN_SELECT`: the gated `REQUIRED_PLANS_COLUMNS` minus `stage2_payload`, `stage2_handoff_text` and `stage2_retry_text`. Plan detail keeps the full brief.
  - Admin views and admin renames read through `get_plan`, which is unchanged.
- **JWKS:** one fetch runs at a time under a fetch lock. Requests arriving meanwhile wait for its keys, bounded at 4 s, instead of going upstream. The 600 s TTL and 30 s refetch floor are unchanged.
  - `warm()` loads the keys from a detached lifespan task.

## Measured (production data, read-only)

Payload per read of each athlete's active plan (13 active plans), serialized as PostgREST does:

| Read | Before | After |
| --- | ---: | ---: |
| Today/XP training projection | 525 KB | **197 KB** (−62%) |
| Athlete whole-plan read (active-plan resolution, viewer) | 1,135 KB (max 1.90 MB) | **585 KB** (max 1.12 MB, −48%) |

Database time to build the JSON, per active plan (`json_agg`, `explain analyze`, warm):

| Expression | ms/row |
| --- | ---: |
| full `plans.*` row | 12.2 |
| athlete projection | **5.3** |
| training projection, raw `planning_brief` | 4.5 |
| training projection via `planning_brief_runtime` (single parse) | 11.5 |
| first function version (four parses, replaced) | 33 |

Python decode of the two passes (response, then brief text) at these sizes on Python 3.11, locally: **4.95 ms → 0.61 ms** per read.

**Net, per Today/XP plan read:** +7 ms database CPU, about −4 to −8 ms Python CPU, and −395 KB transfer between Supabase (Paris) and the API (Nuremberg). This is a modest gain.

**Net, per athlete whole-plan read:** −6.9 ms database time and −550 KB, at no added cost. That covers the 28k-call shape.

Auth, regression test: 8 concurrent first requests on a cold verifier.

| Code | JWKS fetches | Upstream `/auth/v1/user` calls |
| --- | ---: | ---: |
| Before | 1 | **7** |
| After | 1 | **0** |

Not measured: the effect on the 5.6–7.6 s worst cases, and real auth calls after a restart. Both need a deploy. Then check:

- edge-log `response.origin_time` and size for `plans?select=…`;
- the number of `/auth/v1/user` calls in the first minute after a backend boot (`runtime_schema_introspection` marks the boot).

## Tests

- **`tests/test_slim_plan_reads.py`:**
  - athlete plan detail is identical without the stage-2 blobs, and exercise keys are still stamped from the pools;
  - detail output changes without the pools, which is why detail keeps the full brief;
  - Today and XP are identical with and without `candidate_pools`;
  - the in-memory function mirrors the SQL one.
- **`tests/test_backend_projections.py`:** exact selects; `ATHLETE_PLAN_SELECT` is tied to the schema gate; the function is gated.
- **`tests/test_auth_local_jwt.py`:**
  - concurrent cold start: one fetch, no upstream calls (7 upstream calls on the old code);
  - `warm()` loads the keys;
  - a failing fetch stays rate-limited.
- **FakeStore** mirrors both projections, so a runtime consumer that needed the pools or blobs would fail in tests.
- **`tests/test_today_readiness_boundary.py::test_check_in_delegates_to_canonical_service`:** this test was broken by #3198 and is fixed here. It now checks that the boundary delegates to the canonical check-in service through the shared read cache, rather than asserting function identity.
- **Targeted suites (34 files):** 810 passed, 2 failed. Both failures also fail on untouched `6fde133`: `test_library_dose_fields_follow_the_drill[Tempo Hill Runs-intervals]` and `test_index_requirement_satisfied_by_constraint_alias`.
