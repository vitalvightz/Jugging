-- Reminder activation funnel: offer shown -> enable tapped -> subscription
-- saved. Delivery is already recorded in notification_deliveries. Events carry
-- no payload; none is a meaningful action, so activity figures are unchanged.
alter table public.product_events drop constraint product_events_event_name_check;
alter table public.product_events add constraint product_events_event_name_check check (event_name in (
 'plan_opened','plan_editor_opened','plan_edited','plan_replaced',
 'timer_started','timer_round_completed','timer_completed','timer_exited',
 'history_opened','nutrition_opened','nutrition_saved','feedback_prompt_shown',
 'checkin_started','sparring_log_started',
 'push_prompt_shown','push_prompt_accepted','push_subscribed',
 'app_opened','page_viewed','operation_failed','frontend_error','reactivation_email_delivered','reactivation_email_clicked','reactivation_email_returned'));

-- The Today row re-offers on each visit; one shown event per athlete per UTC day.
drop index public.product_event_discovery_daily;
create unique index product_event_discovery_daily on public.product_events
(athlete_id,event_name,feature,(coalesce(flow_id,'00000000-0000-0000-0000-000000000000'::uuid)),((created_at at time zone 'UTC')::date))
where event_name in ('plan_opened','plan_editor_opened','history_opened','nutrition_opened','nutrition_saved','plan_edited','plan_replaced',
 'checkin_started','sparring_log_started','push_prompt_shown');
