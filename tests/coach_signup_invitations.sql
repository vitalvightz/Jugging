-- A dual-role operator retains admin access while acting as a consent-bound coach.
insert into auth.users(id,email,email_confirmed_at) values
 ('00000000-0000-4000-8000-000000000006','admin-coach@test.invalid',now());
insert into public.profiles(id,email,role) values
 ('00000000-0000-4000-8000-000000000006','admin-coach@test.invalid','admin');
insert into public.coach_invitation_rules(coach_id,email_domains,configured_by)
 values('00000000-0000-4000-8000-000000000006',array['tutanota.com','tuta.com'],'isolated-test');
insert into auth.users(id,email,email_confirmed_at,created_at) values
 ('00000000-0000-4000-8000-000000000010','new@tuta.com',now(),now()),
 ('00000000-0000-4000-8000-000000000011','other@tutanota.com',null,now()),
 ('00000000-0000-4000-8000-000000000012','unrelated@test.invalid',now(),now()),
 ('00000000-0000-4000-8000-000000000013','lookalike@tuta.com.attacker.invalid',now(),now()),
 ('00000000-0000-4000-8000-000000000014','older@tuta.com',now(),now()-interval '1 day'),
 ('00000000-0000-4000-8000-000000000015','disabled@tuta.com',now(),now());
-- Spoofed profile email is ignored in both directions.
insert into public.profiles(id,email,role)
 select id,case when email='unrelated@test.invalid' then 'spoof@tuta.com' when email='new@tuta.com' then 'different@test.invalid' else email end,'athlete'
 from auth.users where id in ('00000000-0000-4000-8000-000000000010','00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000012','00000000-0000-4000-8000-000000000013','00000000-0000-4000-8000-000000000014');
select pg_temp.assert_ok((select count(*) from public.coach_connections where coach_id='00000000-0000-4000-8000-000000000006')=2,'only future exact-domain Auth signups get invitations');
select pg_temp.assert_ok(not exists(select 1 from public.coach_connections where coach_id='00000000-0000-4000-8000-000000000006' and (status<>'pending' or athlete_id is not null or share_recovery or share_injuries)),'automatic invitations grant no access or health consent');
update public.coach_invitation_rules set enabled=false where coach_id='00000000-0000-4000-8000-000000000006';
insert into public.profiles(id,email,role) values ('00000000-0000-4000-8000-000000000015','disabled@tuta.com','athlete');
select pg_temp.assert_ok(not exists(select 1 from public.coach_connections where invited_email='disabled@tuta.com'),'disabled invitation rule stops future invitations');
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000006',true);
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters())=0,'admin coach sees no unaccepted fighters');
select pg_temp.assert_ok((select role from public.profiles where id=auth.uid())='admin','coaching retains administrator role');
select public.coach_connection_command('invite',p_email=>'unrelated@test.invalid') as operator_invite \gset
select pg_temp.assert_denied('select * from public.coach_invitation_rules');
select pg_temp.assert_denied('update public.coach_invitation_rules set enabled=true');
select pg_temp.assert_denied('select coach_private.invite_matching_signup()');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000010',true);
select id as automatic_invite from public.coach_connections where invited_email='new@tuta.com' \gset
select public.coach_connection_command('accept', :'automatic_invite');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000006',true);
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters())=1,'admin coach reads only explicitly accepted fighters');
select pg_temp.assert_ok(public.coach_fighters()#>'{0,recovery}'='null'::jsonb,'automatic signup invitation does not authorise health sharing');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000010',true);
select public.coach_connection_command('revoke', :'automatic_invite');
select set_config('request.jwt.claim.sub','00000000-0000-4000-8000-000000000006',true);
select pg_temp.assert_ok(jsonb_array_length(public.coach_fighters())=0,'revocation also removes access from an admin coach');
reset role;
