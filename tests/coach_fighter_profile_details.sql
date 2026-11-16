reset role;
insert into auth.users(id,email,email_confirmed_at) values
 ('00000000-0000-4000-8000-000000000061','profile-coach@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000062','profile-athlete@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000063','profile-empty@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000064','profile-pending@test.invalid',now());
insert into public.profiles(id,email,role,full_name,health_data_consent)
 select id,email,case when email like 'profile-coach%' then 'coach'::public.app_role else 'athlete'::public.app_role end,email,true from auth.users where email like 'profile-%';
update public.profiles set date_of_birth=(current_date-interval '25 years')::date,technical_style=array['pressure_fighter'],tactical_style=array['counter_puncher'],stance='orthodox',record_summary='4-1-0' where id='00000000-0000-4000-8000-000000000062';
insert into public.coach_connections(coach_id,athlete_id,invited_email,status,accepted_at)
 select '00000000-0000-4000-8000-000000000061',id,email,'accepted',now() from public.profiles where id in ('00000000-0000-4000-8000-000000000062','00000000-0000-4000-8000-000000000063');
insert into public.coach_connections(coach_id,invited_email) values('00000000-0000-4000-8000-000000000061','profile-pending@test.invalid');
insert into public.external_training_sessions(id,athlete_id,client_request_id,request_fingerprint,training_day,activity,duration_minutes,effort)
 values('30000000-0000-4000-8000-000000000062','00000000-0000-4000-8000-000000000062',gen_random_uuid(),repeat('c',64),current_date-70,'boxing',45,'moderate');
insert into public.external_training_sessions(athlete_id,client_request_id,request_fingerprint,training_day,activity,duration_minutes,effort,deleted_at)
 values('00000000-0000-4000-8000-000000000062',gen_random_uuid(),repeat('d',64),current_date-2,'strength',45,'moderate',now());
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000061',true);
select pg_temp.assert_ok((public.coach_fighters('00000000-0000-4000-8000-000000000062')#>>'{0,age}')::integer=25,'coach receives derived age');
select pg_temp.assert_ok(not ((public.coach_fighters('00000000-0000-4000-8000-000000000062')->0)?'date_of_birth'),'full birth date never projected');
select pg_temp.assert_ok(public.coach_fighters('00000000-0000-4000-8000-000000000062')#>>'{0,technical_style,0}'='pressure_fighter'
 and public.coach_fighters('00000000-0000-4000-8000-000000000062')#>>'{0,stance}'='orthodox'
 and public.coach_fighters('00000000-0000-4000-8000-000000000062')#>>'{0,record}'='4-1-0','existing style stance and record reused');
select pg_temp.assert_ok(public.coach_fighters('00000000-0000-4000-8000-000000000062')#>>'{0,last_training,day}'=(current_date-70)::text
 and public.coach_fighters('00000000-0000-4000-8000-000000000062')#>>'{0,last_training,activity}'='boxing','last training includes older records and ignores deleted sessions');
select pg_temp.assert_ok(public.coach_fighters('00000000-0000-4000-8000-000000000063')#>'{0,age}'='null'::jsonb
 and public.coach_fighters('00000000-0000-4000-8000-000000000063')#>'{0,last_training}'='null'::jsonb
 and public.coach_fighters('00000000-0000-4000-8000-000000000063')#>'{0,record}'='null'::jsonb,'missing profile data and training remain missing despite recent signup');
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters('00000000-0000-4000-8000-000000000064'))=0,'pending athlete profile fields remain invisible');
select pg_temp.assert_ok(public.coach_fighters('00000000-0000-4000-8000-000000000062')#>'{0,injuries}'='null'::jsonb,'profile extension does not grant injury access');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000063',true);
select pg_temp.assert_denied('select public.coach_fighters()');
select id as profile_connection from public.coach_connections where athlete_id=auth.uid() \gset
select public.coach_connection_command('revoke',:'profile_connection');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000061',true);
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters('00000000-0000-4000-8000-000000000063'))=0,'revocation removes profile projection');
reset role;
