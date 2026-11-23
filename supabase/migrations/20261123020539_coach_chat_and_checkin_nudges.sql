-- Coach chat and coach-set check-in nudges.
--
-- Speed: every screen is one indexed read. The chat row carries the inbox
-- preview and both unread counts, written in the send transaction, so the
-- inbox never counts or scans messages. Each message carries the pair's ids,
-- so its RLS check (which Realtime also runs for every change it delivers) is
-- a column comparison with no join.
--
-- Security: clients get SELECT only, on rows they are a participant in. Every
-- write is a security-definer function in coach_private that rechecks the
-- approved roles and the accepted connection under a row lock, the same
-- boundary as coach feedback. Message text never leaves through a push.

-- 1. Conversation pushes get their own class with its own daily allowance, so
-- a busy chat can never use up the event allowance that plan, feedback and
-- contact-concern alerts share.
alter table public.notification_deliveries
  drop constraint if exists notification_deliveries_class_check;
alter table public.notification_deliveries
  add constraint notification_deliveries_class_check
  check (notification_class in ('routine', 'safety', 'event', 'conversation'));

-- 2. One chat per coach-fighter pair. When the fighter disconnects, the coach
-- loses access on the next request (as with every other coach read) while the
-- fighter keeps the history read-only, as with feedback. Either account's
-- deletion removes it. Reconnecting resumes the same chat.
create table public.coach_chats (
  id uuid primary key default gen_random_uuid(),
  coach_id uuid not null references public.profiles(id) on delete cascade,
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  last_message_at timestamptz,
  last_preview text not null default '' check (char_length(last_preview) <= 120),
  last_sender_id uuid,
  coach_unread integer not null default 0 check (coach_unread between 0 and 999),
  athlete_unread integer not null default 0 check (athlete_unread between 0 and 999),
  coach_read_at timestamptz,
  athlete_read_at timestamptz,
  check (coach_id <> athlete_id),
  unique (coach_id, athlete_id)
);
create index coach_chats_athlete on public.coach_chats (athlete_id);
alter table public.coach_chats enable row level security;
revoke all on public.coach_chats from public, anon, authenticated;
grant select on public.coach_chats to authenticated;
grant all on public.coach_chats to service_role;
-- The coach side also needs the live connection: one probe of the accepted-pair index.
create policy coach_chats_participants on public.coach_chats for select to authenticated
  using (athlete_id = (select auth.uid()) or (coach_id = (select auth.uid()) and exists (
    select 1 from public.coach_connections c where c.coach_id = coach_chats.coach_id
      and c.athlete_id = coach_chats.athlete_id and c.status = 'accepted')));

create table public.coach_messages (
  id bigint generated always as identity primary key,
  chat_id uuid not null references public.coach_chats(id) on delete cascade,
  coach_id uuid not null references public.profiles(id) on delete cascade,
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  sender_id uuid not null references public.profiles(id) on delete cascade,
  -- Chosen by the sender's device: a retried send is the same message.
  client_id uuid not null,
  kind text not null check (kind in ('text', 'photo', 'voice')),
  body text not null default '' check (char_length(body) <= 2000),
  asset_id uuid references public.attachment_assets(id) on delete set null,
  -- A snapshot built by the database from the quoted record, never client text.
  quote jsonb,
  reply_to_id bigint references public.coach_messages(id) on delete set null,
  -- {profile id: emoji}; at most one reaction per participant.
  reactions jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (chat_id, client_id),
  check (sender_id = coach_id or sender_id = athlete_id)
);
-- Opening a chat and paging back are one index range scan.
create index coach_messages_chat_recent on public.coach_messages (chat_id, id desc);
create index coach_messages_asset on public.coach_messages (asset_id) where asset_id is not null;
alter table public.coach_messages enable row level security;
revoke all on public.coach_messages from public, anon, authenticated;
grant select on public.coach_messages to authenticated;
grant all on public.coach_messages to service_role;
create policy coach_messages_participants on public.coach_messages for select to authenticated
  using (athlete_id = (select auth.uid()) or (coach_id = (select auth.uid()) and exists (
    select 1 from public.coach_connections c where c.coach_id = coach_messages.coach_id
      and c.athlete_id = coach_messages.athlete_id and c.status = 'accepted')));

