insert into profiles(id,email,full_name) select ('00000000-0000-4000-8000-' || lpad(n::text,12,'0'))::uuid, 'test@example.invalid', 'Test' from generate_series(1,7) n;
insert into reactivation_email_preferences(profile_id,enabled,opted_in_at) select id,true,now() from profiles;
insert into product_activity select id::text,id,now()-interval '40 days' from profiles where id::text not like '%007';
-- #1 is a plan-free long-inactive eligible adult. #2 is active via visits only.
insert into athlete_daily_activity select id,current_date from profiles where id::text like '%002';
update reactivation_email_preferences set enabled=false where profile_id::text like '%003';
update profiles set date_of_birth=current_date-interval '12 years' where id::text like '%004';
update profiles set access_status='pending' where id::text like '%005';
insert into product_activity select 'recent',id,now() from profiles where id::text like '%006';
do $$ begin
 if (select count(*) from reactivation_email_candidates()) <> 1 then raise exception 'eligibility/consent/inactivity'; end if;
 if (select count(*) from claim_reactivation_email('00000000-0000-4000-8000-000000000001')) <> 1 then raise exception 'claim'; end if;
 if (select count(*) from claim_reactivation_email('00000000-0000-4000-8000-000000000001')) <> 0 then raise exception 'concurrent claim'; end if;
end $$;
-- Simulate a restart after the lease expires. No network requests occur.
update reactivation_email_deliveries set last_attempt_at=now()-interval '2 minutes',next_attempt_at=now()-interval '1 minute';
do $$ begin
 if (select attempts from claim_reactivation_email('00000000-0000-4000-8000-000000000001')) <> 2 then raise exception 'restart retry'; end if;
end $$;
update reactivation_email_deliveries set claimed_at=now()-interval '24 hours',next_attempt_at=now()-interval '1 minute';
do $$ begin
 if exists(select 1 from reactivation_email_candidates()) then raise exception 'expired retry unsafe'; end if;
end $$;
update reactivation_email_deliveries set status='sent',sent_at=now()-interval '1 hour',delivered_at=now(),clicked_at=now();
insert into athlete_daily_activity values('00000000-0000-4000-8000-000000000001',current_date);
do $$ begin
 if exists(select 1 from reactivation_email_candidates()) then raise exception 'duplicate reminder'; end if;
 if (select count(*) from test_events) <> 3 then raise exception 'lifecycle analytics'; end if;
 if has_function_privilege('authenticated','claim_reactivation_email(uuid)','execute') then raise exception 'public claim'; end if;
 if has_table_privilege('authenticated','reactivation_email_deliveries','select') then raise exception 'private deliveries'; end if;
 if exists(select 1 from pg_class where relname in ('reactivation_email_deliveries','reactivation_email_preferences') and not relrowsecurity) then raise exception 'RLS disabled'; end if;
end $$;
