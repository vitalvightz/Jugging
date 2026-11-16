# Rehab streamlining implementation for integration

This draft packages the implementation from the working checkout for review. These are source snapshots, not a patch to apply wholesale. The checkout has no valid HEAD or readable baseline Git objects and has extensive missing files. Current Main has diverged substantially. Production code is deliberately not overwritten by these snapshots.

## Intended behavior

- General training readiness does not block prescribed Calm, Restore or Load rehab.
- Standalone Calm/Restore rehab logs Completed, Modified or Not done directly, without Start/Resume, effort/pain forms or programming review.
- One injury-response question saves on tap; the unasked reduction/stop field remains unknown.
- Next-day feedback remains a separate observation.
- Injury updates focus on changes; repeated guidance is consolidated.
- Pending rehab feedback remains accessible after the completed session disappears.

## Validation from the original checkout

39 focused frontend tests passed, including rendered components and interaction coverage. Six isolated checks of the actual Pydantic answer schema passed. Changed TypeScript files passed syntax checks. Full app/API verification could not run because source dependencies, including api/contracts/checkin_decision.py, are absent.

## Required before ready for review

Port these behavioral changes onto current Main, retaining its newer implementation. Run full API integration tests, typecheck and rendered end-to-end verification. This draft is not deployable and must not be merged as a completed implementation.