-- Reports are read by operators only (service role).
create table public.coach_message_reports (
  id uuid primary key default gen_random_uuid(),
  message_id bigint not null references public.coach_messages(id) on delete cascade,
  reporter_id uuid not null references public.profiles(id) on delete cascade,
  reason text not null check (char_length(trim(reason)) between 1 and 500),
  created_at timestamptz not null default now(),
  unique (message_id, reporter_id)
);
alter table public.coach_message_reports enable row level security;
revoke all on public.coach_message_reports from public, anon, authenticated;
grant all on public.coach_message_reports to service_role;

-- 3. A training card for a quoted record, built here so the stored snapshot
-- can never carry more than the coach is allowed to see. Injury and rehab
-- work reads as "Programme session" without injury sharing.
create function coach_private.chat_card(p_athlete_id uuid, p_injuries boolean, p_source text, p_target_id uuid)
returns jsonb language plpgsql stable security definer set search_path = '' as $$
declare s public.session_completions; e public.external_training_sessions; l public.sparring_logs; title text;
begin
  if p_source = 'unlxck' then
    select * into s from public.session_completions where id = p_target_id and athlete_id = p_athlete_id
      and status in ('done', 'modified', 'skipped') and completion_origin is distinct from 'day_credit';
    if s.id is null then raise exception 'Record unavailable' using errcode = '42501'; end if;
    title := nullif(trim(s.prescription_snapshot #>> '{session,title}'), '');
    if not p_injuries and (s.prescription_snapshot is null or coach_private.health_session(
        s.prescription_snapshot #>> '{session,session_type}', title, s.prescription_snapshot #> '{session,blocks}')) then
      title := null;
    end if;
    return jsonb_build_object('source', 'unlxck', 'target_id', s.id, 'day', s.training_day,
      'title', left(coalesce(title, 'Programme session'), 80),
      'meta', concat_ws(' · ', initcap(s.status), case when s.session_rpe is not null then 'RPE ' || s.session_rpe end));
  elsif p_source = 'external' then
    select * into e from public.external_training_sessions where id = p_target_id and athlete_id = p_athlete_id
      and deleted_at is null;
    if e.id is null then raise exception 'Record unavailable' using errcode = '42501'; end if;
    return jsonb_build_object('source', 'external', 'target_id', e.id, 'day', e.training_day,
      'title', case when e.activity = 'other' then 'Training session' else initcap(e.activity) end,
      'meta', concat_ws(' · ', e.duration_minutes || ' min', initcap(e.effort)));
  elsif p_source = 'sparring' then
    select * into l from public.sparring_logs where id = p_target_id and athlete_id = p_athlete_id
      and (external_session_id is null or exists (select 1 from public.external_training_sessions x
        where x.id = external_session_id and x.athlete_id = p_athlete_id and x.deleted_at is null));
    if l.id is null then raise exception 'Report unavailable' using errcode = '42501'; end if;
    return jsonb_build_object('source', 'sparring', 'target_id', l.id, 'day', l.training_day, 'title', 'Sparring',
      'meta', concat_ws(' · ', l.rounds_completed || ' rounds', initcap(l.intensity)));
  end if;
  raise exception 'Record unavailable' using errcode = '42501';
end $$;
revoke all on function coach_private.chat_card(uuid, boolean, text, uuid) from public, anon, authenticated;

-- 4. Send. One round trip: resolve the pair, lock the chat, write the message
-- and the inbox row together. A retry with the same client_id returns the
-- stored message and changes nothing.
create function coach_private.chat_send(p_other_id uuid, p_client_id uuid, p_kind text, p_body text,
  p_asset_id uuid, p_quote_source text, p_quote_target uuid, p_reply_to bigint)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare actor public.profiles; other public.profiles; athlete public.profiles; c public.coach_connections;
  chat public.coach_chats; msg public.coach_messages; card jsonb; asset_ok boolean;
  body text := trim(coalesce(p_body, '')); preview text;
begin
  select * into actor from public.profiles where id = auth.uid() and access_status = 'approved';
  select * into other from public.profiles where id = p_other_id and access_status = 'approved';
  if actor.id is null or other.id is null or p_client_id is null then
    raise exception 'Chat unavailable' using errcode = '42501'; end if;
  -- The caller is whichever side of an accepted connection they are on.
  select * into c from public.coach_connections where status = 'accepted' and (
      (coach_id = actor.id and athlete_id = other.id and actor.role in ('coach', 'admin') and other.role = 'athlete')
      or (athlete_id = actor.id and coach_id = other.id and actor.role = 'athlete' and other.role in ('coach', 'admin')))
    for share;
  if c.id is null then raise exception 'Chat unavailable' using errcode = '42501'; end if;
  if c.athlete_id = actor.id then athlete := actor; else athlete := other; end if;

  insert into public.coach_chats (coach_id, athlete_id) values (c.coach_id, c.athlete_id)
    on conflict (coach_id, athlete_id) do nothing;
  select * into chat from public.coach_chats where coach_id = c.coach_id and athlete_id = c.athlete_id for update;

  select * into msg from public.coach_messages where chat_id = chat.id and client_id = p_client_id;
  if msg.id is not null then
    if msg.sender_id <> actor.id then raise exception 'Chat unavailable' using errcode = '42501'; end if;
    return to_jsonb(msg) || jsonb_build_object('created', false);
  end if;

  if p_kind is null or p_kind not in ('text', 'photo', 'voice') or char_length(body) > 2000 then
    raise exception 'Messages can be up to 2000 characters' using errcode = '22023'; end if;
  if p_kind = 'text' and body = '' and p_quote_source is null then
    raise exception 'Write a message' using errcode = '22023'; end if;
  if p_kind = 'text' and p_asset_id is not null then
    raise exception 'File unavailable' using errcode = '22023'; end if;
  if p_kind in ('photo', 'voice') then
    -- Only the sender's own clean, rebuilt file, used once.
    select true into asset_ok from public.attachment_assets a where a.id = p_asset_id and a.owner_id = actor.id
      and a.status = 'ready' and a.kind = p_kind
      and not exists (select 1 from public.coach_messages m where m.asset_id = a.id)
      and not exists (select 1 from public.training_attachments t where t.asset_id = a.id);
    if asset_ok is not true then raise exception 'File unavailable' using errcode = '42501'; end if;
  end if;
  if p_quote_source is not null then
    -- Training records are health data: quoting one needs the fighter's consent.
    if athlete.health_data_consent is not true then
      raise exception 'Training cards need the fighter''s health-data consent' using errcode = '42501'; end if;
    card := coach_private.chat_card(athlete.id, c.share_injuries and athlete.health_data_consent,
      p_quote_source, p_quote_target);
  end if;
  if p_reply_to is not null and not exists (select 1 from public.coach_messages
      where id = p_reply_to and chat_id = chat.id) then
    raise exception 'Message unavailable' using errcode = '22023'; end if;
  -- Flood guard: 30 messages a minute from one sender in one chat.
  if (select count(*) from (select 1 from public.coach_messages m where m.chat_id = chat.id
      and m.sender_id = actor.id and m.created_at > now() - interval '1 minute' order by m.id desc limit 30) recent) >= 30 then
    raise exception 'Too many messages. Wait a moment.' using errcode = 'PT429'; end if;

  insert into public.coach_messages (chat_id, coach_id, athlete_id, sender_id, client_id, kind, body, asset_id,
    quote, reply_to_id)
  values (chat.id, c.coach_id, c.athlete_id, actor.id, p_client_id, p_kind, body,
    case when p_kind = 'text' then null else p_asset_id end, card, p_reply_to)
  returning * into msg;

  preview := case when body <> '' then left(regexp_replace(body, '\s+', ' ', 'g'), 120)
    when p_kind = 'photo' then 'Photo' when p_kind = 'voice' then 'Voice note'
    else left(coalesce(card ->> 'title', 'Training'), 120) end;
  update public.coach_chats set last_message_at = msg.created_at, last_preview = preview, last_sender_id = actor.id,
    coach_unread = case when actor.id = c.coach_id then 0 else least(coach_unread + 1, 999) end,
    athlete_unread = case when actor.id = c.athlete_id then 0 else least(athlete_unread + 1, 999) end,
    coach_read_at = case when actor.id = c.coach_id then msg.created_at else coach_read_at end,
    athlete_read_at = case when actor.id = c.athlete_id then msg.created_at else athlete_read_at end
  where id = chat.id;
  return to_jsonb(msg) || jsonb_build_object('created', true);
end $$;
revoke all on function coach_private.chat_send(uuid, uuid, text, text, uuid, text, uuid, bigint) from public, anon;
grant execute on function coach_private.chat_send(uuid, uuid, text, text, uuid, text, uuid, bigint) to authenticated;
create function public.coach_chat_send(p_other_id uuid, p_client_id uuid, p_kind text, p_body text default '',
  p_asset_id uuid default null, p_quote_source text default null, p_quote_target uuid default null,
  p_reply_to bigint default null)
returns jsonb language sql security invoker set search_path = '' as $$
  select coach_private.chat_send(p_other_id, p_client_id, p_kind, p_body, p_asset_id, p_quote_source, p_quote_target, p_reply_to);
$$;
revoke all on function public.coach_chat_send(uuid, uuid, text, text, uuid, text, uuid, bigint) from public, anon;
grant execute on function public.coach_chat_send(uuid, uuid, text, text, uuid, text, uuid, bigint) to authenticated;

-- 5. Mark read. Writes only when something is unread, so reopening a chat
-- costs no write and sends no Realtime event. History stays readable after
-- a connection ends, so this needs participation only.
create function coach_private.chat_read(p_chat_id uuid) returns void
language sql security definer set search_path = '' as $$
  update public.coach_chats set
    coach_unread = case when coach_id = auth.uid() then 0 else coach_unread end,
    coach_read_at = case when coach_id = auth.uid() then now() else coach_read_at end,
    athlete_unread = case when athlete_id = auth.uid() then 0 else athlete_unread end,
    athlete_read_at = case when athlete_id = auth.uid() then now() else athlete_read_at end
  where id = p_chat_id and last_message_at is not null and (
    (coach_id = auth.uid() and (coach_unread > 0 or coach_read_at is null or coach_read_at < last_message_at))
    or (athlete_id = auth.uid() and (athlete_unread > 0 or athlete_read_at is null or athlete_read_at < last_message_at)));
$$;
revoke all on function coach_private.chat_read(uuid) from public, anon;
grant execute on function coach_private.chat_read(uuid) to authenticated;
create function public.coach_chat_read(p_chat_id uuid) returns void
language sql security invoker set search_path = '' as $$ select coach_private.chat_read(p_chat_id); $$;
revoke all on function public.coach_chat_read(uuid) from public, anon;
grant execute on function public.coach_chat_read(uuid) to authenticated;

-- 6. React (null clears). Only while the pair is still connected.
create function coach_private.chat_react(p_message_id bigint, p_emoji text) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare result jsonb;
begin
  if p_emoji is not null and p_emoji not in ('👍', '🔥', '💪', '👊', '😂', '❤️') then
    raise exception 'Reaction unavailable' using errcode = '22023'; end if;
  update public.coach_messages m set reactions = case when p_emoji is null then m.reactions - auth.uid()::text
      else m.reactions || jsonb_build_object(auth.uid()::text, p_emoji) end
    where m.id = p_message_id and (m.coach_id = auth.uid() or m.athlete_id = auth.uid())
      and exists (select 1 from public.coach_connections c where c.coach_id = m.coach_id
        and c.athlete_id = m.athlete_id and c.status = 'accepted')
    returning m.reactions into result;
  if result is null then raise exception 'Message unavailable' using errcode = '42501'; end if;
  return result;
end $$;
revoke all on function coach_private.chat_react(bigint, text) from public, anon;
grant execute on function coach_private.chat_react(bigint, text) to authenticated;
create function public.coach_chat_react(p_message_id bigint, p_emoji text default null) returns jsonb
language sql security invoker set search_path = '' as $$ select coach_private.chat_react(p_message_id, p_emoji); $$;
revoke all on function public.coach_chat_react(bigint, text) from public, anon;
grant execute on function public.coach_chat_react(bigint, text) to authenticated;

-- 7. Report a message from the other participant. Works after a disconnect too.
create function coach_private.chat_report(p_message_id bigint, p_reason text) returns void
language plpgsql security definer set search_path = '' as $$
begin
  if p_reason is null or char_length(trim(p_reason)) not between 1 and 500 then
    raise exception 'Say what is wrong in up to 500 characters' using errcode = '22023'; end if;
  if not exists (select 1 from public.coach_messages m where m.id = p_message_id
      and (m.coach_id = auth.uid() or m.athlete_id = auth.uid()) and m.sender_id <> auth.uid()) then
    raise exception 'Message unavailable' using errcode = '42501'; end if;
  insert into public.coach_message_reports (message_id, reporter_id, reason)
    values (p_message_id, auth.uid(), trim(p_reason)) on conflict (message_id, reporter_id) do nothing;
end $$;
revoke all on function coach_private.chat_report(bigint, text) from public, anon;
grant execute on function coach_private.chat_report(bigint, text) to authenticated;
create function public.coach_chat_report(p_message_id bigint, p_reason text) returns void
language sql security invoker set search_path = '' as $$ select coach_private.chat_report(p_message_id, p_reason); $$;
revoke all on function public.coach_chat_report(bigint, text) from public, anon;
grant execute on function public.coach_chat_report(bigint, text) to authenticated;

-- 8. Check-in nudges. A coach picks weekdays and a local time; the fighter is
-- nudged only if they have not checked in that day. Never for under-18s or
-- without health-data consent: the children's policy forbids nudging a child
-- to disclose health data, and a check-in is health data.
create table public.coach_checkin_nudges (
  id uuid primary key default gen_random_uuid(),
  coach_id uuid not null references public.profiles(id) on delete cascade,
  athlete_id uuid not null references public.profiles(id) on delete cascade,
  -- Bit 0 is Monday ... bit 6 is Sunday.
  days smallint not null check (days between 1 and 127),
  local_time time not null check (local_time between time '05:00' and time '21:00'),
  enabled boolean not null default true,
  last_sent_day date,
  last_sent_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (coach_id <> athlete_id),
  unique (coach_id, athlete_id)
);
create index coach_checkin_nudges_athlete on public.coach_checkin_nudges (athlete_id);
create index coach_checkin_nudges_enabled on public.coach_checkin_nudges (local_time) where enabled;
alter table public.coach_checkin_nudges enable row level security;
revoke all on public.coach_checkin_nudges from public, anon, authenticated;
grant select on public.coach_checkin_nudges to authenticated;
grant all on public.coach_checkin_nudges to service_role;
-- The fighter sees who asks them to check in, and when.
create policy coach_checkin_nudges_participants on public.coach_checkin_nudges for select to authenticated
  using (coach_id = (select auth.uid()) or athlete_id = (select auth.uid()));

create function coach_private.set_checkin_nudge(p_athlete_id uuid, p_days smallint, p_time time, p_enabled boolean)
returns public.coach_checkin_nudges language plpgsql security definer set search_path = '' as $$
declare actor public.profiles; athlete public.profiles; c public.coach_connections; result public.coach_checkin_nudges;
begin
  select * into actor from public.profiles where id = auth.uid() and access_status = 'approved' and role in ('coach', 'admin');
  select * into athlete from public.profiles where id = p_athlete_id and access_status = 'approved' and role = 'athlete';
  select * into c from public.coach_connections where coach_id = actor.id and athlete_id = athlete.id
    and status = 'accepted' for share;
  if actor.id is null or athlete.id is null or c.id is null then
    raise exception 'Fighter not found' using errcode = '42501'; end if;
  if p_days is null or p_days not between 1 and 127 or p_time is null or p_time not between time '05:00' and time '21:00' then
    raise exception 'Pick at least one day and a time between 05:00 and 21:00' using errcode = '22023'; end if;
  if coalesce(p_enabled, true) then
    if athlete.date_of_birth is null or athlete.date_of_birth > current_date - interval '18 years' then
      raise exception 'Check-in nudges are for fighters aged 18 and over' using errcode = '42501'; end if;
    if athlete.health_data_consent is not true then
      raise exception 'This fighter has not allowed health-data use' using errcode = '42501'; end if;
  end if;
  insert into public.coach_checkin_nudges (coach_id, athlete_id, days, local_time, enabled)
    values (actor.id, athlete.id, p_days,
      make_time(extract(hour from p_time)::integer, extract(minute from p_time)::integer, 0), coalesce(p_enabled, true))
  on conflict (coach_id, athlete_id) do update set days = excluded.days, local_time = excluded.local_time,
    enabled = excluded.enabled, updated_at = now()
  returning * into result;
  return result;
end $$;
revoke all on function coach_private.set_checkin_nudge(uuid, smallint, time, boolean) from public, anon;
grant execute on function coach_private.set_checkin_nudge(uuid, smallint, time, boolean) to authenticated;
create function public.coach_set_checkin_nudge(p_athlete_id uuid, p_days smallint, p_time time, p_enabled boolean default true)
returns public.coach_checkin_nudges language sql security invoker set search_path = '' as $$
  select coach_private.set_checkin_nudge(p_athlete_id, p_days, p_time, p_enabled);
$$;
revoke all on function public.coach_set_checkin_nudge(uuid, smallint, time, boolean) from public, anon;
grant execute on function public.coach_set_checkin_nudge(uuid, smallint, time, boolean) to authenticated;

-- The worker's claim: every nudge due now, in one statement. The time zone
-- list is scanned once, not per fighter. Claiming stamps the day, so a second
-- worker or a retried sweep can never nudge the same fighter twice in a day.
create function public.claim_coach_checkin_nudges(p_limit integer default 200)
returns table (nudge_id uuid, coach_id uuid, athlete_id uuid, coach_name text, timezone text, training_day date)
language sql volatile security definer set search_path = '' as $$
  with zones as materialized (select name from pg_catalog.pg_timezone_names),
  candidates as (
    select n.id, n.coach_id, n.athlete_id, n.days, n.local_time, n.last_sent_day, coach.full_name as coach_name,
      coalesce(z.name, 'UTC') as zone,
      (now() at time zone coalesce(z.name, 'UTC')) as local_now
    from public.coach_checkin_nudges n
    join public.coach_connections c on c.coach_id = n.coach_id and c.athlete_id = n.athlete_id and c.status = 'accepted'
    join public.profiles a on a.id = n.athlete_id and a.role = 'athlete' and a.access_status = 'approved'
      and a.health_data_consent is true and a.date_of_birth is not null
      and a.date_of_birth <= current_date - interval '18 years'
    join public.profiles coach on coach.id = n.coach_id and coach.role in ('coach', 'admin') and coach.access_status = 'approved'
    left join zones z on z.name = a.athlete_timezone
    where n.enabled
  ),
  due as (
    select cd.id, cd.coach_name, cd.zone, cd.local_now::date as day
    from candidates cd
    where (cd.days & (1 << (extract(isodow from cd.local_now)::integer - 1))) <> 0
      and cd.local_now::time >= cd.local_time
      and cd.local_now::time < cd.local_time + interval '2 hours'
      and cd.last_sent_day is distinct from cd.local_now::date
      and not exists (select 1 from public.today_checkins t
        where t.athlete_id = cd.athlete_id and t.training_day = cd.local_now::date)
    order by cd.local_time
    limit greatest(1, least(coalesce(p_limit, 200), 500))
  )
  update public.coach_checkin_nudges n set last_sent_day = d.day, last_sent_at = now()
  from due d
  where n.id = d.id and n.last_sent_day is distinct from d.day
  returning n.id, n.coach_id, n.athlete_id, d.coach_name, d.zone, d.day;
$$;
revoke all on function public.claim_coach_checkin_nudges(integer) from public, anon, authenticated;
grant execute on function public.claim_coach_checkin_nudges(integer) to service_role;

-- 9. The inbox: every chat plus every connected pair without one yet, in one
-- call. Ended connections keep their chat as read-only history.
create function coach_private.chat_inbox() returns jsonb
language sql stable security definer set search_path = '' as $$
  with me as (
    select p.id from public.profiles p where p.id = auth.uid() and p.access_status = 'approved'
  ),
  live as (
    select c.coach_id, c.athlete_id, c.share_injuries from public.coach_connections c
    where c.status = 'accepted' and (c.coach_id = (select id from me) or c.athlete_id = (select id from me))
  ),
  chats as (
    select ch.* from public.coach_chats ch
    where ch.coach_id = (select id from me) or ch.athlete_id = (select id from me)
  ),
  pairs as (
    select coalesce(ch.coach_id, l.coach_id) as coach_id, coalesce(ch.athlete_id, l.athlete_id) as athlete_id,
      ch.id as chat_id, ch.last_message_at, ch.last_preview, ch.last_sender_id, ch.coach_unread, ch.athlete_unread,
      ch.coach_read_at, ch.athlete_read_at, l.coach_id is not null as active, coalesce(l.share_injuries, false) as share_injuries
    from chats ch full join live l on l.coach_id = ch.coach_id and l.athlete_id = ch.athlete_id
  )
  select coalesce(jsonb_agg(rows.row order by rows.sort_at desc nulls last, rows.row ->> 'name'), '[]'::jsonb)
  from (
    select x.last_message_at as sort_at, jsonb_build_object(
      'chat_id', x.chat_id,
      'other_id', o.id,
      'name', coalesce(nullif(trim(o.full_name), ''), case when me_coach then 'Fighter' else 'Coach' end),
      'avatar_url', o.avatar_url,
      'role', case when me_coach then 'coach' else 'athlete' end,
      'active', x.active,
      'last_message_at', x.last_message_at,
      'last_preview', coalesce(x.last_preview, ''),
      'last_from_me', x.last_sender_id = (select id from me),
      'unread', coalesce(case when me_coach then x.coach_unread else x.athlete_unread end, 0),
      'other_read_at', case when me_coach then x.athlete_read_at else x.coach_read_at end,
      'fight_date', case when me_coach then plan.fight_date end,
      -- The same consent that shows these signals on the coach overview.
      'attention', me_coach and x.active and x.share_injuries and o.health_data_consent is true and (
        exists (select 1 from public.injury_flags i where i.athlete_id = o.id and i.status in ('open', 'monitoring'))
        or exists (select 1 from public.sparring_logs s where s.athlete_id = o.id
          and s.training_day >= current_date - 7 and (s.rocked is true or s.head_contact = 'heavy'))),
      'missed_checkin', me_coach and x.active and exists (select 1 from public.coach_checkin_nudges n
        where n.coach_id = x.coach_id and n.athlete_id = x.athlete_id and n.enabled
          and n.last_sent_day is not null and n.last_sent_at > now() - interval '20 hours'
          and not exists (select 1 from public.today_checkins t where t.athlete_id = n.athlete_id
            and t.training_day = n.last_sent_day))
    ) as row
    from pairs x
    cross join lateral (select x.coach_id = (select id from me) as me_coach) side
    join public.profiles o on o.id = case when side.me_coach then x.athlete_id else x.coach_id end
    left join public.plans plan on side.me_coach and plan.id = o.active_plan_id and plan.athlete_id = o.id
    -- A coach no longer connected no longer sees the chat; the fighter keeps it.
    where (select id from me) is not null and (x.active or not side.me_coach)
  ) rows;
$$;
revoke all on function coach_private.chat_inbox() from public, anon;
grant execute on function coach_private.chat_inbox() to authenticated;
create function public.coach_chat_inbox() returns jsonb
language sql stable security invoker set search_path = '' as $$ select coach_private.chat_inbox(); $$;
revoke all on function public.coach_chat_inbox() from public, anon;
grant execute on function public.coach_chat_inbox() to authenticated;

-- 10. Files sent in a chat are attached files: cleanup must keep them.
create or replace function public.attachment_cleanup_candidates() returns setof public.attachment_assets
language plpgsql security invoker set search_path = '' as $$
begin
  -- Fencing happens before removal; failed removals remain eligible on every run.
  update public.attachment_assets a set status='rejected'
    where a.updated_at < now()-interval '24 hours' and
      (a.status in ('reserved','processing') or
       (a.status='ready' and not exists(select 1 from public.training_attachments t where t.asset_id=a.id)
        and not exists(select 1 from public.coach_messages m where m.asset_id=a.id)));
  return query select a.* from public.attachment_assets a
    where a.status='rejected' or (a.status='ready' and not a.intake_removed)
    order by a.updated_at limit 50;
end $$;

-- 11. Live delivery. New messages, reactions and inbox rows stream through
-- Realtime, which applies the SELECT policies above to every change. Typing
-- goes over a private broadcast channel per chat and never touches a table.
create function coach_private.in_chat_topic(p_topic text) returns boolean
language sql stable security definer set search_path = '' as $$
  select p_topic ~ '^coach-chat:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    and exists (select 1 from public.coach_chats ch
      join public.coach_connections c on c.coach_id = ch.coach_id and c.athlete_id = ch.athlete_id and c.status = 'accepted'
      where ch.id = substr(p_topic, 12)::uuid and (ch.coach_id = auth.uid() or ch.athlete_id = auth.uid()));
$$;
revoke all on function coach_private.in_chat_topic(text) from public, anon;
grant execute on function coach_private.in_chat_topic(text) to authenticated;

do $$
begin
  if exists (select 1 from pg_catalog.pg_publication where pubname = 'supabase_realtime' and not puballtables) then
    alter publication supabase_realtime add table public.coach_messages, public.coach_chats;
  end if;
  if to_regclass('realtime.messages') is not null and to_regprocedure('realtime.topic()') is not null then
    execute $policy$create policy coach_chat_typing_receive on realtime.messages for select to authenticated
      using (realtime.messages.extension = 'broadcast' and coach_private.in_chat_topic((select realtime.topic())))$policy$;
    execute $policy$create policy coach_chat_typing_send on realtime.messages for insert to authenticated
      with check (realtime.messages.extension = 'broadcast' and coach_private.in_chat_topic((select realtime.topic())))$policy$;
  end if;
end $$;
