-- Athlete-directed exercise changes (swap / add / remove) on an app-generated
-- plan, kept beside the plan rather than inside it.
--
-- The generated card (plans.structured_plan) is never edited: it stays the AI
-- plan's source of authority and the provenance every change points back to.
-- Each entry in exercise_overrides names one change, its scope (one dated
-- occurrence, or that workout's future occurrences of an open plan), the
-- original block it replaced and the athlete's selection. Readers apply the
-- active entries after the calendar is projected (api/services/
-- exercise_overrides.py), so Plan Detail, Today, logging and session start all
-- see the same effective workout.
--
-- Writes are service-role only and compare-and-swap on
-- exercise_overrides_revision, so concurrent edits from two devices cannot
-- silently drop one another. An undo marks an entry revoked; nothing is
-- deleted, and completed sessions and exercise logs keep their own frozen
-- copies of what was prescribed.
alter table public.plans
  add column if not exists exercise_overrides jsonb not null default '[]'::jsonb,
  add column if not exists exercise_overrides_revision integer not null default 0;

alter table public.plans
  drop constraint if exists plans_exercise_overrides_shape;
alter table public.plans
  add constraint plans_exercise_overrides_shape check (
    jsonb_typeof(exercise_overrides) = 'array'
    and jsonb_array_length(exercise_overrides) <= 500
    and octet_length(exercise_overrides::text) <= 1048576
    and exercise_overrides_revision >= 0
  );
