-- Run only in a disposable analytics_ database after all analytics migrations.
begin;
do $$
declare actor uuid=gen_random_uuid(); excluded uuid=gen_random_uuid(); req uuid=gen_random_uuid(); payload jsonb;
begin
 if current_database() !~ '^analytics_' then raise exception 'Disposable database required'; end if;
 insert into public.profiles(id,role,email,created_at) values(actor,'athlete','beta@example.test',now()),(excluded,'athlete','excluded@example.test',now());
 update public.profiles set analytics_excluded=true where id=excluded;
 if public.ingest_product_event(actor,'app_opened','onboarding',req,null) is not true then raise exception 'New beta account blocked'; end if;
 perform public.ingest_product_event(actor,'app_opened','onboarding',req,null);
 if (select count(*) from public.product_events where athlete_id=actor)<>1 then raise exception 'Retry duplicated'; end if;
 insert into public.product_analytics_preferences(athlete_id,enabled,consent_version,updated_at) values(actor,false,'product-analytics-v1',now());
 if public.ingest_product_event(actor,'page_viewed','plans',gen_random_uuid(),null) is not true then raise exception 'Legacy preference still gates beta'; end if;
 if public.ingest_product_event(excluded,'app_opened','onboarding',gen_random_uuid(),null) is not false then raise exception 'Exclusion bypass'; end if;
 payload=public.product_analytics_facts('{}',actor);
 if (payload#>>'{tracking_health,healthy}')::boolean is distinct from true then raise exception 'Collector unhealthy'; end if;
 if payload->>'tracking_since' is null then raise exception 'Missing tracking start'; end if;
 if jsonb_array_length(payload->'optional_coverage') is distinct from 1 then raise exception 'Beta coverage missing'; end if;
 if exists(select 1 from public.product_activity where athlete_id=actor and meaningful) then raise exception 'Visit counted as training'; end if;
 if (select sum((x->>'count')::int) from jsonb_array_elements(payload->'facts') x) is distinct from 2 then raise exception 'Report loses delivered events'; end if;
 if has_function_privilege('authenticated','public.ingest_product_event(uuid,text,text,uuid,uuid)','execute') then raise exception 'Client ingestion privilege'; end if;
 perform cron.alter_job((select jobid from cron.job where jobname='product-events-retention'),active:=false);
 if public.ingest_product_event(actor,'timer_started','timer',gen_random_uuid(),null) is not false then raise exception 'Unhealthy retention accepted'; end if;
 if (public.product_analytics_facts('{}',actor)#>>'{tracking_health,healthy}')::boolean is distinct from false then raise exception 'Missing health warning'; end if;
end $$;
rollback;
