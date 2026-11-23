-- Coach feedback reads must follow the same record-level injury-sharing rule as
-- save_feedback. Previously coach_own_feedback checked only the connection and
-- overall health consent, so a note written on a rehab/injury record while
-- injury sharing was on stayed readable after the athlete turned sharing off.
-- Sharing is re-evaluated on every read: turning it back on restores the notes.
create function coach_private.feedback_visible(p_athlete_id uuid, p_source text, p_target_id uuid, p_exercise_ref text)
returns boolean language sql stable security definer set search_path='' as $$
 select exists(select 1 from public.coach_connections c
 join public.profiles coach on coach.id=c.coach_id and coach.role in ('coach','admin') and coach.access_status='approved'
 join public.profiles athlete on athlete.id=c.athlete_id and athlete.role='athlete' and athlete.access_status='approved' and athlete.health_data_consent
 where c.coach_id=auth.uid() and c.athlete_id=p_athlete_id and c.status='accepted'
 and (
   c.share_injuries
   -- Sparring reports are never withheld as a whole; save_feedback does not gate them.
   or p_source='sparring'
   -- Same test save_feedback applies: a missing snapshot is treated as health work.
   or (p_source='unlxck' and exists(select 1 from public.session_completions s
       where s.id=p_target_id and s.athlete_id=p_athlete_id and s.prescription_snapshot is not null
       and not coach_private.health_session(s.prescription_snapshot#>>'{session,session_type}',
         s.prescription_snapshot#>>'{session,title}',s.prescription_snapshot#>'{session,blocks}')))
   or (p_source='external' and (coalesce(p_exercise_ref,'')='' or not exists(select 1 from public.manual_exercise_entries e
       where e.id::text=p_exercise_ref and e.external_session_id=p_target_id and e.athlete_id=p_athlete_id
       and coach_private.health_session('',e.display_name,'[]'::jsonb))))
 ));
$$;
revoke all on function coach_private.feedback_visible(uuid,text,uuid,text) from public,anon;
grant execute on function coach_private.feedback_visible(uuid,text,uuid,text) to authenticated;

drop policy coach_own_feedback on public.coach_feedback;
create policy coach_own_feedback on public.coach_feedback for select to authenticated using(
 coach_id=(select auth.uid()) and coach_private.feedback_visible(athlete_id,source,target_id,exercise_ref));

-- Serves the newest-note read (order by updated_at desc, id desc limit 1).
create index coach_feedback_latest on public.coach_feedback(athlete_id,updated_at desc,id desc);
