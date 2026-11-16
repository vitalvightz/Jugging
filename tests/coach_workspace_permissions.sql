-- Synthetic fixtures in an isolated transaction only. See tools/test_coach_workspace.py.
insert into auth.users(id,email,email_confirmed_at) values
 ('00000000-0000-4000-8000-000000000001','coach@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000002','other-coach@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000003','athlete@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000004','imposter@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000005','unverified@test.invalid',null);
insert into public.profiles(id,email,role,full_name,health_data_consent)
 select id,case when email='imposter@test.invalid' then 'spoofed@test.invalid' else email end,
 case when email like '%coach%' then 'coach'::public.app_role else 'athlete'::public.app_role end,'Test fighter',true from auth.users;
-- Profile email spoofing must never allow taking over an invitation.
update public.profiles set email='different-profile@test.invalid' where id='00000000-0000-4000-8000-000000000003';
update public.profiles set email='athlete@test.invalid' where id='00000000-0000-4000-8000-000000000004';
insert into public.plans(id,athlete_id,plan_name,fight_date,structured_plan) values
 ('10000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000003','Test camp',current_date+20,
 '{"plan_metadata":{"sport":"boxing"},"athlete_context":{"injury":"SECRET MEDICAL"},"weeks":[{"days":[{"date":"2026-11-15","sessions":[{"session_id":"s1","session_type":"strength_power","title":"Strength","blocks":[{"notes":"SECRET MEDICAL"}]}]}]}]}');
update public.profiles set active_plan_id='10000000-0000-4000-8000-000000000001' where id='00000000-0000-4000-8000-000000000003';
insert into public.session_completions(athlete_id,plan_id,session_id,training_day,status,completion_origin,notes) values
 ('00000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','s1',current_date,'done','reported','SECRET MEDICAL'),
 ('00000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','s2',current_date,'done','day_credit','');
insert into public.daily_checkins(athlete_id,checkin_date,readiness,fatigue,soreness,sleep_quality,notes) values
 ('00000000-0000-4000-8000-000000000003',current_date,2,4,3,2,'SECRET MEDICAL');
insert into public.injury_flags(athlete_id,body_area,description) values
 ('00000000-0000-4000-8000-000000000003','Knee','SECRET MEDICAL');

insert into public.sparring_logs(athlete_id,training_day,source,intensity,rounds_completed,head_contact,rocked,notes) values
 ('00000000-0000-4000-8000-000000000003',current_date,'free','hard',3,'heavy',true,'SECRET MEDICAL');

create function pg_temp.assert_ok(value boolean, message text) returns void language plpgsql as $$
begin if value is distinct from true then raise exception 'FAILED: %',message; end if; raise notice 'PASS: %',message; end $$;
create function pg_temp.assert_denied(statement text) returns void language plpgsql security invoker as $$
begin execute statement; raise exception 'Unexpectedly allowed: %',statement; exception when insufficient_privilege then raise notice 'PASS: denied %',statement; end $$;

set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000001',true);
select public.coach_connection_command('invite',p_email=>'ATHLETE@test.invalid') as invite \gset
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters())=0,'pending invite reveals no data');
select pg_temp.assert_ok((select count(*) from public.session_completions)=0,'coach cannot read raw training table');
select pg_temp.assert_denied(format('select public.coach_connection_command(''accept'',%L)', :'invite'));
select pg_temp.assert_denied(format('update public.coach_connections set status=''accepted'' where id=%L', :'invite'));
select pg_temp.assert_denied('insert into public.coach_connections(coach_id,invited_email) values(auth.uid(),''x@test.invalid'')');

select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000004',true);
select pg_temp.assert_ok(jsonb_array_length(public.coach_connections_list())=0,'spoofed profile email reveals no invite');
select pg_temp.assert_denied(format('select public.coach_connection_command(''accept'',%L)', :'invite'));
select pg_temp.assert_denied('select public.coach_fighters()');

select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000003',true);
select pg_temp.assert_ok(jsonb_array_length(public.coach_connections_list())=1,'verified auth email receives invite');
select public.coach_connection_command('accept', :'invite',p_recovery=>false,p_injuries=>false);
select pg_temp.assert_ok((select count(*) from public.session_completions)=2,'athlete own data remains available');

select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000001',true);
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters())=1,'accepted fighter visible');
select pg_temp.assert_ok(public.coach_fighters()#>'{0,recovery}'='null'::jsonb and public.coach_fighters()#>'{0,injuries}'='null'::jsonb,'optional health withheld');
select pg_temp.assert_ok(public.coach_fighters()#>'{0,sparring_reports,0,head_contact}'='null'::jsonb and public.coach_fighters()#>'{0,sparring_reports,0,rocked}'='null'::jsonb,'contact health withheld without injury consent');
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters()#>'{0,sparring_reports}')=1,'training intensity shares independently of contact health');
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters()#>'{0,sessions}')=1,'day credits excluded from workload');
select pg_temp.assert_ok(public.coach_fighters('00000000-0000-4000-8000-000000000003')::text not like '%SECRET MEDICAL%','private notes and raw plan excluded');
select pg_temp.assert_denied(format('select public.coach_connection_command(''consent'',%L,p_recovery=>true)', :'invite'));
select pg_temp.assert_denied('update public.session_completions set status=''skipped''');

select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000002',true);
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters('00000000-0000-4000-8000-000000000003'))=0,'unrelated coach cannot query fighter ID');
select pg_temp.assert_denied(format('select public.coach_connection_command(''remove'',%L)', :'invite'));
select public.coach_connection_command('invite',p_email=>'athlete@test.invalid') as second_invite \gset

select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000003',true);
select public.coach_connection_command('accept', :'second_invite');
select public.coach_connection_command('consent', :'invite',p_recovery=>true,p_injuries=>true);
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000001',true);
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters()#>'{0,recovery}')=1 and jsonb_array_length(public.coach_fighters()#>'{0,injuries}')=1,'explicit optional consent shares health summaries');
select pg_temp.assert_ok(public.coach_fighters()#>>'{0,sparring_reports,0,head_contact}'='heavy','contact health shares only after optional consent');
select pg_temp.assert_ok(public.coach_fighters()::text not like '%SECRET MEDICAL%','health consent still excludes medical notes');

reset role;
update public.profiles set health_data_consent=false where id='00000000-0000-4000-8000-000000000003';
set local role authenticated;
select pg_temp.assert_ok(public.coach_fighters()#>'{0,recovery}'='null'::jsonb,'global health consent withdrawal hides shared health');
reset role;
update public.profiles set health_data_consent=true where id='00000000-0000-4000-8000-000000000003';
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000003',true);
select public.coach_connection_command('revoke', :'invite');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000001',true);
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters())=0,'revocation blocks next read immediately');
select pg_temp.assert_denied(format('select public.coach_connection_command(''accept'',%L)', :'invite'));
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000002',true);
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters())=1,'revoking one coach preserves other connection');
select public.coach_connection_command('remove', :'second_invite');
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters())=0,'coach removal blocks future reads');
select public.coach_connection_command('invite',p_email=>'unverified@test.invalid') as unverified \gset
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000005',true);
select pg_temp.assert_denied(format('select public.coach_connection_command(''accept'',%L)', :'unverified'));
set local role anon;
select pg_temp.assert_denied('select public.coach_fighters()');
select pg_temp.assert_denied('select public.coach_connections_list()');
select pg_temp.assert_denied('select * from public.coach_connections');
reset role;
