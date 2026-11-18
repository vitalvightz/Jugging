reset role;
-- Progress photos: the athlete's own photos on their own profile; nobody else adds them.
insert into auth.users(id,email,email_confirmed_at) values
 ('00000000-0000-4000-8000-0000000000d1','pp-athlete@test.invalid',now()),
 ('00000000-0000-4000-8000-0000000000d3','pp-coach@test.invalid',now());
insert into public.profiles(id,email,role,full_name,health_data_consent)
 select id,email,(case when email like 'pp-coach%' then 'coach' else 'athlete' end)::public.app_role,'PP user',true
 from auth.users where email like 'pp-%';
update public.profiles set access_status='approved' where email like 'pp-%';
insert into public.coach_connections(id,coach_id,invited_email,athlete_id,status,accepted_at,share_media)
 values('66000000-0000-4000-8000-0000000000d1','00000000-0000-4000-8000-0000000000d3','pp-athlete@test.invalid',
        '00000000-0000-4000-8000-0000000000d1','accepted',now(),true);
create temp table pp(kind text, id uuid) on commit drop;
grant all on pp to service_role;
insert into pp select 'photo', id from public.reserve_attachment_upload('00000000-0000-4000-8000-0000000000d1','photo',1000,'image/jpeg');
insert into pp select 'voice', id from public.reserve_attachment_upload('00000000-0000-4000-8000-0000000000d1','voice',1000,'audio/wav');
insert into pp select 'coachvoice', id from public.reserve_attachment_upload('00000000-0000-4000-8000-0000000000d3','voice',1000,'audio/wav');
update public.attachment_assets set status='ready', ready_path=owner_id||'/'||id||'/clean.'||(case kind when 'photo' then 'jpg' else 'wav' end),
  width=case kind when 'photo' then 800 end, height=case kind when 'photo' then 600 end, duration_ms=case kind when 'voice' then 3000 end
  where id in (select id from pp);
set local role service_role;
-- Only photos, only by the athlete, only on their own profile.
select pg_temp.assert_raises(format($q$select public.attach_training_asset('00000000-0000-4000-8000-0000000000d1','00000000-0000-4000-8000-0000000000d1',%L,'progress','00000000-0000-4000-8000-0000000000d1','')$q$,(select id from pp where kind='voice')),'42501');
select pg_temp.assert_raises(format($q$select public.attach_training_asset('00000000-0000-4000-8000-0000000000d1','00000000-0000-4000-8000-0000000000d1',%L,'progress','00000000-0000-4000-8000-0000000000d3','')$q$,(select id from pp where kind='photo')),'42501');
select pg_temp.assert_raises(format($q$select public.attach_training_asset('00000000-0000-4000-8000-0000000000d1','00000000-0000-4000-8000-0000000000d1',%L,'progress','00000000-0000-4000-8000-0000000000d1','x')$q$,(select id from pp where kind='photo')),'42501');
select pg_temp.assert_raises(format($q$select public.attach_training_asset('00000000-0000-4000-8000-0000000000d3','00000000-0000-4000-8000-0000000000d1',%L,'progress','00000000-0000-4000-8000-0000000000d1','')$q$,(select id from pp where kind='coachvoice')),'42501');
select public.attach_training_asset('00000000-0000-4000-8000-0000000000d1','00000000-0000-4000-8000-0000000000d1',id,'progress','00000000-0000-4000-8000-0000000000d1','') from pp where kind='photo';
select pg_temp.assert_ok((select count(*) from public.training_attachments where source='progress' and athlete_id='00000000-0000-4000-8000-0000000000d1')=1,'progress photo attached');
-- Videos never attach to the profile.
select pg_temp.assert_ok(not public.training_record_available('00000000-0000-4000-8000-0000000000d1','progress','00000000-0000-4000-8000-0000000000d1',''),'no videos on progress');
reset role;
-- The table itself refuses unknown sources.
select pg_temp.assert_raises($q$insert into public.training_attachments(athlete_id,author_id,asset_id,source,target_id) values('00000000-0000-4000-8000-0000000000d1','00000000-0000-4000-8000-0000000000d1',gen_random_uuid(),'profile','00000000-0000-4000-8000-0000000000d1')$q$,'23514');
