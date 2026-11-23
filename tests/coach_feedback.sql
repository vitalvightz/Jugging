reset role;
insert into auth.users(id,email,email_confirmed_at) values
 ('00000000-0000-4000-8000-000000000051','feedback-coach@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000052','feedback-athlete@test.invalid',now()),
 ('00000000-0000-4000-8000-000000000053','feedback-other@test.invalid',now());
insert into public.profiles(id,email,role,full_name,health_data_consent)
 select id,email,case when email like 'feedback-athlete%' then 'athlete'::public.app_role else 'coach'::public.app_role end,'Feedback author',true
 from auth.users where email like 'feedback-%';
insert into public.coach_connections(id,coach_id,athlete_id,invited_email,status,accepted_at)
 values('20000000-0000-4000-8000-000000000052','00000000-0000-4000-8000-000000000051','00000000-0000-4000-8000-000000000052','feedback-athlete@test.invalid','accepted',now());
insert into public.external_training_sessions(id,athlete_id,client_request_id,request_fingerprint,training_day,activity,duration_minutes,effort,takeaway)
 values('30000000-0000-4000-8000-000000000052','00000000-0000-4000-8000-000000000052',gen_random_uuid(),repeat('b',64),current_date-1,'strength',40,'moderate','PRIVATE ATHLETE NOTE');
insert into public.manual_exercise_entries(id,athlete_id,external_session_id,client_entry_id,training_day,exercise_key,display_name,measurement_format,actual,notes)
 values('40000000-0000-4000-8000-000000000052','00000000-0000-4000-8000-000000000052','30000000-0000-4000-8000-000000000052',gen_random_uuid(),current_date-1,'back-squat','Squat','strength','{}','PRIVATE EXERCISE NOTE');
