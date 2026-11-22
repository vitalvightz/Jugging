# Five-pilot bank publication

Built on merged PR #2979 (`42ba894e`), retaining #2977's permission, response,
setback and multi-injury safeguards. No database changes or new assessments.

## Coverage shipped

**+** = newly available for this injury. Entries are alternatives unless marked
as a bundle; Today selects one routine, not every exercise in the row.

| Pilot | CALM | RESTORE | LOAD | DYNAMIC / RETURN |
| --- | --- | --- | --- | --- |
| Achilles tendonitis | Recovery support (1) | Seated heel movement (1) | Floor-level controlled lowering; **+ seated toe press against wall** (2) | Unsupported |
| Elbow tendonitis | Recovery support (1) | Gentle elbow movement; **+ supported dowel forearm rotation** (2) | Supported empty-hand wrist extension (1) | Unsupported |
| Ankle sprain | Gentle ankle movement (1) | Supported balance + heel lowering **atomic bundle**; **+ seated ankle circles as a standalone alternative** (3 exercises / 2 routines) | Seated bilateral heel raise (1) | Unsupported |
| Shoulder tendonitis | Recovery support (1) | Gentle external rotation; **+ supported comfortable wall slide** (2) | Close-to-wall push-up (1) | Unsupported |
| Groin strain | Recovery support (1) | Unresisted side-lying adduction; **+ gentle supine ball squeeze** (2) | Unsupported | Unsupported |

These are 5 additional injury/exercise options, using 5 existing bank identities.
Four identities were dormant; the wall slide was already published for shoulder
impingement and now explicitly supports tendonitis too. Total live bank identities
rise from 107 to 111. No higher stage is opened by this change.

New identities and exact boundaries:

- `achilles_unspecified_isometric_toe_presses_against_wall`: LOAD, wall,
  10 holds of 10 seconds, once daily, gentle seated effort. Replaces its dormant
  five-second pre-competition note with the sourced rehabilitation version.
  No step, heel drop, forceful stretch or extra resistance. The existing
  midportion applicability and loading-permission requirements still apply.
- `elbow_unspecified_pronation_supination_with_dowel`: RESTORE, lightweight
  centre-held dowel supported by the other hand; small comfortable rotation,
  relaxed grip, self-paced. Removes the dormant automatic band progression.
  No lever loading, weighted implement, forced twist or repetition target.
- `ankle_unspecified_ankle_cars_controlled_articular_rotations`: RESTORE,
  chair-seated small circles, self-paced; removes the unsupported requirement
  to achieve full range. No band, standing balance or forced movement.
- `shoulder_impingement_scapular_wall_slides_with_chin_tuck`: RESTORE, wall and
  cloth; reuses its completed review and exact existing movement instructions.
  Comfortable movement no higher than shoulder level; no extra resistance.
- `groin_unspecified_isometric_adductor_squeeze_ball_between_knees`: RESTORE,
  soft ball, gentle supine squeeze, self-paced with holds up to five seconds.
  Replaces the dormant thirty-second/pulsing/unstable-surface progression.
  No bridge, maximal effort, fatigue target or automatic increase.

The two existing ankle standing drills now declare the stable support already
required by their instructions. With support available, Today keeps the complete
standing bundle. With a chair but no stable support, it selects seated circles;
it never presents an incomplete standing bundle. Elbow/dowel, shoulder/cloth and
groin/ball options are preferred when those exact props are reported, with their
existing unweighted movements retained as fallbacks. Achilles wall work is an
alternative when standing support is unavailable. Props are recorded through the
existing equipment picker; no daily equipment check or additional form.

## One publication contract

`rehab_pathways.json.bank_publication_profiles` scopes the rollout to these five
injuries. `fightcamp/rehab_publication.py` turns complete, compatible bank content
into the **existing** `ClinicalPrescription` consumed by generation and Today.
There is no Python exercise-ID list, per-exercise publication flag, invented
clinician record, or second progression engine.

Bank records use their existing identity, notes, stage, mechanics, equipment,
severity, dose and stop fields. `compatible_injury_types` explicitly states
indications; an `unspecified` group alone grants none. The `prescription` object
reuses scheduling, source and loading-limit fields from `ClinicalPrescription`.
It cannot override identity, instructions, dose, severity or stop rules.

- `dose: null` remains unknown and cannot publish. `dose: {}` explicitly means
  self-paced instructions, with no invented numeric target. Numeric prescriptions
  use the existing sets/reps/duration fields.
- Unknown demand, absent equipment classification, missing instructions/sources,
  missing stop rules or ambiguous IDs fail closed. The validator prints the gap.
- LOAD also requires an exact bounded dose, range, resistance and restrictions.
  Content joins only an already supported stage. Classification never opens a
  transition or supplies evidence of strength, impact tolerance or sport readiness.
- Content hashes are computed automatically for snapshot integrity. Existing
  sourced prescriptions remain readable without re-entering advice or creating
  an approval record. Started/completed work retains its frozen content.

The mechanical review ledger records content provenance; it is not a clinical
sign-off and is not a new runtime publication gate. The historical fixture under
`tests/fixtures/rehab_pilot_publication_history.json` only reconstructs dated
migration tests by exact whole-record hashes. Runtime eligibility never reads it.

## Candidates deliberately left unavailable

