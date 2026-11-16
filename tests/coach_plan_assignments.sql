reset role;
insert into auth.users(id,email,email_confirmed_at) values
 ('00000000-0000-4000-8000-000000000071','plans-coach@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000072','plans-fighter@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000073','plans-other@test.invalid',now());
insert into public.profiles(id,email,role,full_name,health_data_consent)
 select id,email,case when email like 'plans-coach%' then 'coach'::public.app_role else 'athlete'::public.app_role end,'Plan author',true
 from auth.users where email like 'plans-%';
insert into public.coach_connections(coach_id,athlete_id,invited_email,status,accepted_at)
 values('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','plans-fighter@test.invalid','accepted',now());
set local role service_role;
select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000071','create','{"name":"Coach week","sessions":[],"incomplete":false}');
select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000071','assign');
select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000071','assign');
select pg_temp.assert_ok((select count(*) from public.custom_training_plans where fighter_id='00000000-0000-4000-8000-000000000072')=1,'assignment retry preserves one draft');
select pg_temp.assert_ok((select active_plan_id is null from public.profiles where id='00000000-0000-4000-8000-000000000072'),'assignment does not activate a plan');
select pg_temp.assert_denied($$select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000071','update','{"name":"Edited","sessions":[],"incomplete":false}')$$);
select pg_temp.assert_denied($$select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000073','10000000-0000-4000-8000-000000000071','assign')$$);
insert into public.plans(id,athlete_id,full_name,technical_style,status,planning_brief)
 values('20000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','Fighter','{}','review_required','{"coach_assignment_id":"10000000-0000-4000-8000-000000000071"}');
select pg_temp.assert_ok(not public.respond_coach_plan('00000000-0000-4000-8000-000000000073','10000000-0000-4000-8000-000000000071','activate','20000000-0000-4000-8000-000000000071'),'wrong fighter cannot accept');
select pg_temp.assert_ok(not public.respond_coach_plan('00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000071','activate','20000000-0000-4000-8000-000000000071','20000000-0000-4000-8000-000000000071'),'stale expected pointer cannot activate');
select pg_temp.assert_ok(public.respond_coach_plan('00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000071','activate','20000000-0000-4000-8000-000000000071'),'fighter accepts atomically');
select pg_temp.assert_ok((select status='ready' from public.plans where id='20000000-0000-4000-8000-000000000071'),'accepted plan becomes executable');
select pg_temp.assert_ok((select active_plan_id='20000000-0000-4000-8000-000000000071' from public.profiles where id='00000000-0000-4000-8000-000000000072'),'accepted plan is active');
select pg_temp.assert_ok(not public.respond_coach_plan('00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000071','decline'),'accepted plan cannot be declined');
select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000072','create','{"name":"Another week","sessions":[],"incomplete":false}');
select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000072','assign');
select pg_temp.assert_ok(public.respond_coach_plan('00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000072','decline'),'fighter declines');
select pg_temp.assert_ok(public.respond_coach_plan('00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000072','decline'),'decline retry succeeds');
select pg_temp.assert_ok(not public.respond_coach_plan('00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000072','activate','20000000-0000-4000-8000-000000000071'),'decline cannot race into acceptance');
select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000073','create','{"name":"Dated programme","sessions":[],"incomplete":true,"schedule_mode":"dated","start_date":"2026-11-16","end_date":"2026-12-13"}');
select pg_temp.assert_ok((select schedule_mode='dated' and start_date='2026-11-16'::date and end_date='2026-12-13'::date from custom_training_plans where id='10000000-0000-4000-8000-000000000073'),'coach writes preserve authored date range');
-- Retract: only an unanswered plan, back to an editable draft, inert copy removed.
select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000074','create','{"name":"Retract me","sessions":[],"incomplete":false}');
select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000074','assign');
insert into public.plans(id,athlete_id,full_name,technical_style,status,planning_brief)
 values('20000000-0000-4000-8000-000000000074','00000000-0000-4000-8000-000000000072','Fighter','{}','review_required','{"coach_assignment_id":"10000000-0000-4000-8000-000000000074"}');
select pg_temp.assert_denied($$select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000074','remove')$$);
select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000074','retract');
select pg_temp.assert_ok((select assigned_at is null and coach_name is null from public.custom_training_plans where id='10000000-0000-4000-8000-000000000074'),'retracted plan is a draft again');
select pg_temp.assert_ok(not exists(select 1 from public.plans where id='20000000-0000-4000-8000-000000000074'),'retract removes the inert compiled copy');
select pg_temp.assert_ok(not public.respond_coach_plan('00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000074','activate','20000000-0000-4000-8000-000000000074','20000000-0000-4000-8000-000000000071'),'retracted plan cannot be accepted');
select pg_temp.assert_ok(not public.respond_coach_plan('00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000074','decline'),'retracted plan cannot be declined');
select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000074','retract');
select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000074','update','{"name":"Edited after retract","sessions":[],"incomplete":false}');
-- Answered plans cannot be retracted; the active one cannot be removed either.
select pg_temp.assert_denied($$select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000071','retract')$$);
select pg_temp.assert_denied($$select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000072','retract')$$);
select pg_temp.assert_denied($$select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000071','remove')$$);
select pg_temp.assert_ok((select active_plan_id='20000000-0000-4000-8000-000000000071' from public.profiles where id='00000000-0000-4000-8000-000000000072'),'a refused removal leaves the active plan alone');
-- A declined plan leaves the coach's list only; its consent record stays.
select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000072','remove');
select pg_temp.assert_ok((select coach_removed_at is not null and declined_at is not null and assigned_at is not null from public.custom_training_plans where id='10000000-0000-4000-8000-000000000072'),'removal hides a declined plan and keeps its record');
update public.coach_connections set status='revoked' where coach_id='00000000-0000-4000-8000-000000000071';
select pg_temp.assert_denied($$select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000072','assign')$$);
select pg_temp.assert_ok((select active_plan_id='20000000-0000-4000-8000-000000000071' from public.profiles where id='00000000-0000-4000-8000-000000000072'),'revocation preserves accepted plan');
-- Athlete/admin deletion retains consent history without a restrictive FK.
delete from public.plans where id='20000000-0000-4000-8000-000000000071';
select pg_temp.assert_ok((select accepted_at is not null and published_plan_id is null from public.custom_training_plans where id='10000000-0000-4000-8000-000000000071'),'deleting an accepted plan retains assignment history');
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000073',true);
select pg_temp.assert_ok((select count(*) from public.custom_training_plans where fighter_id='00000000-0000-4000-8000-000000000072')=0,'no raw cross-fighter draft access');
select pg_temp.assert_denied($$select public.respond_coach_plan('00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000071','activate')$$);
select pg_temp.assert_denied($$select public.coach_plan_command('00000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-000000000072','10000000-0000-4000-8000-000000000071','assign')$$);
reset role;
