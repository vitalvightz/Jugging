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

Cleanup remains opportunistic on processor calls. Operations must invoke it
regularly even when uploads are disabled (the service-only processor endpoint
runs cleanup after an unavailable asset claim too). No production scheduler was
created or changed by this patch.

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
