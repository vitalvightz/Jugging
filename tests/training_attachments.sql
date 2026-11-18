reset role;
create function pg_temp.assert_raises(statement text, code text) returns void language plpgsql security invoker as $$
begin execute statement; raise exception 'Unexpectedly allowed: %',statement;
exception when others then
  if sqlstate <> code then raise exception 'Expected % but got % (%) for %',code,sqlstate,sqlerrm,statement; end if;
  raise notice 'PASS: % raised %',statement,code; end $$;
insert into auth.users(id,email,email_confirmed_at) values
 ('00000000-0000-4000-8000-0000000000a1','attach-athlete@test.invalid',now()),
 ('00000000-0000-4000-8000-0000000000a2','attach-other@test.invalid',now());
insert into public.profiles(id,email,role,full_name,health_data_consent)
 select id,email,'athlete'::public.app_role,'Attach athlete',true from auth.users where email like 'attach-%';
-- Verified uploads (worker-finished) for both athletes, plus one still processing.
insert into public.private_video_assets(id,athlete_id,status,size_bytes,content_type,intake_path,rendition_path,duration_seconds,width,height,sha256,processor_version,created_at)
 select ('61000000-0000-4000-8000-00000000000'||n)::uuid,
  case when n=3 then '00000000-0000-4000-8000-0000000000a2'::uuid else '00000000-0000-4000-8000-0000000000a1'::uuid end,
  'verified',1000,'video/mp4','intake/'||n,'ready/'||n,10,720,1280,repeat('a',64),'isolated-v1',now()-interval '10 days'
 from generate_series(1,9) n;
insert into public.private_video_assets(id,athlete_id,status,size_bytes,content_type,intake_path)
 values('61000000-0000-4000-8000-000000000020','00000000-0000-4000-8000-0000000000a1','processing',1000,'video/mp4','intake/20');
insert into public.private_exercise_media(id,athlete_id,provider,asset_id,start_s,end_s,orientation)
 select ('62000000-0000-4000-8000-00000000000'||n)::uuid,a.athlete_id,'uploaded',a.id,0,10,'portrait'
 from generate_series(1,9) n join public.private_video_assets a on a.id=('61000000-0000-4000-8000-00000000000'||n)::uuid;
insert into public.external_training_sessions(id,athlete_id,client_request_id,request_fingerprint,training_day,activity,duration_minutes,effort)
 values('63000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-0000000000a1',gen_random_uuid(),repeat('c',64),current_date-1,'strength',40,'moderate'),
       ('63000000-0000-4000-8000-000000000072','00000000-0000-4000-8000-0000000000a2',gen_random_uuid(),repeat('d',64),current_date-1,'strength',40,'moderate');
insert into public.manual_exercise_entries(id,athlete_id,external_session_id,client_entry_id,training_day,exercise_key,display_name,measurement_format,actual)
 values('64000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-0000000000a1','63000000-0000-4000-8000-000000000071',gen_random_uuid(),current_date-1,'back-squat','Squat','strength','{}');
insert into public.sparring_logs(id,athlete_id,training_day,source,intensity,rounds_completed,head_contact)
 values('65000000-0000-4000-8000-000000000071','00000000-0000-4000-8000-0000000000a1',current_date-1,'free','medium',5,'none');

set local role service_role;
select public.attach_training_media('00000000-0000-4000-8000-0000000000a1','62000000-0000-4000-8000-000000000001','external','63000000-0000-4000-8000-000000000071','');
select public.attach_training_media('00000000-0000-4000-8000-0000000000a1','62000000-0000-4000-8000-000000000002','external','63000000-0000-4000-8000-000000000071','64000000-0000-4000-8000-000000000071');
select public.attach_training_media('00000000-0000-4000-8000-0000000000a1','62000000-0000-4000-8000-000000000004','sparring','65000000-0000-4000-8000-000000000071','');
select pg_temp.assert_ok((select count(*) from public.training_attachments)=3,'athlete attaches videos to a session, one exercise and a sparring report');
select pg_temp.assert_ok((select count(*) from public.attach_training_media('00000000-0000-4000-8000-0000000000a1','62000000-0000-4000-8000-000000000001','external','63000000-0000-4000-8000-000000000071',''))=0,'a video is attached once');
select pg_temp.assert_raises($q$select public.attach_training_media('00000000-0000-4000-8000-0000000000a1','62000000-0000-4000-8000-000000000003','external','63000000-0000-4000-8000-000000000071','')$q$,'42501');
select pg_temp.assert_raises($q$select public.attach_training_media('00000000-0000-4000-8000-0000000000a1','62000000-0000-4000-8000-000000000005','external','63000000-0000-4000-8000-000000000072','')$q$,'42501');
select pg_temp.assert_raises($q$select public.attach_training_media('00000000-0000-4000-8000-0000000000a1','62000000-0000-4000-8000-000000000005','external','63000000-0000-4000-8000-000000000071','not-an-entry')$q$,'42501');
select pg_temp.assert_raises($q$select public.attach_training_media('00000000-0000-4000-8000-0000000000a1','62000000-0000-4000-8000-000000000005','sparring','65000000-0000-4000-8000-000000000071','ref')$q$,'42501');
update public.private_video_assets set status='processing' where id='61000000-0000-4000-8000-000000000005';
select pg_temp.assert_raises($q$select public.attach_training_media('00000000-0000-4000-8000-0000000000a1','62000000-0000-4000-8000-000000000005','external','63000000-0000-4000-8000-000000000071','')$q$,'55000');
update public.private_video_assets set status='verified' where id='61000000-0000-4000-8000-000000000005';
select public.attach_training_media('00000000-0000-4000-8000-0000000000a1',('62000000-0000-4000-8000-00000000000'||n)::uuid,'external','63000000-0000-4000-8000-000000000071','')
 from generate_series(5,9) n where n<>6 or true;
select pg_temp.assert_ok((select count(*) from public.training_attachments where target_id='63000000-0000-4000-8000-000000000071' and exercise_ref='')=6,'six videos fill one record');
reset role;
-- Cleanup keeps every attached video and still removes an unattached old one.
update public.training_attachments set created_at=created_at where false;
set local role service_role;
select pg_temp.assert_ok(not exists(select 1 from public.private_video_cleanup_candidates() c
  join public.training_attachments t on t.media_id in (select id from public.private_exercise_media where asset_id=c.id)),'cleanup never removes an attached video');
reset role;
select pg_temp.assert_ok((select status from public.private_video_assets where id='61000000-0000-4000-8000-000000000003')='deleted','cleanup still removes unattached old videos');

set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-0000000000a1',true);
select pg_temp.assert_denied('select * from public.training_attachments');
select pg_temp.assert_denied($q$select public.attach_training_media('00000000-0000-4000-8000-0000000000a1','62000000-0000-4000-8000-000000000001','external','63000000-0000-4000-8000-000000000071','')$q$);
reset role;
