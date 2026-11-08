# Canonical State Machines

## Purpose

Status strings are shared by the API, Supabase rows, admin review actions,
workers, and the frontend. This document is the human-readable contract for
those states.

The executable contract lives in `api/state_machine.py`. Status checks and
writes should use that module instead of open-coded sets.

## Status vocabularies

There are three distinct status fields. They may share words but live on
different rows and must not be treated as interchangeable.

| Field | Lives on | Allowed values | Authority |
|---|---|---|---|
| Generation job status | `generation_jobs.status` | `queued`, `running`, `completed`, `review_required`, `failed` | `GENERATION_JOB_STATUSES` |
| Plan status | `plans.status` | `generated`, `ready`, `review_required`, `held_for_review`, `publishable_with_flags`, `triage_blocked`, `medical_hold`, `restricted_rehab_only`, `needs_review`, `archived` | `PLAN_STATUSES` |
| `stage2_status` | plan admin output/audit | `stage2_pass`, `stage2_failed`, legacy/admin audit values, or empty when not run | Stage 2/admin services |

`stage2_status` is an audit field. It is never a plan status or a generation-job
status.

## Generation job states

- `queued`: persisted and waiting for a worker.
- `running`: claimed by a worker.
- `completed`: worker finished and saved a releasable plan result.
- `review_required`: a pre-release planner/triage/admin workflow requires human action.
- `failed`: worker could not produce or persist a usable result.

Allowed transitions:

- `queued` -> `running`, `failed`
- `running` -> `queued`, `completed`, `review_required`, `failed`
- `failed` -> `queued`
- `completed` -> `queued`
- `review_required` -> `queued`, `completed`, `failed`

Self-transitions are allowed for idempotent updates.

## Plan states

- `generated`: legacy/default pre-finalization state.
- `ready`: clean plan available to the athlete.
- `publishable_with_flags`: athlete-visible plan with validator/admin audit findings.
- `review_required`: explicit pre-release review state; ordinary post-plan validator findings do not create it.
- `held_for_review`: explicit admin hold/historical review state.
- `triage_blocked`: injury triage blocked normal generation before a usable plan was produced.
- `medical_hold`: injury triage requires medical clearance.
- `restricted_rehab_only`: triage allows only restricted rehab planning.
- `needs_review`: triage requires review before normal planning proceeds.
- `archived`: hidden from athlete-facing active plan lists.

### Explicit plan transition matrix

| From \ To | generated | ready | review_required | held_for_review | publishable_with_flags | triage_blocked | medical_hold | restricted_rehab_only | needs_review | archived |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| generated | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ready | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| review_required | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| held_for_review | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| publishable_with_flags | ❌ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| triage_blocked | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |
| medical_hold | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| restricted_rehab_only | ❌ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ |
| needs_review | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| archived | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |

## Stage 2 outcomes and release policy

`fightcamp/stage2_policy.py` is the Stage 2 release-policy surface, but it is not
a second planner. Its `apply_stage2_release_policy` is structurally incapable of
holding: it only ever returns `release_decision` of `publish` or
`publish_with_flags`, and always sets `is_athlete_releasable` and `is_publishable`
true. Once Stage 2 has produced non-empty usable athlete-facing plan text,
**validator findings are observational only**.

The invariant is:

> **Planner decides. Validators report. Usable plans ship.**

Four deterministic owners do still apply a hold when the *deterministic planner
output itself* is unusable — but persistence releases it again. See "Deterministic
planner holds" below for what that actually means for the athlete.

For a usable Stage 2 plan with a sound deterministic calendar:

| Validator result | Plan status | `stage2_status` | Job status |
|---|---|---|---|
| no findings | `ready` | `stage2_pass` | `completed` |
| one or more findings of any code/severity | `publishable_with_flags` | `stage2_pass` | `completed` |

This includes findings labelled as:

- `goal_preservation_render_mismatch`;
- restriction/safety or late-fight validator findings;
- dose/evidence mismatches;
- admin-review classifications;
- unknown validator codes;
- `severity="blocker"`; and
- malformed validator collections.

The original finding remains in the validator/admin report. It may be surfaced
for QA and asynchronous review. On its own — as a judgement about the quality of
the rendered text — it cannot blank `plan_text`, change a usable plan to
`review_required`, force planner regeneration, or trigger another model call
solely to make validation pass.

### Deterministic planner holds

Four findings are not quality judgements: they say the deterministic plan handed to
(or returned from) the model is structurally unusable. Each is applied by a named
owner *after* the release policy has run — the release policy itself never holds.

