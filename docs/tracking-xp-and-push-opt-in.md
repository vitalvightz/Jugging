# Training XP and notification opt-in

The first successful new manual log or done/modified prescribed session offers
**UNLXCK your next move.** on push-capable devices with default permission.
The native dialog blurs the background, traps focus and restores scroll/focus.
Its red CTA has two light sweeps; reduced motion disables them. Permission is
requested only on the Enable click using preloaded subscription prerequisites.
Session takeaway and sparring/safety dialogs finish first. Unsupported, blocked,
already subscribed, server-disabled and account-paused devices are excluded.
The offer is recorded once per athlete/browser device only after the native
dialog successfully opens. Navigation, a blocked open or another interruption
before that point preserves the next opportunity; overlay locks are released on
an unsuccessful open. Settings remains available.

## Rewards

| Action | XP | Scope |
| --- | ---: | --- |
| Manual training recorded | 25 | Once per training day; shares recording credit with prescribed training |
| First session recorded | 50 | Once per athlete, either source |
| First exercise result recorded | 25 | Once; positive saved measurements, including confirmed numeric prescriptions |
| 10 / 25 / 50 / 100 training days | 100 / 150 / 250 / 500 | Each once; distinct days, either source |
| Approved YouTube exercise contribution | 100 | Original creator; once per contribution and per video |
| First manual plan published | 100 | Existing `first_plan_ready`; shared with generated plans |

Prescribed `training_logged` and `planned_session_completed` earning paths and
historical awards remain. `manual_training_logged` is a separate source action
because `training_logged` already requires a prescribed completion in the
database. The manual daily credit is suppressed if prescribed recording XP was
already earned that day; prescribed recording credit is suppressed if the
manual reward landed first. The separate prescribed completion award remains.

Milestones count owned live manual occurrences and directly reported done or
modified prescribed occurrences. Future work, skipped sessions, sibling day
credit and unverified legacy completions do not count. The athlete's existing
03:00 training-day rollover is used. Multiple sessions on a day count as one
milestone day. Existing eligible history can establish a one-time milestone on
the next qualifying save; there is no daily-history backfill. Banked XP is never
removed by later edits/deletion, and idempotency prevents retry/delete farming.
Name-only exercises, rest-only measurements and zero-work entries do not earn.
Exercise recording still creates no workload, rehab exposure or contact clearance.

Community rewards require a currently published, consenting contribution with
verified and confirmed available YouTube media. SQL derives the creator, exercise
and canonical video identity. Private demos, pending/rejected/withdrawn entries
cannot earn. Reapproval/revisions cannot earn again for the same contribution;
duplicate videos (including alternate clips/accounts) cannot earn again.

New milestone/approval results produce one combined progress notification,
including any level-up from the same reconciliation. Copy uses **UNLXCKED**.
Existing account preferences, quiet hours, event caps, spacing and delivery
deduplication apply. Routine daily logs have no new milestone push. XP and push
failures never turn a successful product save or approval into a failed action.
Later qualifying saves/retries reconcile missing training awards. The existing
generation worker automatically retries missed approval XP every 60 seconds in
batches of at most 50 eligible contributions, independently of push settings.
Publication state is the durable replay source, so a process restart cannot
lose an approval. Backend-only discovery excludes already rewarded contributions
and canonical videos. Keyset pagination wraps after the scan and revisits failed
rows without letting one failure block later contributions. Each attempt uses
the existing authoritative reconciliation RPC, which checks current eligibility
and prevents duplicate credit across workers. Only newly awarded XP can notify.
Shutdown stops replay before its next contribution; generation claiming does
not wait for a replay batch. This requires the normal generation worker to run.
The backend-only per-revision RPC remains available for immediate manual repair.

## Rollout and verification

1. Verify existing migrations through `20261019180404_add_manual_exercise_entries.sql`
   are applied in the target database.
2. Review/apply `20261019195932_add_tracking_and_contribution_xp.sql` before backend
   rollout. Runtime schema requirements include both reconciliation RPCs, the
   `list_unrewarded_contribution_revisions` discovery RPC and the new source
   guard. Only `service_role` can execute them. No new browser grants, RLS
   assumptions or environment variables are introduced.
3. Deploy backend and frontend. Verify push server settings/VAPID already work;
   this change reuses them and does not provision delivery credentials.
4. Log a first manual session: 25 + 50 XP; add measured exercise actuals: 25 XP.
   Retry and log another same-day session: no additional manual daily reward.
   Publish a manual plan: the existing first-plan milestone pays once.
5. Approve a verified public YouTube contribution: its creator earns 100 XP.
   Reapproval and duplicate-video submissions must award zero. If the immediate
   XP call fails after approval, verify the running worker credits it on replay
   and subsequent replays leave the ledger and notification count unchanged.

Applied and verified on **unlxckpdf** (`leienvqynijrgghhzczt`) on 2026-10-19,
recorded as migration `20261019195932_add_tracking_and_contribution_xp`.
The three reconciliation/discovery RPCs are service-role only; the source guard,
deduplication indexes and XP readiness checks passed. Applying the schema did
not change existing ledger rows or account totals. The backend schema CI check
passed on rerun. Deploying PR2951's normal API/worker enables the new earning and
replay paths.

Use a disposable localhost PostgreSQL cluster for `tests/test_tracking_xp_postgres.py`;
never point the test DSN at a hosted database. Tests cover migration replay,
ownership, daily sharing, 03:00 eligibility, source provenance, measurement
requirements, thresholds, browser-role denial and concurrent retries.
Frontend checks cover mobile/desktop blur, focus, dismissal, subscription saving,
no overflow, accessibility and reduced motion. Real iPhone/PWA permission and
live push delivery still need a deployed-device check.
