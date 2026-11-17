-- An exercise-level note uses its manual_exercise_entries.client_entry_id as source_id.
-- Existing per-athlete isolation, note revisions and model-call budgets are unchanged.
alter table public.training_focus_inferences
  drop constraint if exists training_focus_inferences_source_kind_check,
  add constraint training_focus_inferences_source_kind_check
    check (source_kind in ('sparring_log', 'manual_takeaway', 'manual_exercise', 'plan_completion'));
