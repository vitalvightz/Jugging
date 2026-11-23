# Log Training

Implements the first delivery from investigation PR #2816. Plan offers a single Log Training action row opening a modal
form: activity, whole minutes (1–600, matching the existing session-duration
boundary), and reported Easy / Moderate / Hard effort. Saving requires neither
an active plan nor a prescribed session. The existing History page shows completed Unlxck sessions and manual entries,
with edits/deletion only for manual work. Plan shows a brief save confirmation,
with no session feed. Manual edit/delete controls are a small pencil and adjacent
Delete action. Those idempotent API operations retry transient gateway/network
failures using the existing bounded retry helper; permanent errors retain the
entry and leave the form/action available for retry.
An optional training-date disclosure allows logging yesterday or earlier work;
the default form still shows only the three required fields.
The existing History session tab reads the same service, retaining prescribed
RPE, pain, notes, reasons and skipped records; skipped work is never adaptive load.

## Storage and history

Apply `20261010184612_add_external_training_sessions.sql` before releasing the API
and frontend. It adds one table with an athlete FK, owner SELECT policy, validated
fields, and an athlete-scoped unique request key. Authenticated browser clients
cannot write directly; the API's existing profile/consent gates authorize writes,
and service-role queries explicitly scope every read/update/delete to the actor.
Reading prior history and deleting entries remain available after consent withdrawal.
The runtime schema checker includes the table, RLS and indexes.

When no date is supplied, the server assigns the athlete-local training day using
the existing 03:00 rollover, including DST handling. An explicit ISO date may
select that day or earlier; future training days are rejected against the same
local boundary. Edits cannot change its recorded date or owner. Atomic
conflict-ignore insertion plus an immutable original-request fingerprint makes
retries safe across date changes and subsequent edits. Deleted rows retain a
tombstone, so a late retry cannot recreate them. There is no plan FK: deleting or
regenerating a plan does not delete manual history. Account deletion still cascades.
Omitting the date retains the original three-field request fingerprint, preserving
retries from earlier clients. Explicit dates are part of the retry identity.
Backdating uses the existing date column and requires no additional migration.

`training_history` merges terminal `session_completions` with live
manual entries. Exercise logs and sparring detail logs are detail records, never
additional session-level workload. Planned, started and skipped work are excluded.
The History tab explicitly requests skipped records for display only.
Manual names/date matches are distinct occurrences; no guessed deduplication
across separately reported workouts. The API returns the most recent 30 rows
(up to 200 when requested). Existing prescribed-completion contracts are unchanged.

## Both adaptive paths

- **Today:** the existing readiness context reads the shared history; its existing
  repeated-hard-work rules accept validated qualitative Hard as demanding work.
  The original prescribed RPE/pain rules remain unchanged. Today re-evaluates the
  current check-in while recent manual work exists (including deletion tombstones),
  without rewriting it, so an earlier stored decision
  cannot hide work logged later or survive deletion of that work. Mutations invalidate
  the existing Today cache; failed history reads use its established fail-safe.
- **Generation:** the existing authenticated generation annotation loads the same
  shared history. The conditioning step calls the same pure Today decision engine.
  With repeated hard work plus poor readiness recorded for the current training
  day, it uses the existing moderate-fatigue conditioning selection penalties in
  the first active phase. It never lowers an existing higher fatigue setting.
  Later phases, phase lengths, strength, saved plans and coach sessions are unchanged.

Both paths retain actual total minutes, Hard minutes, effort and activity for the
existing recent-session window (most recent three completed sessions, five local
days). Hard never becomes RPE 8 or a head-contact measurement. This release does
not invent a duration-weighted fatigue score: the existing rule uses demanding
session counts, and durations remain explicit evidence. One 60-minute Hard
Sparring session with good readiness is considered without automatically cancelling
training. Generation does not infer current readiness from a stale check-in.

Manual logging is historical evidence only. It cannot award prescribed-session
XP, create rehab exposure evidence, progress stages, grant injury clearance or
change the existing contact-permission logic. Underlying conditioning injury and
restriction filters still apply.

## Verification

`tests/test_external_training.py` covers no-plan/rest-day/multiple-session logging,
retries, edits/deletion, local dates/DST, invalid fields, ownership/consent, plan
independence, cache invalidation, both adaptive inputs and decisions, stale readiness,
failed history, planned-work exclusion, and unchanged rehab/XP evidence. Store
query tests verify privileged-client scoping. Existing prescribed completion,
Today/readiness, generation, rehab and XP tests provide regression coverage.
Backdated 60-minute Hard Sparring reaches both adaptive paths on its reported day;
future/invalid dates, local rollover, retry conflicts and legacy fingerprints are tested.

`web/e2e/log-training.spec.ts` exercises the actual Plan UI and API adapter at
390px and 1280px, with isolated API responses: log, navigation to History, reload, edit, delete, retry
identity, field count, accessibility and overflow. It uses no live athlete data.
The optional-date mobile flow verifies logging yesterday, reloading and editing
without moving the recorded training day.

The migration also runs on isolated PostgreSQL via PGlite:

```powershell
# Install only in a temporary QA directory, not in application dependencies.
npm install --prefix <temporary-directory> @electric-sql/pglite@0.5.8
$env:PGLITE_ROOT = '<temporary-directory>'
node tests/sql/external_training_sessions.test.mjs
```

This verifies actual SQL constraints, RLS, forbidden browser writes, concurrent
request keys, plan independence and edit/delete retry safety. Production migration
application is complete on `unlxckpdf`, with live catalog verification of constraints,
RLS, grants and migration history. Live API/PostgREST verification remains a
deployment follow-up. There are no recurring sessions, custom plans or library changes.

