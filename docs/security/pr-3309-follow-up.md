# PR 3309 follow-up

Uploads must stay disabled. This patch does not change production, enable flags,
merge the PR, or waive dependency findings.

## Changes

- Cleanup fences expired uploads as rejected, calls the Storage API, and only
  acknowledges deletion after both buckets succeed. Failed removal remains
  eligible on later cleanup calls. Approved files with failed intake deletion
  also remain discoverable. Processing records the prospective approved path so
  cleanup covers an upload whose final database write failed.
- Claims are never reclaimed. A stalled upload must expire for cleanup and be
  uploaded again. This deliberately trades automatic recovery for avoiding two
  workers writing the same object. Finish/reject require a claim token, finish
  enforces a five-minute lease, and Storage writes cannot overwrite objects.
- Microphone cancellation closes streams returned after a pending permission
  request. The component registers the recorder before awaiting permission.
- Playback consults `training_record_available` using the authenticated athlete
  and the stored record/exercise identifiers before producing a signed link.
- The production verification workflow cannot write flags. Enabling requires a
  separate explicit operator approval/change; routine pushes and verification
  cannot reverse an emergency disable.
- Live verification now processes a 2048 × 2048 JPEG and fails on cleanup errors.

## Dependency assessment (2026-11-18)

`npm audit --omit=dev` still reports six high findings (ten total findings).
The six are picomatch and five dependent packages, rather than six separate
root vulnerabilities. GHSA-fc5q-39xc-vjpf affects picomatch <=4.0.7; the registry
currently reports 4.0.7 as latest. No override to a fixed release is available.

Installed paths:

- @sentry/nextjs 10.76.2 -> @rollup/plugin-commonjs 28.0.1 -> picomatch 4.0.7,
  also through @rollup/pluginutils 5.5.1 and fdir 6.5.0.
- next-intl 4.14.5 -> @parcel/watcher 2.6.0 -> picomatch 4.0.7.

The matching code serves build transforms/file discovery and translation source
watching. No request-controlled glob input was identified in this patch's media
APIs. This is not proof of unreachable runtime exposure: these packages are
production dependencies and a complete bundle/call-path review remains needed.
Audit also reports moderate glob/minimatch findings in Sentry tooling. Its
suggested Sentry major upgrade needs a separate compatibility assessment and
does not itself establish a fixed picomatch version.

The audit gate remains unchanged and failing. There is no risk acceptance or
suppression in this patch. Resolve upstream packages or obtain a reviewed,
specific risk decision before claiming the release gate passes.

## Release verification still required

Apply the new migration before deploying the updated Edge Function (old unfenced
RPC signatures are intentionally removed). Keep upload flags off throughout.
Run the real pipeline verification, then inspect Edge CPU and peak memory for
maximum-dimension images, including high-entropy images and concurrent requests.
A successful local decode or wall-clock request time does not demonstrate the
256 MB memory and 2-second CPU limits. Verify cleanup retries during Storage
failure and repeat playback/navigation browser checks. Run the complete CI
pipeline after resolving the audit gate. Activation is a separate approved
operator action after these checks; verification alone must never enable flags.

Cleanup now has a dedicated `attachment-cleanup` Compose service, independent
of uploads and processing calls. It starts on deployment/reboot, sweeps through
the Storage API every minute and retries failures. Only a successful sweep
updates its heartbeat; five minutes without success makes it unhealthy. The
Hetzner deployment readiness check requires cleanup health. Check it with
`docker compose ps attachment-cleanup` and `docker compose logs attachment-cleanup`.
This service is implemented in the PR and has **not** been deployed here.

## Local verification

- Full application-schema SQL suite passes in isolated PostgreSQL 17, including
  permission assertions, migration replay checks and the new cleanup/lease cases.
- 13 targeted Python tests pass; ruff and Python compilation pass.
- 14 targeted sanitizer/recorder tests pass.
- Deno processor test passes for failed intake deletion, failed approved-file
  deletion and successful retry; Deno type checking passes as part of that test.
- Frontend TypeScript, lint (65 existing warnings, no errors) and production
  build pass. Build used `NEXT_PUBLIC_API_BASE_URL=https://api.example.com`.
- Full dependency audit still fails as detailed above.

Useful commands: `python tools/test_coach_workspace.py --container <isolated-pg>`,
`python -m pytest tests/test_attachment_playback_revalidation.py tests/test_photo_voice_attachments.py tests/test_training_attachments.py -q`,
`npx deno test --allow-env supabase/functions/process-attachment/index_test.ts`,
and the standard `web` npm checks. No production credentials or live resource
verification were used for this patch.
- Complete frontend unit suite: 2,718 passed, zero failures (eight workers; same
  test files and assertions as the normal npm script). The initial serial run
  was stopped while unrelated component files were slow to exit.


## Remaining-blocker follow-up

### Scheduling CI regression

The exact CI failure was
`test_revalidation_preserves_conditioning_goals_and_legal_final_doses` in Python
Quality shard 0 (run 44233578717). It reproduced both on this branch and on Main
c5e92b7e. A selected 5 km aerobic fallback was excluded from goal evidence because
it differed from a role's preferred exercise names. Evidence now checks the
exact final closed-pool member without treating preferences as restrictions;
restriction hits, blocked tags, explicit day allowlists and other safety checks
remain in force. No fixture seed, assertion or production dose was changed.
A regression covers both the valid fallback and rejection of a restricted one.

### Audit remains a failing release gate

The current audit still reports six high and four moderate findings. No fixed
picomatch release was available when checked. The existing Web Build job runs
its audit after build/browser checks and even after earlier failures (unless
cancelled). An audit failure still fails the same build job; there is no ignore,
allowlist, `continue-on-error`, or separate optional check. This exposes browser
results without weakening the existing required check.

### Production deployment mismatch: do not run the old processor

Read-only inspection found the fencing migration applied in production as
`20261118043914`. The repository migration is aligned as
`20261118043914_attachment_processing_fencing.sql`; its SQL is unchanged, and
this filename correction does not reapply the migration. Meanwhile, `process-attachment` is still version 1 with unfenced RPC
calls and the old cleanup implementation. Deploy the matching updated function
before invoking it or running verification. The old cleanup assumes every
candidate can have its approved file deleted; the new SQL also returns ready
assets needing **intake-only** deletion. Therefore the old processor must not
be used for cleanup against the updated schema.

This workspace has no production service-role credential. The credential-backed
verification workflow is not registered on Main yet (GitHub returned 404). No
live processing, production deployment, enablement or real-device permission
prompt test was performed in this follow-up.

The verification script now includes two independent concurrent high-entropy
2048 × 2048 photos as well as duplicate claims of one upload. These functional
checks must run against the matching deployed function; inspect CPU/peak-memory
logs separately before accepting the 256 MB / 2-second resource limits.

Local Chromium attachment tests pass at 320/390/1280 px, including delayed
microphone permission resolution after session close and navigation. This uses
controlled fake media and an isolated API, not production or a physical
microphone. Browser plugin was not available; project Playwright was used with
its installed full Chromium channel. Real-device permission prompts and live
Supabase resource/concurrency checks remain release blockers.

Follow-up validation: 169 scheduling/goal/cleanup regressions pass; 18 attachment
API/playback/cleanup tests pass; all five rendered Chromium attachment tests
pass. TypeScript, lint (65 warnings, no errors), production build with CI public
placeholders, Python ruff/compilation, Compose validation, deployment shell syntax
and diff checks pass. New GitHub CI results must be assessed separately.
