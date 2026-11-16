-- Operator-managed invitation rules. These create pending invitations only;
-- they never accept sharing or expose athlete performance to a coach.
create table public.coach_invitation_rules (
  coach_id uuid primary key references public.profiles(id) on delete cascade,
  email_domains text[] not null check (cardinality(email_domains) between 1 and 20),
  enabled boolean not null default true,
  enabled_at timestamptz not null default now(),
  configured_by text not null,
  updated_at timestamptz not null default now()
);
alter table public.coach_invitation_rules enable row level security;
revoke all on public.coach_invitation_rules from public, anon, authenticated;
grant select, insert, update, delete on public.coach_invitation_rules to service_role;

create function coach_private.invite_matching_signup() returns trigger
language plpgsql security definer set search_path='' as $$
declare recipient_email text; signed_up_at timestamptz;
begin
  if new.role <> 'athlete' then return new; end if;
  select lower(email), created_at into recipient_email, signed_up_at
    from auth.users where id=new.id;
  if recipient_email is null then return new; end if;
  insert into public.coach_connections(coach_id,invited_email)
    select rule.coach_id, recipient_email
    from public.coach_invitation_rules rule
    join public.profiles coach on coach.id=rule.coach_id
      and coach.role in ('coach','admin') and coach.access_status='approved'
    where rule.enabled and signed_up_at >= rule.enabled_at
      and split_part(recipient_email,'@',2)=any(rule.email_domains)
      and rule.coach_id <> new.id
    on conflict (coach_id,invited_email) where status in ('pending','accepted') do nothing;
  return new;
end $$;
revoke all on function coach_private.invite_matching_signup() from public,anon,authenticated;
create trigger coach_invite_new_signup after insert on public.profiles
  for each row execute function coach_private.invite_matching_signup();
