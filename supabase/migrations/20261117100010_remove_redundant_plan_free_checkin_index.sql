-- The athlete/day unique index introduced by one_daily_checkin_per_athlete
-- covers both planned and plan-free check-ins and is the upsert RPC's arbiter.
-- Keeping the older partial unique index can raise a competing uniqueness
-- violation during concurrent plan-free inserts before ON CONFLICT resolves it.
drop index if exists public.today_checkins_athlete_plan_free_day_key;
