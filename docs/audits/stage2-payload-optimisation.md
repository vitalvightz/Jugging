# Stage 2 payload audit

Baseline: `43db0aed` (includes #3228). This audit does not establish that payload
size caused the reported 13m33s generation. The supplied incident has no job ID,
per-call usage records, final markdown, raw responses or timing trace in this
checkout. No production model calls were made.

## Pipeline and latency

1. `fightcamp/main.py:generate_plan_sync` parses the athlete, builds runtime
   context, generates deterministic plan blocks and renders the Stage 1 draft.
   `plan_pipeline_rendering.build_stage2_outputs` builds the rich payload, brief
   and handoff. Candidate pools support deterministic selection and validation;
   they are not the model's exercise menu.
2. `stage2_payload.build_stage2_handoff_text` applies the LLM boundary projection,
   creates the compact finalizer packet, then joins the finalizer prompt, render
   contract, mode instructions, locked manifest, packet, athlete profile, injury
   context, coach notes, draft (nutrition removed) and completeness check.
   `stage2_finalizer_packet_impl` already excludes candidate pools, removes
   assignment provenance and duplicated calendar fields, and filters writing
   rules covered by the prompt. Full briefs remain available to validators.
3. `api/generation/orchestrator.py` invokes the whole-finalize wrapper in
   `stage2_runner.py`. `Stage2Automator.finalize` builds the package, preflights,
   sends `first_pass`, reconciles authoritative programming and validates output.
   A plan-text deadline triggers one `first_pass_retry`; ordinary content gaps
   use deterministic reconciliation, not another content-generation call.
4. `_attempt_structured_plan` sends approved markdown to
   `build_structured_plan_prompt`. It strips planner-owned locked sessions for
   deterministic reinsertion; uses a context allowlist capped at 8,000 characters;
   includes authoritative computed support, conversion rules and a root skeleton.
   It does not resend the Stage 2 handoff or full candidate pools. Parsing,
   normalization, schema validation, safety, faithfulness, calendar and sparring
   reconciliation still govern the result. `structured_repair`, when explicitly
   enabled, repeats the source with errors and up to 12,000 characters of broken
   JSON. It is off by default. Pre-release card handling avoids repeating terminal
   failed conversions; see `retry_structured_card_before_release`.

SDK retries default to zero (environment overrides exist). Plan call deadlines
are 180s; structured deadlines are now 480s, previously 720s. SDK stream-gap
timeouts differ from the total call deadline. The whole-finalize/job runtime
budget adds another boundary. Defaults and models are unchanged by this PR.

813 total seconds minus approximately 40 Stage 1 seconds leaves approximately
773 seconds. A 720-second structured deadline plus 53 seconds elsewhere is
arithmetically consistent with that report, but is **not an incident diagnosis**.
Model reasoning/output time, transport delays, deadline retry and an enabled
structured repair must be checked independently. Existing `stage2_calls` records
include label, elapsed seconds, input/output/reasoning tokens, prompt/response
characters and validation outcome. Use those with job milestones and environment
overrides to attribute the incident; prompt length alone cannot do so.

## Evidence and minimal changes

The representative two-week rich brief is 482,867 characters using default JSON
serialization. Largest fields: candidate pools 319,149; weekly role map 91,570;
selection summary 26,092; decision rules 10,215; progression 9,997. Those internal
objects explain why brief size need not track programme length closely: exercise
banks and selection machinery have a fixed cost even for a short camp.

The two-week handoff is already much smaller: 101,909 characters. Its packet is
58,930; main prompt 25,341; locked manifest 6,524; render contract 5,652; athlete
profile 3,606; remaining mode/injury/draft/completeness sections are smaller.
The packet's selected-plan payload contains the calendar, progression and
fight-week continuation needed even by a short programme. The incident's
638,620/122,323 character counts are not reproduced by this different athlete
fixture; they must not be presented as measured before/after production values.

Changes:

- Send identical athlete fields once in `FINALIZER PACKET.athlete_model`.
  `ATHLETE PROFILE` retains every additional or conflicting value, with explicit
  precedence instructions. No source brief or packet object is mutated.
- Compact whitespace in the structured converter's two JSON context blocks.
  Keep the existing context-selection budget unchanged, so compaction does not
  accidentally admit more context or change which fields are included.

Retained: locked manifest plus selected assignments (explicit render completeness
and authority), draft plus programme (source for conversion), prescriptions and
ceilings, injury/support context, taper/continuation contracts, sparring rules,
progression and phase context, coaching notes, all validators. Some overlap is
intentional and cannot safely be removed merely because text looks similar.
No candidate-pool cap, schedule trimming, changed exercise selection, new model,
extra AI call, timeout change or validator change is introduced.

## Offline validation

Reproduce from repository root:

```sh
.venv/bin/python tools/audit_stage2_payload.py 43db0aed > metrics.json
```

Uses the existing fixed-clock/seed boxing camp fixture at D-14/28/56/84 with
power/speed goals, four weekly sessions and Thursday hard sparring. It exercises
real deterministic planning and handoff assembly. Its Stage 1 draft is deliberately
minimal, so structured-prompt sizes exclude a full generated programme and cannot
stand for live conversion sizes. Longer programmes retain their complete calendar;
shorter handoffs are smaller already, with no forced length-based truncation.

| Weeks | Handoff chars before → after | Estimated input tokens before → after | Structured chars before → after | Estimated structured tokens before → after |
|---|---:|---:|---:|---:|
| 2 | 101,909 → 100,897 | 25,478 → 25,225 | 18,941 → 18,432 | 4,736 → 4,608 |
| 4 | 141,533 → 140,535 | 35,384 → 35,134 | 19,294 → 18,752 | 4,824 → 4,688 |
| 8 | 228,752 → 227,754 | 57,188 → 56,939 | 19,293 → 18,670 | 4,824 → 4,668 |
| 12 | 319,720 → 318,721 | 79,930 → 79,681 | 15,754 → 15,499 | 3,939 → 3,875 |

Token figures use ceil(characters/4), matching the application's fallback estimate;
these are not model-tokenizer or billing measurements. Tokenizer vocabulary download
was denied by the environment proxy. Structured context varies with selected phase
context and its existing cap; actual conversion also scales with source markdown.
Handoff savings are 0.31–0.99%; structured savings here are 1.62–3.23%. They are
useful small reductions, not evidence of a major latency improvement.

The audit asserts exact reconstruction of athlete profile, byte equivalence of
all other handoff sections (apart from the added merge instruction), and equality
of structured prompts after JSON whitespace normalization for all four durations.
Programme choices/sets/reps/progression/schedule/goals/safety/taper/sparring values
are therefore unchanged in the measured inputs. Model-generated programme/card
output equivalence is **not measured**: no live A/B generation or incident replay.
No measured model latency improvement is claimed. Deterministic tests do not prove
stochastic output equivalence; retain this limitation when reviewing the PR.

Tests: 308 passed across profile deduplication, tactical identity, structured
conversion, Stage 2 automation and late-camp fight-week handoff. An additional 161 tests passed across structured safety, faithfulness, calendar,
sparring reconciliation and card spine suites (469 total; no remaining failures).
The initial run exposed the expected profile-location assertion, which was
updated to assert the reconstructed profile; the subsequent run passed.
Per-section character/token estimates are retained in
[stage2-payload-metrics.json](stage2-payload-metrics.json). Environment uses
available fast-lane dependencies; spaCy/NegEx are absent and injury parsing reports
degraded mode. Injury parser equivalence with production NLP is not claimed.
