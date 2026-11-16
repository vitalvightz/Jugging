-- Runs only inside the full-schema harness transaction; never production.
reset role;
insert into auth.users(id,email,email_confirmed_at) values
 ('00000000-0000-4000-8000-000000000041','logging-coach@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000042','logging-athlete@test.invalid',now());
insert into public.profiles(id,email,role,full_name,athlete_timezone,health_data_consent)
 select id,email,case when email like 'logging-coach%' then 'coach'::public.app_role else 'athlete'::public.app_role end,'Logging test','UTC',true
 from auth.users where email like 'logging-%';
insert into public.coach_connections(id,coach_id,athlete_id,invited_email,status,accepted_at) values
 ('20000000-0000-4000-8000-000000000042','00000000-0000-4000-8000-000000000041','00000000-0000-4000-8000-000000000042','logging-athlete@test.invalid','accepted',now());
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000041',true);
select pg_temp.assert_ok((public.coach_fighters()#>>'{0,allow_session_logging}')::boolean is false,'legacy accepted connection remains read-only');
select pg_temp.assert_denied('select public.coach_connection_command(''consent'',''20000000-0000-4000-8000-000000000042'',p_logging=>true)');
select pg_temp.assert_denied('update public.coach_connections set allow_session_logging=true');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000042',true);
select public.coach_connection_command('consent','20000000-0000-4000-8000-000000000042',p_logging=>true);
select pg_temp.assert_ok((public.coach_connections_list()#>>'{0,allow_session_logging}')::boolean,'athlete explicitly authorises logging');
select pg_temp.assert_ok((public.coach_connections_list()#>>'{0,share_weight}')::boolean is false,'logging permission does not grant health sharing');
reset role;
insert into public.external_training_sessions(id,athlete_id,client_request_id,request_fingerprint,training_day,activity,duration_minutes,effort,logged_by_coach_id,logged_by_coach_name) values
 ('30000000-0000-4000-8000-000000000042','00000000-0000-4000-8000-000000000042',gen_random_uuid(),repeat('a',64),current_date-1,'strength',30,'moderate','00000000-0000-4000-8000-000000000041','Logging coach');
insert into public.manual_exercise_entries(athlete_id,external_session_id,client_entry_id,training_day,exercise_key,display_name,measurement_format,actual) values
 ('00000000-0000-4000-8000-000000000042','30000000-0000-4000-8000-000000000042',gen_random_uuid(),current_date-1,'back-squat','Back squat','strength','{"sets":3,"reps":5}');
select pg_temp.assert_ok(not exists(select 1 from public.xp_recorded_training_days('00000000-0000-4000-8000-000000000042')),'coach session excluded from training-day XP');
select pg_temp.assert_ok(not public.xp_has_exercise_result('00000000-0000-4000-8000-000000000042'),'coach exercises excluded from first-result XP');
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000041',true);
select pg_temp.assert_ok((select count(*) from public.external_training_sessions)=0,'coach cannot read athlete table directly');
select pg_temp.assert_denied('update public.external_training_sessions set duration_minutes=60');
select pg_temp.assert_denied('update public.manual_exercise_entries set actual=''{}''');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000042',true);
select pg_temp.assert_ok((select count(*) from public.external_training_sessions)=1,'athlete sees coach session in own records');
select public.coach_connection_command('revoke','20000000-0000-4000-8000-000000000042');
select pg_temp.assert_ok(jsonb_array_length(public.coach_connections_list())=0,'athlete revokes connection');
reset role;
select pg_temp.assert_ok((select allow_session_logging is false from public.coach_connections where id='20000000-0000-4000-8000-000000000042'),'revocation clears write consent');
delete from public.profiles where id='00000000-0000-4000-8000-000000000041';
select pg_temp.assert_ok((select logged_by_coach_id is null and logged_by_coach_name='Logging coach' from public.external_training_sessions where id='30000000-0000-4000-8000-000000000042'),'coach deletion preserves permanent attribution');
select pg_temp.assert_ok(not public.xp_has_exercise_result('00000000-0000-4000-8000-000000000042')
 and not exists(select 1 from public.xp_recorded_training_days('00000000-0000-4000-8000-000000000042')),'coach deletion never unlocks XP');
