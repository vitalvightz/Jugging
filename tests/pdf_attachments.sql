reset role;
-- PDFs: reserved per page, uploaded only to reserved page paths, ready only when
-- every page is rebuilt, and shared by a coach only with media sharing on.
insert into auth.users(id,email,email_confirmed_at) values
 ('00000000-0000-4000-8000-0000000000c1','pdf-athlete@test.invalid',now()),
 ('00000000-0000-4000-8000-0000000000c2','pdf-other@test.invalid',now()),
 ('00000000-0000-4000-8000-0000000000c3','pdf-coach@test.invalid',now());
insert into public.profiles(id,email,role,full_name,health_data_consent)
 select id,email,(case when email like 'pdf-coach%' then 'coach' else 'athlete' end)::public.app_role,'PDF user',true
 from auth.users where email like 'pdf-%';
update public.profiles set access_status='approved' where email like 'pdf-%';
insert into public.coach_connections(id,coach_id,invited_email,athlete_id,status,accepted_at,share_media)
 values('66000000-0000-4000-8000-0000000000c1','00000000-0000-4000-8000-0000000000c3','pdf-athlete@test.invalid',
        '00000000-0000-4000-8000-0000000000c1','accepted',now(),true);
insert into public.external_training_sessions(id,athlete_id,client_request_id,request_fingerprint,training_day,activity,duration_minutes,effort)
 values('63000000-0000-4000-8000-0000000000c1','00000000-0000-4000-8000-0000000000c1',gen_random_uuid(),repeat('f',64),current_date-1,'boxing',40,'hard');

set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-0000000000c1',true);
select pg_temp.assert_denied($q$select public.reserve_attachment_document('00000000-0000-4000-8000-0000000000c1',1000,2,'plan.pdf')$q$);
select pg_temp.assert_denied('select * from public.attachment_pages');
reset role;

set local role service_role;
-- Names are display-only and plain; page counts are 1 to 30.
select pg_temp.assert_raises($q$select public.reserve_attachment_document('00000000-0000-4000-8000-0000000000c1',1000,2,'../../etc/passwd')$q$,'23514');
select pg_temp.assert_raises($q$select public.reserve_attachment_document('00000000-0000-4000-8000-0000000000c1',1000,2,'<script>.pdf')$q$,'23514');
select pg_temp.assert_raises($q$select public.reserve_attachment_document('00000000-0000-4000-8000-0000000000c1',1000,31,'plan.pdf')$q$,'23514');
select pg_temp.assert_raises($q$select public.reserve_attachment_document('00000000-0000-4000-8000-0000000000c1',9000000,2,'plan.pdf')$q$,'23514');
create temp table pd(kind text, id uuid, path text) on commit drop;
grant all on pd to service_role, authenticated;
insert into pd select 'doc', id, intake_path from public.reserve_attachment_document('00000000-0000-4000-8000-0000000000c1',5000,2,'Week 1 plan.pdf');
insert into pd select 'coachdoc', id, intake_path from public.reserve_attachment_document('00000000-0000-4000-8000-0000000000c3',5000,1,'Nutrition guide.pdf');
select pg_temp.assert_ok((select count(*) from public.attachment_pages where asset_id=(select id from pd where kind='doc'))=2,'pages reserved');
reset role;

-- Uploads: only the owner, only reserved page numbers, only JPEG page names.
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-0000000000c2',true);
select pg_temp.assert_denied(format($q$insert into storage.objects(id,bucket_id,name) values(gen_random_uuid(),'attachment-intake',%L)$q$,(select path from pd where kind='doc')||'/001.jpg'));
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-0000000000c1',true);
select pg_temp.assert_denied(format($q$insert into storage.objects(id,bucket_id,name) values(gen_random_uuid(),'attachment-intake',%L)$q$,(select path from pd where kind='doc')||'/003.jpg'));
select pg_temp.assert_denied(format($q$insert into storage.objects(id,bucket_id,name) values(gen_random_uuid(),'attachment-intake',%L)$q$,(select path from pd where kind='doc')||'/001.pdf'));
select pg_temp.assert_denied(format($q$insert into storage.objects(id,bucket_id,name) values(gen_random_uuid(),'attachment-intake',%L)$q$,(select path from pd where kind='doc')||'/../x/001.jpg'));
insert into storage.objects(id,bucket_id,name) select gen_random_uuid(),'attachment-intake',path||'/001.jpg' from pd where kind='doc';
reset role;

