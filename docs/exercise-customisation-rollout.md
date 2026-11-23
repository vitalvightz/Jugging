# Exercise customisation rollout — PR #2847

Keep the PR unmerged until its required checks pass. Backend rollout requires
the database migration first; existing plan reads now require these columns.

## Confirmed database migration

On 12 October 2026, the user confirmed `unlxckpdf`
(`leienvqynijrgghhzczt`) as the migration target. Supabase identifies this
project as its default `Main` branch; no separate staging branch was listed.

Applied the SQL from
`supabase/migrations/20261012071454_add_plan_exercise_overrides.sql` through
Supabase's migration API. The API recorded it as version `20261012071454`,
name `add_plan_exercise_overrides`. Operators using CLI migration history
must account for this version difference before a later `db push`.

Verified:

- `public.plans.exercise_overrides`: JSONB, NOT NULL, default `[]`.
- `public.plans.exercise_overrides_revision`: integer, NOT NULL, default `0`.
- `plans_exercise_overrides_shape`: validated array, size and revision checks.
- Existing plans received empty overrides and revision zero.
- `tools/run_supabase_runtime_schema_gate.py` passes against this project.

## Deployment order

1. Confirm the production API/worker database project and compare it with the
   confirmed project above. Do not assume the workflow's staging label means
   there is a separate database.
2. If production uses another project, apply the same migration there before
   deploying this backend. Verify the columns and constraint.
3. Run the live runtime schema gate with that environment's credentials;
   retain the existing mandatory deployment gate.
4. Require passing PR checks, then coordinate merge and backend rollout.
   Verify swap, add, remove and undo on an athlete-owned test plan, including
   one-workout and recurring scopes.

## Remaining CI investigation

The original PR's Python Quality jobs fail historical metadata replay with
`Historical source drift: api/contracts/injury_policy.py`. This reproduces
locally, and that source has the same Git blob in the PR base and `Main`.
Resolve the historical audit evidence separately; do not refresh frozen
baselines or weaken preservation checks to make CI pass.