insert into public.sparring_logs(id,athlete_id,training_day,source,intensity,rounds_completed,head_contact)
 values('50000000-0000-4000-8000-000000000052','00000000-0000-4000-8000-000000000052',current_date-1,'free','medium',5,'none');
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000051',true);
select public.save_coach_feedback('00000000-0000-4000-8000-000000000052','external','30000000-0000-4000-8000-000000000052','','Good work');
select public.save_coach_feedback('00000000-0000-4000-8000-000000000052','external','30000000-0000-4000-8000-000000000052','40000000-0000-4000-8000-000000000052','Control the descent');
select public.save_coach_feedback('00000000-0000-4000-8000-000000000052','sparring','50000000-0000-4000-8000-000000000052','','Keep your guard up');
select pg_temp.assert_ok((select count(*) from public.coach_feedback)=3,'coach can leave separate session, exercise and report feedback');
select public.save_coach_feedback('00000000-0000-4000-8000-000000000052','external','30000000-0000-4000-8000-000000000052','','Better work');
select pg_temp.assert_ok((select count(*) from public.coach_feedback)=3 and exists(select 1 from public.coach_feedback where note='Better work'),'edit updates own feedback without a thread');
select pg_temp.assert_denied('select public.save_coach_feedback(''00000000-0000-4000-8000-000000000052'',''external'',''30000000-0000-4000-8000-000000000052'',''bad-exercise'',''Bad target'')');
select pg_temp.assert_denied('select public.save_coach_feedback(''00000000-0000-4000-8000-000000000052'',''external'',''30000000-0000-4000-8000-000000000099'','''',''Bad target'')');
select pg_temp.assert_denied('update public.coach_feedback set note=''spoofed''');
select pg_temp.assert_denied('delete from public.coach_feedback');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000053',true);
select pg_temp.assert_ok((select count(*) from public.coach_feedback)=0,'unconnected coach cannot read received feedback');
select pg_temp.assert_denied('select public.save_coach_feedback(''00000000-0000-4000-8000-000000000052'',''external'',''30000000-0000-4000-8000-000000000052'','''',''Intrusion'')');
reset role;
insert into public.coach_connections(coach_id,athlete_id,invited_email,status,accepted_at)
 values('00000000-0000-4000-8000-000000000053','00000000-0000-4000-8000-000000000052','feedback-athlete@test.invalid','accepted',now());
set local role authenticated;
select public.save_coach_feedback('00000000-0000-4000-8000-000000000052','external','30000000-0000-4000-8000-000000000052','','Another coach note');
select pg_temp.assert_ok((select count(*) from public.coach_feedback)=1,'connected coach reads only own feedback');
select pg_temp.assert_ok((select note from public.coach_feedback)='Another coach note','one coach cannot overwrite another coach feedback');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000052',true);
select pg_temp.assert_ok((select count(*) from public.coach_feedback)=4,'athlete sees all received feedback');
select pg_temp.assert_ok((select takeaway from public.external_training_sessions where id='30000000-0000-4000-8000-000000000052')='PRIVATE ATHLETE NOTE','session feedback never overwrites athlete notes');
select pg_temp.assert_ok((select notes from public.manual_exercise_entries where id='40000000-0000-4000-8000-000000000052')='PRIVATE EXERCISE NOTE','exercise feedback never overwrites athlete notes');
select pg_temp.assert_denied('select public.save_coach_feedback(''00000000-0000-4000-8000-000000000052'',''external'',''30000000-0000-4000-8000-000000000052'','''',''Impersonation'')');
select public.coach_connection_command('revoke','20000000-0000-4000-8000-000000000052');
select pg_temp.assert_ok((select count(*) from public.coach_feedback)=4,'received notes survive disconnection');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000051',true);
select pg_temp.assert_ok((select count(*) from public.coach_feedback)=0,'revoked coach loses feedback reads');
select pg_temp.assert_denied('select public.save_coach_feedback(''00000000-0000-4000-8000-000000000052'',''external'',''30000000-0000-4000-8000-000000000052'','''',''After revoke'')');
reset role;

delete from public.profiles where id='00000000-0000-4000-8000-000000000051';
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000052',true);
select pg_temp.assert_ok((select count(*) from public.coach_feedback)=4,'coach deletion preserves received feedback');
select pg_temp.assert_ok((select count(*) from public.coach_feedback where coach_id is null and coach_name='Feedback author')=3,'deleted coach attribution retained');
reset role;
-- Planned exercise identity is the immutable occurrence plus block id.
insert into public.plans(id,athlete_id,plan_name,structured_plan)
 values('10000000-0000-4000-8000-000000000052','00000000-0000-4000-8000-000000000052','Feedback test','{}');
insert into public.today_checkins(athlete_id,plan_id,training_day,sleep,body,pain,phase,recommendation_state)
 values('00000000-0000-4000-8000-000000000052','10000000-0000-4000-8000-000000000052',current_date-1,'good','normal','none','GPP','train_as_planned');
insert into public.session_completions(id,athlete_id,plan_id,session_id,training_day,status,completion_origin,prescription_snapshot)
 values('60000000-0000-4000-8000-000000000052','00000000-0000-4000-8000-000000000052','10000000-0000-4000-8000-000000000052','lifting',current_date-1,'done','reported',
 jsonb_build_object('readiness_context',(select jsonb_build_object('id',id,'updated_at',updated_at) from public.today_checkins where athlete_id='00000000-0000-4000-8000-000000000052' and plan_id='10000000-0000-4000-8000-000000000052'),'injury_context','[]'::jsonb,'revision',repeat('1',64),'plan_id','10000000-0000-4000-8000-000000000052','training_day',current_date-1,'session',jsonb_build_object('session_id','lifting','session_type','strength_power','title','Power',
 'blocks',jsonb_build_array(jsonb_build_object('block_id','squat-a','display_name','Squat'),jsonb_build_object('block_id','squat-b','display_name','Squat')))));
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000053',true);
select public.save_coach_feedback('00000000-0000-4000-8000-000000000052','unlxck','60000000-0000-4000-8000-000000000052','squat-a','First exercise');
select public.save_coach_feedback('00000000-0000-4000-8000-000000000052','unlxck','60000000-0000-4000-8000-000000000052','squat-b','Second exercise');
select pg_temp.assert_ok((select count(*) from public.coach_feedback where source='unlxck')=2,'same-named exercises keep distinct feedback');
select pg_temp.assert_denied('select public.save_coach_feedback(''00000000-0000-4000-8000-000000000052'',''unlxck'',''60000000-0000-4000-8000-000000000052'',''other-block'',''Bad target'')');
reset role;
-- Feedback written on injury work while sharing was on is withheld once the
-- athlete turns injury sharing off, including from the newest-note read.
insert into public.session_completions(id,athlete_id,plan_id,session_id,training_day,status,completion_origin,prescription_snapshot)
 values('60000000-0000-4000-8000-000000000053','00000000-0000-4000-8000-000000000052','10000000-0000-4000-8000-000000000052','knee-rehab',current_date-1,'done','reported',
 jsonb_build_object('readiness_context',(select jsonb_build_object('id',id,'updated_at',updated_at) from public.today_checkins where athlete_id='00000000-0000-4000-8000-000000000052' and plan_id='10000000-0000-4000-8000-000000000052'),'injury_context','[]'::jsonb,'revision',repeat('2',64),'plan_id','10000000-0000-4000-8000-000000000052','training_day',current_date-1,'session',jsonb_build_object('session_id','knee-rehab','session_type','rehab','title','Knee rehab',
 'blocks',jsonb_build_array(jsonb_build_object('block_id','tke','display_name','Terminal knee extension')))));
update public.coach_connections set share_injuries=true where coach_id='00000000-0000-4000-8000-000000000053';
set local role authenticated;
select public.save_coach_feedback('00000000-0000-4000-8000-000000000052','unlxck','60000000-0000-4000-8000-000000000053','','PRIVATE REHAB FEEDBACK');
select public.save_coach_feedback('00000000-0000-4000-8000-000000000052','sparring','50000000-0000-4000-8000-000000000052','','Sparring note');
reset role;
update public.coach_feedback set updated_at=now()+interval '1 minute' where target_id='60000000-0000-4000-8000-000000000053';
set local role authenticated;
select pg_temp.assert_ok((select note from public.coach_feedback where athlete_id='00000000-0000-4000-8000-000000000052' order by updated_at desc,id desc limit 1)='PRIVATE REHAB FEEDBACK','injury feedback readable while injury sharing is on');
reset role;
update public.coach_connections set share_injuries=false where coach_id='00000000-0000-4000-8000-000000000053';
set local role authenticated;
select pg_temp.assert_ok(not exists(select 1 from public.coach_feedback where note='PRIVATE REHAB FEEDBACK'),'revoked injury sharing hides feedback on injury records');
select pg_temp.assert_ok((select note from public.coach_feedback where athlete_id='00000000-0000-4000-8000-000000000052' order by updated_at desc,id desc limit 1)<>'PRIVATE REHAB FEEDBACK','newest-note read skips withheld injury feedback');
select pg_temp.assert_ok((select count(*) from public.coach_feedback where source='unlxck')=2 and exists(select 1 from public.coach_feedback where note='Sparring note'),'non-injury and sparring feedback stay readable');
reset role;
update public.coach_connections set share_injuries=true where coach_id='00000000-0000-4000-8000-000000000053';
set local role authenticated;
select pg_temp.assert_ok(exists(select 1 from public.coach_feedback where note='PRIVATE REHAB FEEDBACK'),'restoring injury sharing restores the feedback');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000052',true);
select pg_temp.assert_ok(exists(select 1 from public.coach_feedback where note='PRIVATE REHAB FEEDBACK'),'athlete always sees received feedback');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000053',true);
reset role;
update public.profiles set health_data_consent=false where id='00000000-0000-4000-8000-000000000052';
set local role authenticated;
select pg_temp.assert_ok((select count(*) from public.coach_feedback)=0,'health withdrawal hides coach feedback from coach');
select pg_temp.assert_denied('select public.save_coach_feedback(''00000000-0000-4000-8000-000000000052'',''unlxck'',''60000000-0000-4000-8000-000000000052'',''squat-a'',''After consent withdrawn'')');
reset role;
