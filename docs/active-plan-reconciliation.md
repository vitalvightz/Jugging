# Active-plan beta account reconciliation

`profiles.active_plan_id` remains the sole runtime authority. When it does
not reference a usable plan, application reads may conditionally activate the
**only non-archived, eligible** plan using an atomic pointer update. Multiple
non-archived plans never trigger automatic selection. Ambiguous historical
accounts still require an audited support reconciliation through
`set_active_plan(...)`. The automatic single-plan rule uses eligibility and
ownership, **not** historical activity evidence; it does not write a separate
support audit record. The evidence order below applies only to manual repairs.

## Evidence order

For an account with a null pointer, inspect evidence in this order:

1. An explicit activation record retained in backend or support audit history.
2. The one plan that legacy Today or Overview demonstrably served to the athlete.
3. The plan on the newest valid `done` or `modified` session completions.
4. The plan on recent Today check-ins.
5. A sole eligible saved plan, only when every available signal agrees and no
   other eligible plan makes the choice ambiguous.

At each step, require one plan to win unambiguously. Confirm that it belongs to
the athlete and is still eligible under `get_plan_activation_state(...)`. Stop
without changing data if evidence conflicts, is missing, or points to an
archived, ended, unavailable, or foreign plan.

## Safe repair procedure

1. Record the athlete id, candidate plan id, evidence source, operator and time
   in the support case.
2. Preview the candidate and verify ownership and activation state.
3. Use `set_active_plan(...)`; do not update `profiles` directly. This preserves
   validation and the existing overlap pause/replace decision.
4. Re-read `/api/plans/active`, Today and Progress. Weekly progress and adherence
   then reconcile from completion history for that exact plan.
5. Leave ambiguous accounts with no active plan. Ask the athlete to activate a
   saved plan, or escalate for manual review.

This process never deletes completion rows and never treats recency alone as
proof of activation.
## Training streak and adherence repair

These are separate derived views. `athlete_streaks.training_*` drives the
athlete-facing Training Streak. It uses completed, legitimate
`session_completions` plus completed legacy/manual `session_logs`, deduplicated
by athlete-local training day. A completed day advances once. An expected day
missed before today, or an explicit skip, breaks the run. A genuine rest day and
an unresolved current day are neutral. `athlete_streaks.adherence_*` remains the
stricter prescribed-plan measure and is not displayed as Training Streak.

A terminal completion triggers both rebuilds independently of XP eligibility.
Activating a plan also runs both, so re-activating the existing active plan is
the supported deterministic repair path after deployment. Neither rebuild
awards XP.

For a deployment-wide repair, invoke that same service reconciliation once for
each athlete with an active plan (for example from an authenticated maintenance
job). Do not update streak counters in SQL. The service rebuild is idempotent and
preserves deduplication, rest-day, missed-day and active-plan rules.