## Quick Log — Repeat Previous Training

Today includes a compact logging action below its main session/check-in controls,
including when there is no active plan. When the existing history response contains
an eligible manual session, it also offers **Repeat last session** with its activity,
minutes and a chevron. Historical effort appears only inside the review dialog.
A discreet Log Training link beside the Today heading jumps to the logging rows,
so long session details do not require manual scrolling to find them. The shortcut searches the most recent
30 history entries; older manual entries can be repeated from History (up to the
existing 200-row limit). A failed history read leaves the simple logging action
available. Plan's existing tile remains unchanged.

Repeat opens the same Log Training dialog, copies activity and duration only,
and focuses an empty Effort field. The athlete selects today's effort and saves:
open repeat → choose effort → save (four taps with the current picker), compared
with also choosing activity and entering minutes in the blank three-field flow.
Activity and minutes remain editable; new sessions keep blank defaults. The
optional date starts empty, so the server assigns the athlete's current training
day with its existing timezone/03:00 rollover, even for a backdated source.

Opening or cancelling never writes. Each new repeat obtains a fresh request key
at submission; an unchanged failed submission keeps its key for retry. A synchronous
submission guard also blocks duplicate submits before React updates the UI.
History inserts the returned occurrence by source/id without replacing the source;
Today refreshes through `useTodayCommand.refresh` after saving. Existing API
mutation middleware invalidates readiness caches. No prescribed completion,
XP-award or rehab-evidence mutation is called. A 401 retains the open form and
reports that sign-in is required; token changes follow the existing form reset.

Regression coverage includes no history/no active plan, repeat from Today and
History, changed effort, two sessions in a day, unchanged backdated source,
fresh repeat keys, lost HTTP/network responses, double submit, session expiry,
keyboard focus/selection, accessible descriptions and axe checks at 390px.

Beta follow-up: measure median repeat-log time from tapping Repeat to successful
save, including effort selection and service latency. The target is under 10 seconds;
interaction reduction alone does not establish that timing result.

## Progressive training habits

Today optionally learns from the existing **manual-only** history API (up to 200
rows), then considers the last 42 athlete-local training days. No models, training
jobs, dependencies, programme imports or new logging system are introduced.

- One recent observation on today's weekday yields **Suggested today**.
- Two distinct, consecutive weekday dates offer **Boxing every Monday?**
- **Yes, usually** saves an editable routine. **Not always** remembers a refusal.
- Confirmed routines produce **Your usual Monday**. They never create sessions.
- Two later changed weekdays can offer **Switch Monday to strength?** Accepting
  dismisses the previous routine and confirms the replacement in one write.
- If both activities were logged on those days, it offers an additional routine;
  it does not assume a second daily session replaced the first.

Each activity/date counts once, regardless of duplicate entries or retries.
Unconfirmed candidates rank by recency-weighted weekday frequency (14-day
half-life), then latest matching date, then activity as a stable tie-break. One
newer session does not automatically outrank a consistent recent pattern; repeated
recent observations can overtake it. Explicit confirmations and today's manual
corrections retain priority. Tentative evidence expires after 14 days without
another matching weekday. Duration is the median of per-day medians from the
latest three matching weekdays. A confirmed duration stays athlete-controlled.
These are product heuristics, **not calibrated confidence levels**. Even strong
repetition needs confirmation before it becomes a saved routine.

Today's actual manual corrections suppress guesses for that day. If a matching
confirmed routine is logged, another explicitly confirmed activity can still be
suggested; an activity already logged is never suggested again that day. Confirmed
coach/contact fields outrank inference. Readiness holds, modified/pull-back days,
active injuries and withdrawn health consent suppress learned prompts. Sparring
inference requires the live Today decision to be green and the existing clinician
clearance level to be train_contact. This only permits a logging suggestion; it
never starts contact or changes clearance. Existing retrospective manual log/repeat actions
remain available. Habit settings never change contact clearance, prescriptions,
rehab evidence, session completion or prescribed-session XP.

The current form reviews every suggested session and requires today's effort.
**Training habits** opens inline controls to pause suggestions, edit activity or
minutes, remove routines, or allow a declined activity to be suggested again.
Refusals persist until reset. Multiple activities can be confirmed on one weekday;
only one suggestion/prompt is shown at a time to keep Today compact. No start
times are inferred. Existing Plan and History entry points remain unchanged.

Preferences live in the existing profile (`training_habits` and a revision), not
in training history. Apply `20261018110229_add_training_habit_preferences.sql`
before deploying the API. Existing profile ownership/RLS remains in force; the
API validates a maximum of 42 unique weekday/activity decisions and performs an
athlete-scoped compare-and-swap to reject stale-device writes. Lost-response
retries recover by re-reading the saved intention. A failed save never pretends
a routine was confirmed. Older APIs without these profile fields retain simple
logging. Failed history reads and truncated 200-row responses pause new inference
and pattern prompts. Safe confirmed shortcuts remain available from profile
preferences, without a historical effort hint. Known logs from this view still
prevent suggesting an already logged activity or overriding a correction.
Confirmation records add no workload.

Verification includes deterministic learning tests, profile/consent/ownership and
revision tests, isolated SQL migration/RLS checks, API contract generation, and
mobile browser flows for tentative suggestions, one-tap confirmation, reload,
editing, opt-out, declines, replacement, failed saves and lost-response recovery.
Physical iPhone/Safari and real beta behaviour remain deployment follow-ups.
