# S&C import verification — PR #3323

Checked 18 November 2026. Rebased onto Main `f98272bb3114e0367566460bb0e51564870ed22b`, including #3327–#3329.

## Live AI reading

The original supplied **Photo 2.jpg** was sent, without a transcription or predefined model response, through the configured `get_plan_reader()` and `import_plan()` service. This built an unsaved Custom Build draft; it did not publish a plan or write production data.

- Source JPEG SHA-256: `218d50c21c99e4b02a0efd0784d39d57b7684b9ba50da839759ba5afc4f54bc8`.
- Configured/returned model: `gpt-6-luna`.
- Successful response: `resp_062a9a3e46dbfc08016afe231506c087d0a0d2e8825c2f002a`, 20.75 seconds.
- The first actual response (`resp_050cad632c546b23016afe21743c0487d0a4bd9fd7dc5e3e7b`) produced seven exercises but omitted the RDL weight. The reader instructions were clarified to retain numeric WT cells even when units are absent. The repeat live call retained that weight and all other prescription cells.

| Source | Observed in successful response and draft |
| --- | --- |
| A1 Clean | Eight ordered stages: 50/60/70/75/80/85/90/95%; WT 120/140/165/175/190/200/210/225; final 3 × 1 |
| A2 Back Extension Iso | 3 × 25 seconds |
| B1 Back Squat | Six ordered stages: 50/60/70/75/80/88%; WT 170/200/235/250/270/295; final 7 × 1 |
| B2 Ankle PAIL&RAIL | 2 × 2 Ea, per-side retained |
| C1 S.A. DB High Incline Bench | Three separate 4e stages within one exercise |
| C2 Lat Pull Down | 3 × 4 |
| D1 Barbell RDL | 3 × 5, load “225”, no invented unit |
| Structure | Week 5, Day 1 (Total), seven exercises, all 21 prescription rows; no weekday or superset inferred |

Some single-number cells under “Sets x Reps” were retained verbatim in stage `source_text` (e.g. “Sets x Reps: 2; WT: 120”) instead of guessing a set count. Percentages and final sets/reps remain typed fields. No training cell was discarded. This is one successful real reading after a prompt correction, not a claim of perfect model accuracy on every attempt. The existing screenshot comparison warning remains visible.

The successful provider reading is committed as `tests/fixtures/plan_import/sc_week5.live-model.json`, with null/default metadata removed only. `test_actual_screenshot_model_response_replays_without_losing_training_cells` checks every loading cell through the rebased draft builder. The live calls occurred before #3329 landed during this work; its subsequent changes affect text-only second-look corrections, and the final regressions replay this actual image response after that rebase.

## Regression coverage

- Existing 21-row transcription and deterministic model fixture exercise import → scheduling choice → save → edit → reopen → structured publication.
- Existing Word/PDF/image/text browser journeys plus S&C journeys at 390px and 1280px cover editable stages, retained values, seven exercise cards, runtime errors and horizontal overflow.
- Main’s source corrections, tempo, exercise kind, flag links, references, calendar defaults, range prescriptions and second-look time budget are retained.
- New row-index targeting prevents a targeted second-look correction from rewriting other loading rows with the same exercise code/name.

Final command results are recorded in the PR description. The browser transport uses deterministic fixtures; the separate live check above uses the real AI provider. No browser fixture is presented as evidence of OCR accuracy.
