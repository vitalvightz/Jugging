# Home / Today policy-work follow-up — 2026-11-04

Base: `b898cbce` (#3194). This backend change is independent of the frontend Home startup PR and #3195's rehab-label fix.

## Bottleneck and minimal fix

Home waits for a fresh `/api/today` command view, which assembles clinical policies when injuries are present. A profile identified repeated region normalisation inside `expand_bank_prescriptions`: each policy's constant region was normalised again for every bank group. This contributed 31,710 normaliser calls during compilation of the 64 packaged policies.

Compute the policy region once before its bank loop. This removes 15,791 calls (31,710 → 15,919); the added call for an empty bank is included in these totals. The normaliser is pure and the policy is frozen. All bank entries, duplicate-drill counts, prescriptions, stage permissions, source/hash checks and safety validation still run. No cache, data freshness change, dependency or background process is added. No injury decision, user data, database query or authentication result is reused across requests.

## Measurements

[Raw paired samples](home-policy-paired-2026-11-04.json). Ten alternating before/after pairs, local warm CPU, same packaged bank/catalog and fixed-date injury-history fixture. Call counts were collected separately with wrappers; the timings below do not include those wrappers or CPU profiling. The base function was loaded from `b898cbce:fightcamp/rehab_publication.py` and substituted in the same runtime, keeping every other function constant.

| Operation | Before p50 | After p50 | Before p95/max | After p95/max |
| --- | ---: | ---: | ---: | ---: |
| Compile all packaged policies | 61.6 ms | 53.7 ms | 97.0 ms | 80.1 ms |
| Build complete injury-history Today view | 209.4 ms | 199.8 ms | 576.4 ms | 364.5 ms |

Every pair asserts **exact equality** of all serialized policy objects or the entire serialized Today command view. The output hashes are recorded in the artifact. This checks that the reduced work preserves the full data contract, not only counts or selected fields.

This is a small CPU reduction, not an explanation of the reported 5–6 second navigation delay. Timings exclude network, auth/profile resolution, real database execution and browser rendering. The shared host and small sample do not establish production percentiles or a guaranteed speedup. The full Home 1.5-second target remains unverified/unmet in the constrained-browser fixture.

## Verification

The existing policy publication, pathway architecture, policy/hash/stage safety invariants and Today read-amplification suites were run: 266 passed and 3 failed. The three publication failures were independently checked on untouched `b898cbce` before delivery. They concern the selected Achilles drill, a LOAD permission/response hold, and exact-duration frozen LOAD expectations; they are not repaired in this performance patch.

Python compilation and `git diff --check` passed. No schema or migration changes.

Reproduce the validation suite:

```sh
APP_ENV=test UNLXCK_ENV=test ENVIRONMENT=test NODE_ENV=test python -m pytest -q \
  tests/test_rehab_bank_publication.py tests/test_rehab_pathways.py \
  tests/test_rehab_pathway_safety_invariants.py tests/test_today_read_amplification.py
```

For paired CPU measurement, warm the existing `fixture("injury_history")` and NLP model, then alternate the original and final `expand_bank_prescriptions` while invoking `load_clinical_policies(bank=get_rehab_bank())` and `build_today_command_view(..., now=NOW)`. Time with `perf_counter` outside profiling/count wrappers and compare full `model_dump(mode="json")` output after each pair. Keep the application date fixed and retain first samples/tails; do not call these SQL or user-perceived times.
