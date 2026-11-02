# Calendar-integrity characterization baselines

Frozen **semantic** planner projections used by
`tests/test_calendar_integrity_characterization.py` (Stage 3A).

> **Refrozen after Stage 3B and later planner work.** These were first captured
> from the pre-Stage-3B planner. The planner has since changed on purpose (the
> calendar-integrity governor, the Fight Visualisation protocol, weight-cut
> handling, tail composition), so the files were regenerated from the current
> planner with the clock pinned (see `tests/planner_clock.py`). They now freeze
> today's behaviour; the "pre-governor" remarks below describe their origin.

Each JSON file is the expected `_semantic_projection(...)` of one representative
scenario, captured from the **pre-Stage-3B** deterministic planner (Main /
PR #2396's base). The test asserts the live projection equals the baseline, so
Stage 3B's calendar-integrity governor can only change a calendar that violates
the shared `combat_load_policy` seam — every other row here must be preserved,
and any drift shows up as an exact diff against these files.

These are deliberately **not** full Stage 2 payload dumps. Exercise names,
athlete-facing copy, HTML, render formatting, and LLM wording are all excluded.

## Row schema (positional)

Rows are positional arrays, sorted for stability:

- **`roles[]`** — `[week_index, phase, role_key, category, weekday, d_day,
  late_fight_tail_owned, late_camp_role_morph, late_camp_strength_morph,
  stress_class, cost_class]`
- **`contacts[]`** — `[week_index, weekday, status, effective_load]`
- **`suppressed[]`** — `[week_index, role_key, category, replacement_role_key,
  compression_reason_codes]`

Plus the top-level `generator_mode`, `payload_variant`, and `tail_handoff`
(the D-14/D-13 handoff metadata).

## Scenarios

| File | Days out | Hard sparring | What it pins |
| --- | --- | --- | --- |
| `d24_no_contact.json` | 24 | none | Clean normal-camp role/day/D-day structure |
| `d24_one_hard.json` | 24 | Thursday | One resolved hard-contact day + finished tail |
| `d24_two_hard.json` | 24 | Tue, Fri | Two declared contacts (pre-governor baseline, defects included) |
| `d16_technical_conversion.json` | 16 | Tue, Fri | Declared contact converted away from effective hard |
| `d14_boundary.json` | 14 | Thursday | Normal planner still owns D-14 |
| `d13_direct.json` | 13 | Thursday | Direct D-13 uses the late-fight Stage 2 payload |
| `d28_finished_tail.json` | 28 | Thursday | Long camp; finished D-13→D-1 tail is immutable |
| `d24_high_fatigue.json` | 24 | Tue, Fri | High-fatigue suppression semantics |

`d24_two_hard.json` intentionally captures the pre-governor defect surface (a
lone effective-hard week-1 Friday, compressed week-1 strength, a duplicate
tail-spliced Friday). It is a baseline to diff against, **not** a statement of
desired behaviour.

## Regenerating

If Stage 3B legitimately changes one of these calendars, update the affected
baseline in the same PR and explain in review exactly which rows changed and
why the shared calendar policy required it.

## Physical-stimulus restoration review

The restoration PR refreezes these eight scenarios after removing appointment
trimming, declaration-based technical-day exclusions and duplicate primer
penalties. Each proposed contact projection was compared with both the old
snapshot and a fresh Main (`a76fd0ef`) run: **all contact rows are unchanged**.
Every scheduled normal physical role passes the shared calendar policy, and
every selected normal S&C member retains an effective prescription.

Physical differences against live Main include retained coordination/flush
appointments, strength at D-23/D-19/D-17 and touches at D-12, and primers moving
onto compatible converted technical dates. The direct/boundary cases retain
sharpness at D-5, freshness at D-3 and neural work at D-1. The high-fatigue case
still uses its existing fatigue and dose controls; no raw weekday exclusion or
post-allocation count ceiling is restored. See
[the replay report](../../../docs/restore-physical-stimulus.md) for actual
appointment totals and explicit over-target counts; snapshot role-row counts
are not appointment counts.

The old snapshots also differed from live Main's mental/support cadence before
this change. Those existing Main rows are reflected in the refreshed fixtures;
this PR changes no mental preparation implementation or coaching UI.

## Hard-sparring lock stacking review

The hard-sparring lock now keeps work the shared combat policy lets share a
converted technical-only day, instead of displacing it. Two baselines changed:

- `d24_one_hard.json`: the final taper calendar is identical (D-7..D-0). Only
  the suppression ledger differs. The neural primer is no longer displaced from
  converted Thursday, so `fight_week_freshness_day` is the spar-first-cap cut.
  The provisional flush that D-0 overrides is now `aerobic_flush_day`, not
  `converted_recovery_flush_day`: with the primer kept, the second pass places
  the converted flush on Wednesday. Both are low-load recovery conditioning
  placeholders, and neither renders; D-0 stays fight-day protocol only.
- `d24_high_fatigue.json`: GPP `primary_strength_day` and SPP
  `neural_plus_strength_day` are kept instead of displaced by the lock; GPP
  `aerobic_coordination_day` moves from Monday to Thursday. Contact rows are
  unchanged.