| Hold | Applied by | Trigger |
|---|---|---|
| Planner preflight | `fightcamp/planner_authority_integrity.py`, via `build_stage2_package` | Stage 1 handed over a plan the finalizer must not be asked to render. Held before the first model call. |
| Structural integrity | `stage2_pipeline.apply_structural_integrity_hold` | `phase_section_missing`, `missing_week_session_role`, `late_camp_session_incomplete` or `late_fight_missing_required_countdown_session` survives deterministic repair. |
| Conditioning-render | `api/stage2_automation.py` | `missing_selected_conditioning_assignment` / `selected_conditioning_effective_prescription_mismatch` unresolved after the deterministic reconciliation and the one repair call. |
| Goal-preservation regeneration | `goal_preservation.validate_goal_preservation` | `goal_preservation_failed`: selected goal coverage is unmet and needs deterministic planner repair. |

A hold sets `release_decision = "hold"`, `is_athlete_releasable = false`,
`is_publishable = false` and `validator_findings_observational = false`; moves the
rendered text to `final_plan_text`; blanks `plan_text`; and produces plan status
`review_required` with `stage2_status = stage2_failed`. Inside Stage 2 that is
final — nothing in `api/stage2_automation.py` may upgrade it, and because
`should_attempt_structured_plan` requires an athlete-displayable status, a held
plan gets **no structured card**.

### The release override

`api/generation/persistence._release_held_plan_with_flags` then runs last, on every
Stage 2 result, and rewrites any `review_required` / `held_for_review` plan that has
usable content to `publishable_with_flags` — restoring `plan_text` from
`final_plan_text` when a hold blanked it. Every validator and contract finding is
preserved. The same override also cancels the `review_required` downgrade
`_apply_plan_contract_validation` applies immediately before it. Only a genuinely
empty result (no plan text, no final text, no clean card) stays held.

So in production:

- **the invariant above holds end to end** — a plan with usable content ships;
- a deterministic hold is an **audit signal and a card suppressor**, not a gate: the
  athlete gets the plan as `publishable_with_flags` on the raw markdown fallback;
- `stage2_status` stays `stage2_failed` through the override, so admins can still
  find every held-then-released plan;
- the only genuinely withheld outcomes are Stage 1 injury triage — which uses the
  separate `persist_triage_review_required` path and is **not** overridden — and an
  empty result.

The override is deliberate and locked by `tests/test_validator_release_invariant.py`.

Note that `goal_preservation_failed` and `goal_preservation_render_mismatch` are
**not** the same finding. The render mismatch is a rendering discrepancy that ships
with flags and never holds; `goal_preservation_failed` is a coverage failure that
does apply a hold (and is then released with flags like any other).

### True terminal failures

The non-blocking validator rule applies only after usable Stage 2 text exists.
Generation can still fail when there is genuinely nothing usable to publish or
release is technically impossible, for example:

- provider/runtime failure before a usable Stage 2 response exists;
- an incomplete provider response with no extractable plan text;
- an empty/unparseable response that yields no athlete-facing plan;
- persistence/database failure; or
- a Stage 1 triage/planner gate that stops generation before a usable plan exists.

A deterministic planner hold is not in this category either: the job completes, the
plan row exists, and the release override makes it athlete-displayable with flags.

A provider response marked incomplete **with usable Stage 2 text** is not in this
category. Its text ships as `publishable_with_flags` and the incomplete-response
finding remains in audit telemetry.

### Post-generation plan-contract validator

`fightcamp/plan_contract_validator.py` still checks calendar/payload invariants
and stores its report in `why_log`. Those findings are also observational. They
cannot downgrade an already-visible `ready`/`publishable_with_flags` plan to
`review_required`.

The contract validator is therefore a drift detector, not a release veto. Fix a
reported calendar/payload defect in the canonical planner/renderer owner rather
than withholding the plan because the validator disagrees.

### Structured-card conversion

Structured cards are projections of an already-approved Stage 2 result. They do
not own release policy and cannot override planner state.

Because ordinary validator findings no longer create a hold, structured
conversion may proceed for `ready` and `publishable_with_flags` plans. If the
structured conversion fails validation, the raw Stage 2 `plan_text` remains the
athlete-facing fallback. A structured-card problem must not destroy usable Stage
2 text.

A phrase such as **“structured card cannot rescue a held Stage 2 result”** means
only that the card is not a competing release authority. It does not grant
validators permission to create a hold after usable plan text exists.

## Plan status to generation job status

Terminal generation-job reporting is derived from saved plan status by
`job_status_for_plan_status(...)`.

| Plan status | Generation job status |
|---|---|
| `generated` | `completed` |
| `ready` | `completed` |
| `publishable_with_flags` | `completed` |
| `archived` | `completed` |
| `review_required` | `review_required` |
| `held_for_review` | `review_required` |
| `needs_review` | `review_required` |
| `medical_hold` | `review_required` |
| `restricted_rehab_only` | `review_required` |
| `triage_blocked` | `review_required` |
| unknown | `review_required` |

Unknown *plan statuses* still fail closed at the state-machine mapping layer.
That compatibility guard is distinct from an unknown validator finding attached
to an otherwise usable plan; the latter ships with flags.

## Athlete-facing labels

The UI never shows raw status strings. `web/lib/plan-labels.ts` maps them to
human copy.

