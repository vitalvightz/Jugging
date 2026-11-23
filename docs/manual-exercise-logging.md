# Optional exercise logging for manual training: audit and storage model

Athletes following their own coach's programme log a manual session (activity,
duration, effort). This feature lets them optionally record the movements they
did on that session, without creating an UNLXCK programme.

## Audit

- `external_training_sessions` is the occurrence: athlete-owned, no plan FK,
  retry key `(athlete_id, client_request_id)`, soft-deleted tombstones. Its
  activity/duration/effort already feed readiness, completed workload and
  sparring readiness (`training_history`, `completed_workload`). Nothing else
  should count the same workout again.
- `exercise_logs` is keyed by `(athlete_id, plan_id, training_day, block_id)`;
  `block_id` is a server-owned prescription identity and `status`
  (`as_prescribed` / `modified` / `skipped`) only means something against a
  frozen prescription. Today pre-fill (`recent_loads`) also reads it. Writing
  manual results there would need fake plan/block identities and would leak
  external work into prescribed-session behaviour. Not reused.
- The Exercise Library index (PR #2942) and progress (PR #2944) group by exact
  `exercise_key` (`key:`) or an unlinked log UUID (`log:`). Best loads keep kg
  and lb apart and never parse display strings.
- Session Details (PR #2940) shows manual sessions with no blocks.
- The training catalogue (`/api/training/library`) carries `exercise_key` in the
  same namespace as prescribed logs. `dose_guidance` already derives a dose
  mode from the catalogue's own evidence.
- Community exercises and private media are separate opt-in tables; nothing
  here touches them.

## Storage model (migration `20261019180404_add_manual_exercise_entries.sql`)

`personal_exercises` — an athlete's private movement names.
- Unique `(athlete_id, name_key)`; `name_key` folds case and whitespace only,
  so "Pause back squat" never merges with "Back squat". RLS: owner read only.

`manual_exercise_entries` — one recorded movement on one manual session.
- `external_session_id` → `external_training_sessions(id, athlete_id)`
  (composite FK keeps the link inside one athlete's records).
- Exactly one identity: catalogue `exercise_key` (validated server-side from a
  `catalogue_id`, or a key already in the athlete's records) or
  `personal_exercise_id` (composite FK to the same athlete).
- `display_name` snapshot, `measurement_format`
  (`strength`/`bodyweight`/`isometric`/`intervals`/`distance`), `actual`
  (exercise_logs' actual shape: only entered values), optional `notes`.
- Retry key `(athlete_id, client_entry_id)`; saves use
  `INSERT … ON CONFLICT DO NOTHING`, so a retried PUT never adds a second
  occurrence. Deletes are tombstones; a late retry gets 410.
- `revision` orders edits. Each PUT names the revision it was based on (0 to
  create). An edit is a compare-and-set on that revision and bumps it by one.
  A replay of a save that already applied (stored revision = base + 1, same
  values) returns the stored row; anything older is 409. So a delayed retry of
  "80 kg" can never overwrite a later "90 kg" correction. The trigger only lets
  revisions step forward by one.
- Triggers: an insert takes the parent's `training_day`, needs a live parent and
  fewer than 30 live siblings (a retry of an existing entry passes through); an
  update cannot change owner, session, retry key or day, and cannot undelete.
  Updating the parent's day or `deleted_at` updates its entries in the same
  statement. The API also enforces each rule, and the store tombstones children
  on session delete.
- RLS: owner reads live rows only; only the service role writes.

## API

- `GET /api/training/sessions/{id}/exercises` — the live session's entries.
- `PUT /api/training/sessions/{id}/exercises/{entry_id}` — idempotent create or
  update (health-data consent required).
- `DELETE …/exercises/{entry_id}` — tombstone; repeat deletes succeed.
- `/api/training/history` adds `exercise_count` to manual rows (one batched read).
- `/api/training/history/external/{id}` returns entries as `blocks` with
  `source: "manual"` and an empty `prescribed`; nothing is compared.
- `/api/training/library?for_log=true` adds `log_format` from catalogue evidence,
  or null when it is ambiguous (the athlete then picks a format).
- Exercise Library and progress read both sources. `key:` identities merge
  prescribed and manual results; `custom:<uuid>` is a private `personal` identity;
  `log:` legacy records stay separate. Pagination is one `(training_day, id)`
  keyset across both tables, using the same as-of cutoff. Name-only entries are
  "performed, no measurements recorded"; they never become a zero or a best.

## Boundaries

- No workload, readiness, XP, streak, rehab exposure, injury clearance or
  sparring contact consumer reads the new tables. Tests assert those outputs are
  unchanged after adding entries.
- Repeating a session can reuse its exercise names. Previous values are shown as
  "Last time …" reference text; inputs start empty.
- Notes are health-adjacent free text: hidden from reads while consent is off.

## UI

- Log Training is unchanged. After a non-sparring save, the existing takeaway
  card gains one quiet "Add exercises" row. Sparring keeps its own follow-up.
- Session Details lists recorded exercises ("Back Squat · 3 × 5 · 80 kg") with
  Add/Edit exercises. The button closes the details sheet before opening the
  logger and reopens it afterwards, so only one modal is open at a time.
- The logger is one bottom sheet: search, then recent/recorded movements first,
  then catalogue matches, then "Add “name”". Selecting a movement shows one row
  of inputs for its format; "More details" holds rest, RPE, extra measures and
  notes. Each exercise is saved on Add, so closing never loses a finished row;
  Done saves an exercise in progress; closing with one unsaved asks first.

## Limits

- Max 30 exercises per session. Strength/bodyweight entries can optionally hold
  up to 50 confirmed `actual.set_entries` rows, each with its own reps, load and optional RPE (whole or half steps).
  Common sets/reps/load/RPE remain compatibility summaries; varied values are never
  flattened into one weight. Existing aggregate logs remain valid.
- Catalogue formats are a suggestion from bank evidence; the athlete can change them.
- Renaming a personal exercise and merging a personal identity into a catalogue
  movement are not supported; both would need an explicit, reviewed action.
- The hosted JSON size constraint is verified at 8 KiB; see the applied migration below.

## Per-set logging

- The compact Sets / Reps / Load entry remains the default. “Edit sets
  individually” expands the shared row editor; an edit changes only that set.
  “Apply to remaining sets” copies only the edited field to later sets.
- Planned-session logging seeds each row from exact prescribed values. Ranges
  stay hints, and missing measurements stay unknown.
- The S&C timer shows prescription chips during work. “Set done” starts rest
  immediately and opens a non-modal sheet with load, reps and RPE. Confirming
  collapses it to one editable confirmation line; starting the next set hides
  it automatically. Editing and confirming preserve the countdown timestamps.
  While the logger is open, Confirm is the single prominent action; starting
  the next set and ending the session stay as quiet footer actions. Duplicate
  dose chips, next-exercise teasers and rest adjustment tools stay out of this
  logging view; the clock and header adjustment remain available.
- Saved timer runs retain drafts and confirmations. Unconfirmed measurements
  stay out of actual history. At session end, missed confirmations open a
  compact row review where athletes can correct and save their completed sets.
  The final set opens the same sheet at the exercise transition, while the
  next exercise is already queued. Starting the next exercise hides it without
  blocking work or reopening it during that exercise's prep/rest. The session's
  final set uses the same compact sheet inside the existing completion screen;
  older unconfirmed sets use review only if they still need attention.
- Session Details and exercise performance history use the shared, collapsed
  set summary and expand into compact read-only rows. Best-load records and load
  charts inspect the individual loads; a varied workout never becomes an
  invented sets × reps × maximum-load result.
- The rows retain their original `set_number` when only some sets were
  reported. `actual.sets` retains the completed count independently. Empty rows
  normalize to unreported work; history never manufactures measurements from
  them. Partial logs never supply a common reps/load/RPE for all completed sets.
- Review offers “Leave remaining unreported” and a per-row control. Confirmed
  measurements remain saved; unknown entries stay out of `set_entries`. A fully
  unreported exercise saves its completed count with modified status, preventing
  prescription values from becoming actual results or best loads.
- All per-set editors show optional RPE by default; the aggregate RPE control is
  hidden while editing individual sets to avoid conflicting values.
- Migration `20261023073352_allow_numbered_manual_set_results.sql` raises the
  manual-entry JSON bound from 4 KiB to 8 KiB, matching exercise logs. This keeps
  the supported 50 numbered sets with RPE within the storage limit. It has now
  been applied by the owner to hosted Supabase: the constraint was verified
  at 8192 bytes on 2026-10-23. Deploy the API before the frontend sends
  numbered results.
