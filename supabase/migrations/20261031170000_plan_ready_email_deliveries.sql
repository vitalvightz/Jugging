-- Queue transactional plan-ready emails atomically with completed generation jobs.
-- No backfill: historical plans must not unexpectedly email existing athletes.
create table public.plan_ready_email_deliveries (
  job_id uuid primary key references public.generation_jobs(id) on delete cascade,
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  plan_id uuid not null references public.plans(id) on delete cascade,
  status text not null default 'pending' check (status in ('pending', 'sending', 'sent', 'failed')),
  attempts integer not null default 0 check (attempts between 0 and 6),
  next_attempt_at timestamptz not null default now(),
  claim_token uuid,
  claimed_at timestamptz,
  sent_at timestamptz,
  last_error_code text
);
create index plan_ready_email_due_idx on public.plan_ready_email_deliveries (next_attempt_at)
where status = 'pending';
alter table public.plan_ready_email_deliveries enable row level security;
revoke all on public.plan_ready_email_deliveries from public, anon, authenticated;
grant select, insert, update on public.plan_ready_email_deliveries to service_role;

create function public.enqueue_plan_ready_email()
returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
begin
  if new.status = 'completed' and old.status is distinct from new.status and new.plan_id is not null then
    insert into public.plan_ready_email_deliveries (job_id, athlete_id, plan_id)
    select new.id, new.athlete_id, new.plan_id
    from public.plans p
    where p.id = new.plan_id and p.athlete_id = new.athlete_id
      and p.status in ('ready', 'publishable_with_flags')
    on conflict (job_id) do nothing;
  end if;
  return new;
end $$;

create trigger enqueue_plan_ready_email_on_complete
after update of status on public.generation_jobs
for each row execute function public.enqueue_plan_ready_email();
revoke all on function public.enqueue_plan_ready_email() from public, anon, authenticated;

-- Atomic claims; expired leases are reclaimable after a worker interruption.
create function public.claim_plan_ready_email_deliveries(p_limit integer default 5)
returns table (job_id uuid, athlete_id uuid, plan_id uuid, claim_token uuid, attempts integer)
language plpgsql security definer set search_path = public, pg_temp as $$
begin
  update public.plan_ready_email_deliveries d set status = 'failed', last_error_code = 'retry_exhausted'
  where d.status = 'sending' and d.attempts >= 6 and d.claimed_at < now() - interval '5 minutes';

  return query
  with due as (
    select d.job_id from public.plan_ready_email_deliveries d
    where d.attempts < 6
      and ((d.status = 'pending' and d.next_attempt_at <= now())
        or (d.status = 'sending' and d.claimed_at < now() - interval '5 minutes'))
    order by d.next_attempt_at
    limit least(greatest(p_limit, 1), 10)
    for update skip locked
  )
  update public.plan_ready_email_deliveries d
  set status = 'sending', attempts = d.attempts + 1, claim_token = gen_random_uuid(), claimed_at = now()
  from due where d.job_id = due.job_id
  returning d.job_id, d.athlete_id, d.plan_id, d.claim_token, d.attempts;
end $$;
revoke all on function public.claim_plan_ready_email_deliveries(integer) from public, anon, authenticated;
grant execute on function public.claim_plan_ready_email_deliveries(integer) to service_role;
