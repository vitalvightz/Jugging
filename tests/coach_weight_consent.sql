-- Synthetic fixtures in an isolated transaction only. See tools/test_coach_workspace.py.
-- Weight is its own consent: legacy connections do not gain it, and a mixed
-- session's injury-named title stays hidden without injury sharing.
insert into auth.users(id,email,email_confirmed_at) values
 ('00000000-0000-4000-8000-000000000021','weight-coach@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000022','weight-legacy@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000023','weight-new@test.invalid',now());
insert into public.profiles(id,email,role,full_name,health_data_consent)
 select id,email,case when email like 'weight-coach%' then 'coach'::public.app_role else 'athlete'::public.app_role end,'Weight fighter',true
 from auth.users where email like 'weight-%';
insert into public.plans(id,athlete_id,plan_name,structured_plan) values
 ('10000000-0000-4000-8000-000000000022','00000000-0000-4000-8000-000000000022','Mixed camp',
 jsonb_build_object('weeks',jsonb_build_array(jsonb_build_object('days',jsonb_build_array(jsonb_build_object(
   'date',((now() at time zone 'UTC') - interval '4 hours')::date,'sessions',jsonb_build_array(
     jsonb_build_object('session_id','mix','session_type','strength_power','title','Knee Rehab + Strength',
       'blocks',jsonb_build_array(jsonb_build_object('block_type','strength'))),
     jsonb_build_object('session_id','hidden','session_type','strength_power','title','Power',
       'blocks',jsonb_build_array(jsonb_build_object('block_type','rehab'))),
     jsonb_build_object('session_id','plain','session_type','strength_power','title','Power Day',
       'blocks',jsonb_build_array(jsonb_build_object('block_type','strength'))))))))));
update public.profiles set active_plan_id='10000000-0000-4000-8000-000000000022' where id='00000000-0000-4000-8000-000000000022';
insert into public.session_completions(athlete_id,plan_id,session_id,training_day,status,completion_origin)
 select '00000000-0000-4000-8000-000000000022','10000000-0000-4000-8000-000000000022',s,((now() at time zone 'UTC') - interval '4 hours')::date,'done','reported'
 from unnest(array['mix','hidden','plain']) s;
-- Accepted under the earlier wording: recovery and injuries, never weight.
insert into public.coach_connections(id,coach_id,athlete_id,invited_email,status,share_recovery,share_injuries,accepted_at) values
 ('20000000-0000-4000-8000-000000000022','00000000-0000-4000-8000-000000000021','00000000-0000-4000-8000-000000000022',
  'weight-legacy@test.invalid','accepted',true,true,now());

set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000021',true);
select pg_temp.assert_ok((public.coach_fighters('00000000-0000-4000-8000-000000000022')#>>'{0,share_weight}')::boolean is false
  and (public.coach_fighters('00000000-0000-4000-8000-000000000022')#>>'{0,share_recovery}')::boolean,
  'legacy connection keeps recovery but gains no weight access');
select pg_temp.assert_ok(public.coach_connections_list()#>'{0,health_consent}'='null'::jsonb,'coach list carries no athlete consent detail');
select public.coach_connection_command('invite',p_email=>'weight-new@test.invalid') as new_invite \gset

select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000022',true);
select pg_temp.assert_ok((public.coach_connections_list()#>>'{0,share_weight}')::boolean is false
  and (public.coach_connections_list()#>>'{0,health_consent}')::boolean,'athlete sees weight is not yet shared');
select public.coach_connection_command('consent','20000000-0000-4000-8000-000000000022',p_recovery=>true,p_injuries=>false,p_weight=>true);
select pg_temp.assert_ok((public.coach_connections_list()#>>'{0,share_weight}')::boolean
  and (public.coach_connections_list()#>>'{0,share_injuries}')::boolean is false,'explicit athlete consent shares weight');

select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000023',true);
select public.coach_connection_command('accept', :'new_invite',p_recovery=>true,p_injuries=>true,p_weight=>true);
select pg_temp.assert_ok((public.coach_connections_list()#>>'{0,share_weight}')::boolean,'one-tap acceptance shares weight');

select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000021',true);
select pg_temp.assert_ok((public.coach_fighters('00000000-0000-4000-8000-000000000022')#>>'{0,share_weight}')::boolean,'coach sees weight once consented');
select pg_temp.assert_ok(public.coach_fighters('00000000-0000-4000-8000-000000000022')::text not like '%Knee%'
  and public.coach_fighters('00000000-0000-4000-8000-000000000022')::text not like '%Rehab%','mixed session titles withheld without injury sharing');
select pg_temp.assert_ok((select count(*) from jsonb_array_elements(public.coach_fighters('00000000-0000-4000-8000-000000000022')#>'{0,sessions}') s
  where s->>'title' is null and s->>'type'='strength_power')=2,'rehab-carrying sessions keep their workload but lose their titles');
select pg_temp.assert_ok(public.coach_fighters('00000000-0000-4000-8000-000000000022')::text like '%Power Day%','unrelated titles still shown');

reset role;
update public.profiles set health_data_consent=false where id='00000000-0000-4000-8000-000000000022';
set local role authenticated;
select pg_temp.assert_ok((public.coach_fighters('00000000-0000-4000-8000-000000000022')#>>'{0,share_weight}')::boolean is false,'health consent withdrawal withholds weight');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000022',true);
select pg_temp.assert_ok((public.coach_connections_list()#>>'{0,health_consent}')::boolean is false,'athlete list shows health consent is off');
select public.coach_connection_command('consent','20000000-0000-4000-8000-000000000022',p_recovery=>true,p_injuries=>true);
select pg_temp.assert_ok((public.coach_connections_list()#>>'{0,share_weight}')::boolean is false,'consent without weight clears weight');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000021',true);
select public.coach_connection_command('remove', :'new_invite');
reset role;
select pg_temp.assert_ok(not (select share_weight from public.coach_connections where id=:'new_invite'),'removal clears weight sharing');
