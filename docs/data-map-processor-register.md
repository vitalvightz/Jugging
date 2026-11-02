# UNLXCK Data Map & Processor Register

## Purpose
Internal record of the main personal-data flows used by UNLXCK. Keep this aligned with production code, the Retention Policy and the Privacy Notice.

## Data map

| Data | Purpose | Sensitivity | Main location / recipient | Access | Retention |
|---|---|---|---|---|---|
| Account data: name, email, username, role, profile settings | Authentication and account operation | Personal data | Supabase Auth/Postgres | Athlete; authorised backend/admin | While account is active; deletion handled under the Retention Policy |
| Athlete profile: age, sex, height, weight, sport/style, schedule, goals | Personalise training and nutrition | Personal; some fields may contribute to health inferences | Supabase/Postgres; plan-generation pipeline | Athlete; authorised backend/admin | While needed to provide the service; review/delete on account closure |
| Injury and symptom data: area, severity, trend, wound/infection signals, notes, restrictions | Adapt or restrict training and trigger safety escalation | **Special-category health data** | Supabase/Postgres; backend injury engine; relevant context may enter plan generation | Athlete; authorised backend/admin | While needed for personalised training/safety; delete with account unless specifically justified |
| Readiness/recovery: soreness, fatigue, sleep, readiness, injury notes | Daily training adaptation | **Special-category health data where it reveals health status** | Supabase/Postgres; recommendation engine | Athlete; authorised backend/admin | While needed for personalised training/safety; delete with account unless specifically justified |
| Training/session data: completion, RPE, duration, notes | Progress tracking and adaptation | Personal; may reveal health/performance information | Supabase/Postgres | Athlete; authorised backend/admin | While needed for training history/adaptation; review on account closure |
| Nutrition/weight data: bodyweight, target weight where permitted, appetite, supplements/caffeine | Nutrition and fight-camp guidance | Personal; may constitute/infer health data depending on use | Supabase/Postgres; application logic | Athlete; authorised backend/admin | While needed for nutrition/camp functions; delete with account unless specifically justified |
| Plans and adaptation decisions | Deliver personalised programme and explain changes | Personal; may contain health-derived recommendations | Supabase/Postgres; OpenAI for AI-assisted plan generation | Athlete; authorised backend/admin | While needed to provide the service; review on account closure |
| Beta feedback and screenshots | Product QA and support | Personal; screenshot may contain sensitive app information | Supabase private storage/Postgres; Resend notification where enabled | Authorised admins | Screenshots: maximum 90 days; feedback anonymised/deleted when identifiable data no longer required |
| Push subscription, timezone and training-time data | Deliver training/safety notifications at appropriate times | Personal/device data | Supabase/Postgres; web-push infrastructure | Athlete; authorised backend | While needed to provide requested notification functionality; remove when no longer required |
| Security/diagnostic events | Reliability, abuse prevention and incident investigation | Technical/personal data | Server/platform logs; Cloudflare Turnstile for anti-abuse at auth | Authorised operations; relevant processor | Only for the documented security/audit purpose and applicable operational lifecycle |

## Processor / service register

| Service | Role in UNLXCK | Data potentially processed | Verification status |
|---|---|---|---|
| **Supabase** | Authentication, PostgreSQL database, private storage | Account, profile, training, health/injury, nutrition, plans, feedback | **VERIFIED** — Pro project in Paris (`eu-west-3`); DPA and UK transfer safeguards documented |
| **OpenAI** | AI-assisted plan generation; training-focus suggestions | Plan-generation context, potentially including injury/restriction information; with health consent, training notes plus that day's session/exercise names and logged status (no identity; health-language notes are never sent) | **VERIFIED** — DPA/UK transfer safeguards documented; API data not used for training by default |
| **Vercel** | Next.js web hosting / routing | Request, device and technical metadata | **VERIFIED** — UNLXCK uses Vercel Pro; DPA and transfer safeguards documented |
| **Hetzner** | Backend and generation-worker hosting | Data processed by backend/worker in transit and memory; server logs | **VERIFIED** — Nuremberg, Germany; AVV/DPA accepted |
| **Resend** | Service/feedback email | Privacy-minimised email and feedback notification data | **VERIFIED** — DPA and UK transfer framework documented |
| **Cloudflare Turnstile** | Signup/login abuse prevention | Device/network/security-challenge information | **VERIFIED** — DPA and UK transfer safeguards documented; health data must not be sent |
| **Sentry** | Error diagnostics, frontend and backend | Error events, technical context, scrubbed request metadata. No session recording. | **NOT VERIFIED** — see below |

## Sentry

This register previously recorded that Sentry was not used. That was wrong, and the error is kept visible here rather than quietly overwritten, because it is what let an undisclosed processor run in production.

Sentry is live in both surfaces:

- frontend — `@sentry/nextjs` in `web/package.json`, initialised in `web/instrumentation-client.ts`, plus server and edge configs;
- backend — `sentry-sdk` in `requirements.txt`, `init_sentry()` called unconditionally at `api/app.py`.

**Session Replay has been removed** (see `docs/cookies-and-local-storage.md`). Sentry no longer records athlete sessions, which closes the PECR question and the child-session recording question. It does not change Sentry's status as a processor receiving personal data.

**Outstanding before this can be marked VERIFIED:**

1. Execute and record the Sentry DPA.
2. Confirm the data region and, if data leaves the UK, record the UK Addendum/IDTA and complete a transfer risk assessment.
3. Update the DPIA to cover error-monitoring data flows.

Mitigations already in place: `api/sentry_config.py` sets `send_default_pii=false` and scrubs injuries, pain, notes, intake, goals and credentials before send. This reduces the exposure but closes none of the three items above.

**Status is NOT VERIFIED and stays that way until all three are complete.** Removing replay reduced the risk; it did not verify the processor.

## Independent controllers

| Service | Role in UNLXCK | Data it receives | Position |
|---|---|---|---|
| **YouTube (Google)** | Exercise demonstration videos, via the YouTube API Services (IFrame player in privacy-enhanced mode, `api/services/exercise_media.py`, `web/components/exercise-demo.tsx`) | From the athlete's browser: IP address, device/browser information and, once the athlete taps play, player usage and any cookies or local storage YouTube sets. From the backend: video IDs only (the Data API availability and Made for Kids check). No account, training or health data. | **Independent controller**, not a processor: Google decides how player data is used under its own terms, so there is no DPA to execute. Disclosed in the Privacy Notice ("YouTube demonstration videos") with links to the Google Privacy Policy and the YouTube Terms of Service; the Terms of Use bind users to the YouTube Terms of Service as the YouTube API Services Terms require. Made-for-kids videos are never served. |

## Controller position
UNLXCK determines why and how athlete data is processed for the service and treats itself as the controller for these core processing activities. External services acting on UNLXCK's instructions are assessed and contracted as processors where appropriate.

## Rules
- Health and health-inference data must be identified in the Privacy Notice and lawful-basis/DPIA work.
- Do not add a new third-party recipient or materially new data use without updating this register.
- Do not send more athlete data to an external service than the function requires.
- Retention follows `docs/data-retention-deletion-user-rights.md`.
- Processor contracts and international-transfer safeguards follow `docs/processor-dpa-international-transfer-verification.md`.

## Review trigger
Re-check this map when data fields, processors, hosting, AI payloads, notification infrastructure, coach/parent access or retention behaviour materially changes.
