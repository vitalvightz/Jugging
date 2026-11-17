-- Allow longer, grounded coaching instructions and internal mental-skill matching.
alter table public.training_focus_inferences
  drop constraint if exists training_focus_inferences_focus_check,
  add constraint training_focus_inferences_focus_check check (focus is null or char_length(focus) <= 360),
  drop constraint if exists training_focus_inferences_scope_check,
  add constraint training_focus_inferences_scope_check check (
    scope is null or scope in ('specific_exercise', 'movement_pattern', 'general_quality', 'sport_technique', 'mental_skill')
  );
