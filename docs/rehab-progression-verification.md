# Rehab progression verification

Baseline: merged #2980 on Main (`aac3c597`).

## Changes

- Show the CALM setback hold even when the engine supplies no next transition.
- Explain successful LOAD entry using the engine's recorded route.
- Say when clinician-permitted entry needs no prior RESTORE completion.
- Use short, plain text. Do not repeat the summary in expanded injury controls.
- Keep detailed checks in the existing sheet. Add no forms.
- Keep a held daily schedule separate from stage eligibility. A missed check-in
  shows “Today’s rehab on hold”; clinical setbacks still show a progression hold.

The client now types existing `loading_hold` and progression `reason_codes`
fields. No API payload, database, exercise bank, prescription or engine changes.
Family safety checks and injury-specific limits remain unchanged.

## Checks

- Backend: **945 passed, 4 failed** across Today, completion, publication,
  permission, family safety, progression and multi-injury tests.
- The same four failures reproduced on clean Main. All concern ankle scheduling
  in `test_live_clearance_relaxes_only_cleared_baseline_with_or_without_due_rehab`:
  mild/moderate severity, no completed rehab, training/contact scopes.
  The test expects `deferred`; Main returns `unsupported`. Left unchanged.
- Frontend: **2,157 unit tests passed** before the schedule-hold follow-up;
  all **123 affected component tests passed** after it.
  All **18 browser cases passed** at 320/375/668/1280px. Production build,
  typecheck, touched-file ESLint, Python import compilation and Ruff passed.
- The first unit run found an accessible-label regression. Restoring the existing
  label fixed it. A local dependency-link build error was fixed by installing
  the unchanged lockfile.

Run the targeted backend suite from the repo root:

```powershell
python -m pytest tests/test_rehab_progression_experience.py tests/test_rehab_experience.py tests/test_rehab_bank_publication.py tests/test_rehab_permission_entry.py tests/test_rehab_pathway_safety_invariants.py tests/test_rehab_pathway_equivalence.py tests/test_rehab_transition_engine.py tests/test_clinician_clearance_today.py -q
```

Browser fixtures come from authenticated Today test routes:

```powershell
python -m tests.test_rehab_progression_experience web/e2e/fixtures/rehab-progression.json
cd web
npm run test:unit
npm run build
npx playwright test e2e/rehab-experience.spec.ts --workers=2
```

Use the documented CI-safe public variables for the build. Set `REHAB_QA_DIR`
to an external absolute directory for screenshots.

## Limits

These checks use authenticated synthetic athletes, FakeStore and the built Today
app. No production test account was configured. No real athlete data was used.
Production athlete testing and broader CI remain unverified here.

Current pilot LOAD starters use reported clinician permission, even when prior
work exists. The normal progression explanation is covered by the existing
synthetic engine contract and UI tests. It is never inferred from missing
permission metadata or completed logs.

DYNAMIC and RETURN stay unsupported where they lack clinical criteria and
published exercises. Future clinical work must define each criterion, evidence
source, collection method and reviewed pass/fail rule. It is outside this PR.
