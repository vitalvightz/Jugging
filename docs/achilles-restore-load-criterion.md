# Simple Clinical Clearance and Achilles LOAD

Clinical Clearance is the athlete's report of what their clinician advised. It is not medical clearance issued or independently verified by Unlxck.

Two selectors are saved together through the existing episode-owned `clinician_clearance_report`: rehabilitation (gentle recovery, loading, sport-specific, not sure/not cleared) and training (rehab only, non-contact, contact). The optional rehabilitation permission has schema version 1. Existing training-only reports never imply loading permission. Current episode ownership, setback invalidation, most-restrictive multi-injury training scope and hard safety restrictions remain authoritative.

Today no longer renders the Achilles assessment worksheet. The shared assessment, private clinical-review capture, ledger and verified-pin infrastructure remain available to their existing callers; none is a mandatory gate in this athlete workflow. Self-reports never receive verified-clinician pins or approval records.

## Exercise selection and progression

The existing progression engine remains the sole stage authority. The Achilles RESTORE → LOAD transition retains the existing complete-history, reviewed-exposure, during/next-day response and unresolved-setback checks. Its four worksheet inputs and trusted clinician-interpretation prerequisite are replaced by the reported loading permission and applicability of the one reviewed option. This is a permission ceiling, not a clinical readiness assessment.

Only a current loading or sport-specific permission can make LOAD eligible. Gentle recovery and unknown/not-cleared permissions retain conservative CALM/RESTORE work. The simple injury information editor asks once whether the problem is above the heel, at the tendon/heel junction or unknown; it stores the answer in the existing description field. Only an explicit midportion answer supports this option. Unknown, insertional, ambiguous and unsupported sides stay conservative. Previously reported medical concerns and explicit restrictions still hold loading. No assessment data is needed to grant permission.

The only live higher-stage option remains **Floor-level controlled Achilles lowering**: bodyweight, 1 × 10, once daily at most, stable support, floor only, no step/deep dorsiflexion, speed or automatic increase. Equipment, severity, side, readiness, daily allocations and other injury restrictions still apply. DYNAMIC and RETURN remain closed even with sport-specific permission. Every other profile and the exercise bank are unchanged.

## Daily tracking and history

The existing injury check-in uses Better / Same / Worse. The same answer supplies any open next-day rehab response. Standalone rehab uses Completed / Modified / Skipped without ratings, measurements or text boxes. Mixed training sessions retain their training review and use the same three choices for their rehab portion.

Completion records and immutable episode response contexts still establish which work happened. Completed as shown can quantify the frozen dose. Modified records changed work with unknown performed amount. Skipped creates no rehab exposure, including when the surrounding training session is completed. Exposure recording is idempotent and retries partial writes through the existing contexts.

New exposure provenance explicitly selects injury-check-in response tracking. A same-day explicit response after logged work supplies an observed categorical response in the read projection; an earlier response cannot predict tolerance of later work. Worsening wins over reassuring later same-day reports. An absent response stays unknown. Existing stored exposures are not rewritten.

Permission reductions or withdrawal invalidate incompatible unstarted work and stale acceptance revisions. Started/completed snapshots and historical exposures remain immutable. Current red flags, worsening and multi-injury restrictions can still stop unsafe continuation.

## Schema and verification

Rehabilitation permission is JSON in the existing clearance event; it requires no new table or column. Migration `20261019174438_allow_skipped_rehab_performance.sql` adds `skipped` to the existing completion-field constraint so a completed training session can record zero rehab work. It changes no RLS or ownership policy and must precede deployment of the updated API.

The updated activation tests exercise reported permission → existing engine → actual Today → acceptance → completion → exposure without assessments or admin approval. Existing shared review tests remain independent. UI tests cover the two selectors, self-report copy, removed worksheet, three response/completion choices and ownership-bound delayed answers. Current inventory reports use the live catalog; dated planning/archive checks reconstruct their exact preserved baseline.
