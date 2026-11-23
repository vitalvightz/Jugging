-- NULL preserves legacy ambiguity. Never backfill by date/title/timestamp.
-- The existing row UUID is the reported execution identity; sibling credit
-- remains in this table for day/week progress, not physical workload/contact.
alter table public.session_completions
  add column if not exists completion_origin text
    check (completion_origin in ('reported', 'day_credit'));

alter table public.session_completions
  drop constraint if exists session_completions_credit_has_no_actuals;
alter table public.session_completions
  add constraint session_completions_credit_has_no_actuals check (
    completion_origin is distinct from 'day_credit' or (
      session_rpe is null and pain_after is null and notes = ''
      and prescription_snapshot is null and rehab_performance is null
      and rehab_response_contexts is null
    )
  );

comment on column public.session_completions.completion_origin is
  'Server-owned provenance: reported execution, day_credit for progress only, NULL for unverified legacy. No inferred historical grouping.';
