# Technical Footwork Bank — reclassification and follow-ups

## What changed

`footwork_conditioning_bank.json` was reclassified to
`technical_footwork_bank.json` and removed from the conditioning **scoring
pool**. Previously it was merged into `get_conditioning_bank()` (via a
monkey-patch loader) so its drills competed as ordinary aerobic conditioning
and could be selected as a primary energy-system dose. That merge is gone.

The bank is now consumed through a dedicated, relevance-gated path that mirrors
the coordination-goal guarantee:

- `conditioning.get_technical_footwork_bank()` — standalone cached loader.
- `conditioning.select_technical_footwork_candidates()` — gates on footwork
  relevance (goals/weaknesses), phase eligibility, per-drill `reactive_level`
  vs phase, `technical_complexity` in taper, equipment, and injury; ranks by
  sport + tactical function and returns **every** injury-safe candidate in rank
  order. `select_technical_footwork_drill()` is a thin wrapper returning the top
  pick.
- `_insert_technical_footwork_drill()` — a best-effort guarantee poststep that
  fills a leftover drill slot for footwork-focused athletes. It walks the ranked
  candidate list and inserts the first one that also clears the per-drill
  `late_windows` (taper/D-day) gate applied inside
  `_try_append_conditioning_drill`, so a window-blocked top pick no longer
  strands a valid, window-eligible drill (e.g. at D-4 a `d6_to_d5`-capped angle
  drill no longer suppresses a `d4_to_d2`-eligible stance reset). It is never
  scored against the energy-system pool; when it appears it carries the
  `technical_footwork_guarantee` reason code.

Technical footwork is placed under a dedicated `TECHNICAL_FOOTWORK_GROUP`
channel (see the render-channel note below), so it is gated exactly like an
aerobic drill but is **never counted, grouped, titled, or resolved as an
aerobic energy-system dose**.

Each drill carries meaningful per-drill metadata (phase eligibility,
`late_windows`, rpe, movement cost, `mech_*` tags) and a structured taxonomy
(`footwork_pattern`, `directionality`, `tactical_function`, `reactive_level`,
`braking_demand`, `elastic_demand`, `technical_complexity`,
`stance_transition`). Muay Thai/kickboxing and MMA locomotion were expanded
(teep retreat, check-and-return, switch-step, cage circle, level-change feint).

Its `mech_*` tags are reconciled to the **canonical injury vocabulary** so the
real injury guard gates them (no parallel tag set):

- `mech_change_of_direction` / `mech_deceleration` (already read by the knee,
  ankle and hamstring rules) express the cutting / hard-braking demands that
  were previously carried by the ad-hoc `mech_braking`, `mech_lateral_knee` and
  `mech_level_change` tokens.
- Two genuinely-new demands are wired into the rules and covered by regression
  tests: `mech_plantarflexion` (achilles, calf, ankle, foot — kick-recovery
  push-off) and `mech_hip_rotation` (hip — pivots / switch-steps).
- Descriptive-only tokens with no injury meaning (`mech_ground_transition`,
  `mech_single_leg`, `mech_ankle_stability`, `mech_trunk_stability`,
  `mech_lower_lateral`) were dropped from the bank; the movement information
  they carried lives in the taxonomy fields above.

## Deferred, on purpose

These were intentionally **not** done in this branch because the current
conditioning taxonomy/selector do not yet support them cleanly. Each is a
separate design change.

### 1. Footwork speed / agility layer (audit §7-B)

Short (3–5 s) explosive angle bursts, reactive mirror steps, cue-based
pivot-and-exit, 45°/90° COD bursts. These are alactic/neural, not technical
rehearsal, and belong in a speed-dose channel. The conditioning module already
has a `speed_dose_allowed` path and an alactic quota; the clean home is the
alactic/speed-dose selector, not this technical bank. Adding `speed`/`reactive`
tags to this bank is explicitly blocked by
`test_footwork_drills_do_not_default_to_speed_or_reactive_work`.

### 2. Footwork conditioning layer (audit §7-C)

10–30 s repeated ring-movement / change-of-direction efforts, pressure-footwork
density intervals, stance-preserving shuttle bouts. These are a genuine
physiological dose and belong in the main conditioning bank, but only once the
conditioning taxonomy has a first-class `modality: combat_footwork` (or similar)
that the selector and late-fight dosage policy understand. Until then, adding
them would re-create the "practising movement vs. developing conditioning"
confusion this reclassification removes. Recommended approach:

1. Add a `combat_footwork` modality + energy-system entries to
   `conditioning_bank.json` with real work:rest, lactate, and impact costs.
2. Extend `conditioning_boxing._is_*` modality helpers if bespoke dosing is
   needed.
3. Gate/boost them by the existing footwork goal signal in
   `stage2_planning_brief` / `stage2_payload`.

### 3. Visible render channel for technical footwork — DONE

Previously deferred; now implemented. Technical footwork is inserted under the
dedicated `TECHNICAL_FOOTWORK_GROUP` group key instead of `aerobic`. Because
`selected_counts` / `system_quota` / `missing_systems` all key off the three
real energy systems, this keeps footwork out of energy-system dose accounting
while it still occupies a visible plan slot. `render_conditioning_block` renders
it under its own **"Technical Footwork"** label (via
`athlete_facing_system_label`, keyed off `modality: technical_footwork`), and
`_conditioning_session_title` no longer lets it masquerade as an "Aerobic
support" / energy-system session. It therefore surfaces for footwork-focused
athletes without ever reading as a conditioning dose.

