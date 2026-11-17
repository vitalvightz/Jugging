# Daily readiness migrations

Applied to `unlxckpdf` (`leienvqynijrgghhzczt`) on 2026-11-17 through the Supabase migration tool for PR #3299.

| Version | Migration |
| --- | --- |
| 20261117032526 | one_daily_checkin_per_athlete |
| 20261117032530 | share_daily_readiness_for_rehab |

Repository filenames match the versions assigned by Supabase. SQL content is unchanged from the tested migrations.

Live verification confirmed the unique `(athlete_id, training_day)` key and zero duplicate athlete-days. Deduplication retained the latest answers, removing 14 duplicate rows across seven athlete-days. The rehab trigger validates readiness using athlete, day, check-in ID and update timestamp without matching plan IDs. The plan-free RPC remains restricted to `service_role`. Security advisors showed the same pre-existing informational findings before and after application.

Application code deployment is separate; these migrations do not publish the frontend or backend changes from the PR.
