# Rehab exercise experience

Today keeps the shared rehabilitation engine authoritative. This change builds
on Main `197d0780` (merged PR #2977). It changes presentation only: no bank
activation, dose, permission, progression threshold, completion API, RLS or
environment changes.

## Athlete experience

- The injury summary separates the current stage from future availability.
  Chest RESTORE explicitly says: “LOAD exercises aren’t available for this
  injury in UNLXCK yet.” Unpublished stages offer no logging or permission
  instructions that could imply an unlock.
- Published stages show the actual blocker and next action immediately.
  Worsening/medical restrictions take priority. Detailed requirements remain
  available through the existing sheet.
- The existing exercise row, prescription display and demonstration player show
  approved rehab. Equipment comes from the frozen bank snapshot; range,
  resistance and restrictions come from the approved live/frozen prescription.
  Missing numerical doses stay unnumbered. Instructions remain intact.
  Approved exercise limits stay verbatim in a list, without recasting dose or
  range restrictions as “Stop if” symptom triggers.
- Rehab demonstrations and instructions remain accessible during a started
  session. Missing/invalid media leaves the exercise usable; embedding failure
  retains the shared player's YouTube link and instructions.
- Existing Completed / Modified / Skipped and Better / Same / Worse actions
  remain unchanged. Completion records work, not a favourable injury response
  or clinical clearance.

## Add demonstrations

Follow [the existing reviewed video import workflow](rehab-video-curation.md#attach-a-reviewed-url-to-an-existing-bank-exercise).
Use the exact rehab bank identity, with watch, youtu.be or Shorts URLs in the
existing CSV. No reviewed URL is populated by this PR.

## Regression coverage

`tests/test_rehab_experience.py` calls the real FastAPI Today and completion
routes with synthetic athletes and the existing FakeStore. It checks chest
unavailability; all four published LOAD pilots' exact identity, dose,
instructions and restrictions; missing permission; pending next-day response;
worsening; valid/missing/invalid media; unchanged prescription revisions and
the existing exposure/response hold after completion. Broader permission,
multi-injury, response, progression and media regressions are run separately.

UI tests consume exported Today API fixtures and exercise the shared sheets,
including all four LOAD pilots and self-paced chest instructions. The browser
suite opens the real `/today` route with those synthetic API payloads at 320,
375 and 1280 px, checks disclosure/close interactions, video and error fallback,
and verifies the existing one-tap completion payload and resulting UI state.
YouTube's IFrame API is simulated with a test-only video ID; no unrelated real
demonstration is loaded or published. Production authentication, live database
writes and external YouTube playback are outside this local verification.

```powershell
python -m pytest tests/test_rehab_experience.py -q
# Refresh synthetic browser fixtures only when the consumer contract changes:
python -m tests.test_rehab_experience web/e2e/fixtures/rehab-today.json

cd web
npm run test:unit
npm run typecheck
npm run lint
# CI-safe placeholders for local build/browser tests (no production services):
$env:NEXT_PUBLIC_SUPABASE_URL = 'https://stub.supabase.co'
$env:NEXT_PUBLIC_SUPABASE_ANON_KEY = 'stub-anon-key'
$env:NEXT_PUBLIC_API_BASE_URL = 'http://127.0.0.1:8000'
npm run build
npx playwright test e2e/rehab-experience.spec.ts --workers=1
```

Set `REHAB_QA_DIR` to a local directory to retain browser screenshots.
Browser plugin unavailable; verification uses the repository's Playwright setup.