set local role service_role;
-- Nothing starts until every page has arrived.
select pg_temp.assert_ok((select count(*) from public.claim_attachment_page((select id from pd where kind='doc'),1))=0,'waits for all pages');
reset role;
insert into storage.objects(id,bucket_id,name) select gen_random_uuid(),'attachment-intake',path||'/002.jpg' from pd where kind='doc';
insert into storage.objects(id,bucket_id,name) select gen_random_uuid(),'attachment-intake',path||'/001.jpg' from pd where kind='coachdoc';
set local role service_role;
-- Photos and voice notes go through their own claim; documents never do.
select pg_temp.assert_ok((select count(*) from public.claim_attachment_processing((select id from pd where kind='doc')))=0,'no single-file claim for a document');
create temp table pt(page integer, token uuid) on commit drop;
grant all on pt to service_role;
insert into pt select page, page_token from public.claim_attachment_page((select id from pd where kind='doc'),1);
select pg_temp.assert_ok((select count(*) from pt)=1,'page claimed');
select pg_temp.assert_ok((select count(*) from public.claim_attachment_page((select id from pd where kind='doc'),1))=0,'page claimed once, never reclaimed');
-- A wrong token cannot finish or reject a page.
select pg_temp.assert_raises(format($q$select public.finish_attachment_page(%L,1,gen_random_uuid(),900,1273)$q$,(select id from pd where kind='doc')),'55000');
select public.reject_attachment_page((select id from pd where kind='doc'),1,gen_random_uuid(),'nope');
select pg_temp.assert_ok((select status from public.attachment_assets where id=(select id from pd where kind='doc'))='processing','wrong token does not reject');
select public.finish_attachment_page((select id from pd where kind='doc'),1,(select token from pt where page=1),900,1273);
-- Pages over 1600 px are refused by the table itself.
-- Assembly waits for the last page.
select pg_temp.assert_ok((select count(*) from public.claim_attachment_assembly((select id from pd where kind='doc')))=0,'assembly waits for every page');
insert into pt select page, page_token from public.claim_attachment_page((select id from pd where kind='doc'),2);
select pg_temp.assert_raises(format($q$select public.finish_attachment_page(%L,2,%L,1700,900)$q$,(select id from pd where kind='doc'),(select token from pt where page=2)),'23514');
select public.finish_attachment_page((select id from pd where kind='doc'),2,(select token from pt where page=2),900,1273);
create temp table pa(id uuid, token uuid, ready text) on commit drop;
grant all on pa to service_role;
insert into pa select id, processing_token, ready_path from public.claim_attachment_assembly((select id from pd where kind='doc'));
select pg_temp.assert_ok((select ready from pa)=(select '00000000-0000-4000-8000-0000000000c1/'||id||'/clean.pdf' from pd where kind='doc'),'fixed ready path');
select pg_temp.assert_ok((select count(*) from public.claim_attachment_assembly((select id from pd where kind='doc')))=0,'assembly claimed once');
select pg_temp.assert_ok((select count(*) from public.claim_attachment_page((select id from pd where kind='doc'),1))=0,'no page claims after assembly');
select pg_temp.assert_raises(format($q$select public.finish_attachment(%L,gen_random_uuid(),%L,null,null,null)$q$,(select id from pa),(select ready from pa)),'55000');
select pg_temp.assert_raises(format($q$select public.finish_attachment(%L,%L,'elsewhere/clean.pdf',null,null,null)$q$,(select id from pa),(select token from pa)),'55000');
select public.finish_attachment(id,token,ready,null,null,null) from pa;
select pg_temp.assert_ok((select status from public.attachment_assets where id=(select id from pd where kind='doc'))='ready','document ready');

-- A bad page rejects the whole coach document; nothing is ever released.
delete from pt;
insert into pt select page, page_token from public.claim_attachment_page((select id from pd where kind='coachdoc'),1);
select public.reject_attachment_page((select id from pd where kind='coachdoc'),1,(select token from pt where page=1),'Damaged page');
select pg_temp.assert_ok((select status from public.attachment_assets where id=(select id from pd where kind='coachdoc'))='rejected','bad page rejects document');
select pg_temp.assert_ok((select count(*) from public.claim_attachment_assembly((select id from pd where kind='coachdoc')))=0,'rejected document never assembles');
reset role;
insert into pd select 'coachdoc2', id, intake_path from public.reserve_attachment_document('00000000-0000-4000-8000-0000000000c3',5000,1,'Nutrition guide.pdf');
insert into storage.objects(id,bucket_id,name) select gen_random_uuid(),'attachment-intake',path||'/001.jpg' from pd where kind='coachdoc2';
set local role service_role;
delete from pt;
insert into pt select page, page_token from public.claim_attachment_page((select id from pd where kind='coachdoc2'),1);
select public.finish_attachment_page((select id from pd where kind='coachdoc2'),1,(select token from pt),900,1273);
delete from pa;
insert into pa select id, processing_token, ready_path from public.claim_attachment_assembly((select id from pd where kind='coachdoc2'));
select public.finish_attachment(id,token,ready,null,null,null) from pa;
-- The rejected document cannot be attached by anyone.
select pg_temp.assert_raises(format($q$select public.attach_training_asset('00000000-0000-4000-8000-0000000000c3','00000000-0000-4000-8000-0000000000c1',%L,'external','63000000-0000-4000-8000-0000000000c1','')$q$,(select id from pd where kind='coachdoc')),'55000');

-- The athlete attaches their PDF; the coach shares one while media sharing is on.
select public.attach_training_asset('00000000-0000-4000-8000-0000000000c1','00000000-0000-4000-8000-0000000000c1',id,'external','63000000-0000-4000-8000-0000000000c1','') from pd where kind='doc';
reset role;
update public.coach_connections set share_media=false where id='66000000-0000-4000-8000-0000000000c1';
set local role service_role;
select pg_temp.assert_raises(format($q$select public.attach_training_asset('00000000-0000-4000-8000-0000000000c3','00000000-0000-4000-8000-0000000000c1',%L,'external','63000000-0000-4000-8000-0000000000c1','')$q$,(select id from pd where kind='coachdoc2')),'42501');
reset role;
update public.coach_connections set share_media=true where id='66000000-0000-4000-8000-0000000000c1';
set local role service_role;
select public.attach_training_asset('00000000-0000-4000-8000-0000000000c3','00000000-0000-4000-8000-0000000000c1',id,'external','63000000-0000-4000-8000-0000000000c1','') from pd where kind='coachdoc2';
select pg_temp.assert_ok((select count(*) from public.training_attachments where target_id='63000000-0000-4000-8000-0000000000c1')=2,'athlete and coach PDFs attached');
reset role;