| Status | Athlete-facing label |
|---|---|
| `ready` | Ready |
| `publishable_with_flags` | Ready — review notes included |
| `review_required` | Awaiting review |
| `held_for_review` | Awaiting review |
| `triage_blocked` | Paused for safety review |
| `generated` | Processing |
| `archived` | Archived |

Keep this table, `web/lib/plan-labels.ts`, and `api/state_machine.py` in sync when
a status is added or relabelled.

## Protected-state resume contract

- `approve-and-resume-generation` is only allowed for triage modes `needs_review` and `restricted_rehab_only`.
- `medical_hold` is intentionally non-resumable through approve-and-resume.
- Resumed jobs may end with a releasable plan, remain in an explicit triage/admin state, or fail for a genuine runtime/persistence reason.

## Backend invariant for terminal jobs

For generation jobs ending in `completed` or `review_required`, `plan_id` must
point to an existing `plans` row. Otherwise the job is downgraded to `failed` to
avoid exposing orphaned terminal links.

## Athlete-displayable plan statuses

`ATHLETE_DISPLAYABLE_PLAN_STATUSES` names the states shown to the athlete:

- `ready`
- `publishable_with_flags`

Use `is_athlete_displayable_plan_status(...)` instead of open-coded
`status == "ready"` checks.

Triage/admin hold states remain excluded because they represent a workflow that
stopped before ordinary automatic athlete release, not a post-plan validator
opinion.

## Implementation rule

Use the central state-machine helpers, for example:

```python
can_transition("generation_job", "running", "review_required")
can_transition("plan", "review_required", "ready")
can_transition("generation_job", "running", "failed")
```

Do not introduce scattered status sets or a second release authority in route
handlers, workers, validators, renderers, or structured-card code.

### Stage 2 latency diagnostics

The generation milestone `stage2_model_call_started` surrounds the entire
finalizer, not just the provider call. Likewise,
`stage2_model_response_received` is emitted after finalization and a heartbeat
write. Do not subtract model-call costs from that interval and attribute the
remainder to provider latency.

The validator report now retains `stage2_timings`: preparation, plan model
(including a deadline retry), validation and deterministic repair, inline card
conversion, total finalization, and (when applicable) pre-release card retry.
Monotonic durations are seconds. Logs also record heartbeat and plan/result
persistence durations. Model request logs distinguish stream opening from
awaiting the final response. Conversion timing includes prompt construction,
model calls, schema/faithfulness/safety checks and deterministic recovery.

A held result can skip inline card conversion, then become
`publishable_with_flags` under persistence's existing release override.
The pre-release card gate handles that case. Its source
`generation_card_retry` does not imply a previous card model request.
Do not repeat a conversion whose card validators already rejected it.

#### November 8, 2026 investigation

Job `a0470cc7-145f-441e-8b3e-099b2100eb1c`, inspected against Main
`13c89223`:

- Created 12:55:50.617509; claimed 12:55:56.814784.
- Finalizer wrapper milestone: 12:56:11.399748–13:02:59.894794
  (408.495 seconds); recorded text call 46.73 seconds.
- Card retry: 13:03:00.313825–13:05:07.783310 (127.469 seconds);
  recorded card call 127.34 seconds.
- Plan persistence: 13:05:08.398326–13:05:12.680194 (4.282 seconds).
- Final-result persistence: 0.262 seconds; completed 13:05:13.819895.

Saved final text and Stage 1 brief replayed locally with model calls mocked:
0.337 seconds under cProfile before changes (including optional card preparation).
This does not reproduce production network waits, process contention or provider
SDK retries. It does not establish where the missing 361.765 seconds occurred.
Historical worker logs and the deployed revision are still needed for that
attribution; the job row alone cannot resolve it.

The Anchor warning was a false positive: the open-plan weekday Tactical Review
contained the explicit tactical-only/no-physical-load contract but the existing
exception parsed only countdown sessions. The narrow exception now recognizes
that format and still checks every occurrence for leaks outside tactical review.

Synchronous heartbeat writes discarded their return value but requested the
default full-row read-back, including the large Stage 1 payload. They now use
the existing refresh=False option, as the periodic heartbeat already does.
This removes one database read per synchronous heartbeat. Savings are
network-dependent; neither this change nor the anchor fix has a measured
production latency improvement. In particular, a 362-second improvement must
not be claimed from the available evidence.

Successful deadline recovery previously discarded the first failed request's
cost telemetry. The failed and successful attempts are now merged. Thus an
absence of failed attempts in older records is not conclusive proof that none
occurred. This is a telemetry defect, not evidence that this job retried.
The incident's successful call is labelled first_pass, whereas current deadline
recovery uses first_pass_retry.

Card validation logs separately time parsing and schema/faithfulness/safety
validation for each model conversion attempt. No model, reasoning setting,
safety policy, deterministic fallback or card repair gate was changed.