| Candidate | Specific unresolved information |
| --- | --- |
| Achilles step/weighted variants | Safe step depth or external resistance, usable dose and suitability for the actual tendon presentation; insertional disease is not covered by the midportion pilot. |
| Elbow eccentric reverse wrist curls | Reviewed mechanics specify an individually selected dumbbell and range, but supply neither an executable resistance selection nor starting dose. Empty-hand permission cannot be reused as dumbbell clearance. |
| Ankle banded circles | No executable starting dose or reproducible safe band resistance. |
| Ankle foam-pad single-leg balance | No starting dose or evidence that the athlete can tolerate standing single-leg balance on an unstable surface. Seated heel-raise permission establishes neither. |
| Shoulder banded scaption holds | Individually selected band resistance and arm height remain unknown; no starting dose. Wall work does not establish those tolerances. |
| Groin standing cable adduction | No bounded initial cable resistance/dose or supported acute-adductor entry rule. Groin LOAD remains unavailable. |
| Every pilot's DYNAMIC / RETURN content | No published, executable exercise plus source-backed injury-specific strength/impact/functional criteria. Better/Same or additional completion logs cannot replace them. |

## Sources and prescription limits

The new content uses [NHS Achilles exercise guidance](https://www.dynamichealth.nhs.uk/help-and-advice/foot-or-ankle-pain/ankle-tendon-pain/),
[Newcastle tennis-elbow guidance](https://www.newcastle-hospitals.nhs.uk/services/newcastle-occupational-health-service/information-for-staff/physiotherapy/self-help-leaflets/tennis-elbow/),
[CUH elbow movement guidance](https://www.cuh.nhs.uk/patient-information/elbow-range-of-movement-exercises/),
[NHS ankle-sprain guidance](https://msk-bexley.nhs.uk/conditions/foot-and-ankle-pain/ankle-sprain),
the wall slide's existing [RJAH cuff guidance](https://www.rjah.nhs.uk/our-services/therapy/supported-self-care/rotator-cuff-related-shoulder-pain/),
[Berkshire groin-strain guidance](https://www.berkshirehealthcare.nhs.uk/advice/groin-strain),
and the existing [adductor rehabilitation review](https://pmc.ncbi.nlm.nih.gov/articles/PMC10569248/).
The dowel remains supported rather than being converted into strengthening;
groin work uses conservative self-paced, low-effort guidance instead of inventing
a universal repetition prescription. Low/moderate eligibility, source-matched
mechanics and conservative symptom stops are content limits, not validated
universal stage thresholds. Existing individual restrictions take precedence.

## Demonstrations and verification

Use the exact canonical keys in [rehab-video-curation.csv](rehab-video-curation.csv)
and the existing [YouTube import workflow](rehab-video-curation.md). The four newly
published identities have blank URL rows; the wall slide reuses its existing row.
No unrelated video is attached. Media never changes prescription or eligibility.

## Rehab exercise guide (with or without a demonstration)

Every banked rehab block (one with a `drill_snapshot`) uses one exercise-guide
layout. With a valid curated, private or community demonstration, the plan's
shared player (the same one used by generated-plan exercise rows) sits at the
top of the guide; an embed that errors at runtime still shows "Video unavailable
here. Use the cues below." with the YouTube link. Without one, the guide is
text-first and never shows "Demo not assigned". Legacy prehab/training blocks
are unchanged.

The guide (`web/lib/rehab-exercise-guide.ts`, `RehabExerciseGuide`) reuses the
generated-plan exercise card's styling (stat tiles, red-bar cue panel, closing
label lines with a red stop label) so rehab and S&C exercises look alike. Below
any demonstration it renders, in this order:

1. **Prescription**: hold/duration, reps, sets and frequency as the plan's stat
   tiles, taken only from the approved `dose` (or legacy block fields when there
   is no `dose`). An explicit empty dose shows a **Self-paced** summary line and
   never gains a number from bank metadata or prose.
2. **How to perform**: numbered steps in the cue panel. These are the approved instructions split
   at sentence boundaries, with no rewording, generated text or dose parsing.
3. **Requirements**: equipment, resistance and range as label lines.
4. **Safety limits**: stop rules, mandatory restrictions and safety sentences
   taken out of the instructions, in one list under a red label. Only exact equivalents are
   merged (for example, two "follow clinician restrictions" lines). Red flags,
   escalation, range, resistance, side, timing and conditional restrictions stay
   verbatim.

The existing exercise log panel follows the guide, and the Today completion
control is unchanged. The guide is display-only: it does not change eligibility,
permission gates, worsening/setback handling, multi-injury restrictions or the
completion payload.

`tests/test_rehab_bank_publication.py` exercises the real Today GET/completion
routes with synthetic persisted store data, including all five new options,
exact media identity, permission, pending responses, worsening and timed-dose
freeze checks. Existing pilot regressions cover multi-injury and subtype holds.
`web/e2e/rehab-experience.spec.ts` (12 scenarios) uses exported API payloads at
320, 375, 668 and 1280px. It covers the existing shared player, the
runtime-error fallback, the text-first guide's order (prescription, steps,
safety) and reachable last safety limit, the absence of horizontal overflow and
"Demo not assigned", and one completion action per pilot. The tests do not
contact production Supabase or YouTube. Set `REHAB_QA_DIR` to save screenshots. The generated full [reachability report](rehab-bank-reachability.md)
also probes each pilot routine's actual equipment, including alternatives.

```powershell
python tools/validate_rehab_clinical.py
python tools/validate_rehab_bank.py
python tools/validate_rehab_metadata_review.py
python tools/report_rehab_reachability.py --check
python -m pytest tests/test_rehab_bank_publication.py tests/test_rehab_permission_entry.py tests/test_rehab_experience.py
# Refresh the browser fixtures from real synthetic API responses:
python -m tests.test_rehab_bank_publication web/e2e/fixtures/rehab-bank-publication.json
cd web
npm run build
npm run test:e2e -- e2e/rehab-experience.spec.ts
```