### 4. Experience-aware and cross-phase ranking — DONE

The selector previously ranked only on tactical function, style and sport, so
`technical_complexity` mattered only as a taper exclusion and the same top drill
won every phase. Two ranking nudges now apply inside the relevance-ranked list
(`_technical_footwork_rank_adjustments`); neither changes eligibility or dose:

- **Competitive maturity.** The pipeline derives it from intake status + record
  with the Stage 2 `_derive_competitive_maturity` and passes
  `competitive_maturity` in the conditioning flags (gap-fill reads it from the
  athlete model). `experienced_amateur` / `developing_pro` / `established_pro`
  get high +0.5, low −0.5; `novice_amateur` gets high −0.5, low +0.3.
  Unknown maturity and `early_pro` are unadjusted. The values sit below the
  typical gap between tactical-function matches, so an irrelevant drill never
  overtakes a relevant one just for being harder or easier.
- **Camp history.** Footwork placed in an earlier phase is passed forward as
  `prior_technical_footwork`. In GPP/SPP a repeat is down-ranked by 0.75 (still
  a fallback); in the taper it gets +0.5, so the taper rehearses a grooved drill
  instead of introducing a new one.

Both add reason codes (`technical_footwork_complexity_fit:<band>:<complexity>`,
`technical_footwork_repeat_deprioritised`, `technical_footwork_taper_familiar`).
### 5. Advanced boxing drills

Three boxing drills were added after checking each against the existing bank
for overlap:

| Drill | Complexity | Phases | Fills |
|---|---|---|---|
| Double Pivot Chain | high | GPP, SPP | Advanced progression of Step-Back Pivot / Check-Hook Pivot: chain rear- and lead-foot pivots to take a new attacking angle (counter / distance strikers). |
| Shift-Step Angle Creation | high | GPP, SPP | Boxing shift: no drill previously changed stance and lane mid-attack (pressure fighters / brawlers; `side_rule: alternate_stances`). |
| Foot Feint and Reposition | moderate | GPP, SPP, TAPER | Foot feints to draw a reaction before repositioning (counter / distance strikers). |

Their tactical functions are set so that athletes with unknown or novice
maturity keep their existing default picks; established pros reach Double Pivot
Chain (counter striker) and Shift-Step (pressure fighter) through the maturity
nudge. Foot Feint is a lower-ranked relevant candidate (fallback / gap-fill),
not a default. Outside-foot front shuffle, back shuffle + jam, and the slip-line
180° pivot were left out as overlapping existing drills or being head-movement
work rather than footwork; guard-manipulation shuffle is hand-fighting.

Demonstration clips are not attached yet: they still go through the normal
media review before any URL is added.

### 6. Weekly pattern variety when footwork is the primary weakness — DONE

Production showed a boxing distance striker (footwork primary weakness, no
status/record) whose open 4-week block placed Step-Back Pivot Reset on Monday
and again on Saturday every week. Two causes, both downstream of ranking:

- **Supply.** The single-insert guarantee put one drill per phase into the
  pool, and footwork slots carried no alternates (the reservoir held only the
  inserted drill). For footwork-focused athletes the conditioning reservoir
  step now carries the selector's other ranked drills as same-role alternates
  (`_TECHNICAL_FOOTWORK_RESERVOIR_LIMIT`), each passing the insert path's
  equipment, restriction, injury and late-window gates.
- **Allocation.** Strength composition offers every footwork record when
  footwork is the primary weakness, and each session independently forced the
  first one. `_rotate_footwork` now forces the best drill not yet placed that
  week, avoiding the one the capacity fill would add anyway, and prefers a
  drill's own slot over the same drill offered as another slot's alternate.

Footwork exposure count per session is unchanged, and the week-to-week pattern
is stable so each drill can progress across the block. On the production
intake the block now carries Lateral Exit + Foot Feint (Monday) and Step-Back
Pivot (Saturday) instead of Step-Back twice. Advanced drills are still gated by
maturity.

### 7. Primary footwork survives high-fatigue capacity — DONE

The footwork insert runs last and only fills a leftover place under
`visible_drill_cap`, and `trim_extras` cuts from the end. High fatigue removes
the speed-dose slot (cap 19 → 18 in GPP, 16 → 15 in SPP) and the energy-system
drills fill it, so the same intake with `fatigue_level: high` got no technical
footwork at all.

When footwork is the athlete's **primary** weakness or goal and the cap is
full, the insert now swaps one drill out instead of skipping
(`_displaceable_drill_index`): conditioning-support first, then an energy
system above quota, then one from a system that keeps at least one other drill.
Never a system's last drill, never another `*_guarantee` insert; lowest score
first, most demanding on a tie. The count is unchanged, a displaced drill is
restored if no footwork drill clears its gates, and a full cap with nothing
displaceable logs `technical_footwork_no_capacity`. A secondary footwork
focus stays best-effort. On the production intake at high fatigue this
displaces Sandbag Clean & Press (GPP) and 5K Steady Run (SPP), and the open
block carries Step-Back Pivot + Lateral Exit every week.

## Note on the late-camp golden snapshot

`tests/golden_snapshots/late_camp_selector_audit/after.json` was already stale
on the branch base (deterministic drift in both strength and conditioning,
predating this work — it was last regenerated at commit `63cec74`, before later
bank/selector changes). This branch does **not** regenerate it, to avoid
bundling unrelated strength drift into the footwork change. Removing footwork
from the conditioning pool does change that snapshot's conditioning candidate
lists; regenerate it in a dedicated pass with `tools/late_camp_selector_audit.py`.
