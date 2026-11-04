# UNLXCK Cookies & Local Storage (PECR Position)

**Regulation:** Regulation 6, Privacy and Electronic Communications (EC Directive) Regulations 2003
**Status:** Existing functional storage remains necessary for requested features. Private beta usage analytics is included under the beta agreement; it adds no cookies, localStorage identifiers or replay. See `docs/beta-usage-analytics.md`.

## The rule

PECR reg. 6 requires consent before storing information on, or reading information from, a user's device — cookies, `localStorage`, `sessionStorage` alike. The technology does not matter; the storage does.

There is one exemption, at reg. 6(4): storage that is **strictly necessary** for a service the user has explicitly requested. The ICO reads this narrowly. "Necessary for us to run the business" is not the test; "the requested feature cannot work without it" is. Analytics, product measurement and session recording are outside it.

UNLXCK uses **no cookie banner**, which is only correct while every item of storage is either strictly necessary or consented to.

## Register

| What | Purpose | PECR position |
|---|---|---|
| Supabase auth session (`localStorage`) | Keeps the athlete signed in between page loads | **Strictly necessary.** Authentication is the requested service; without it every navigation would sign the athlete out. |
| Cloudflare Turnstile (`__cf_bm` and related) | Bot and abuse prevention at signup and login | **Strictly necessary.** Reg. 6(4) covers security measures protecting the requested service. The ICO accepts security storage on this footing, and the data at risk here is children's health data. Turnstile must never receive athlete health data — see `docs/processor-dpa-international-transfer-verification.md`. |
| Appearance mode (`auth-provider.tsx:55`) | Remembers the athlete's light/dark preference | **Strictly necessary.** A user-preference store set in direct response to a user action is the textbook reg. 6(4) example. |
| Generation status / dismissed-ribbon keys (`generation-status-provider.tsx`, `global-generation-status.tsx`) | Tracks an in-flight plan generation across tabs and remembers a dismissed notice | **Strictly necessary.** Purely functional state for a feature the athlete started. Contains no health information beyond a job identifier. |
| Watched demos (`unlxck:demo-watched:v1`, `web/lib/exercise-demo.ts`) | Remembers which exercise demo videos the athlete has already played, so the next view shows a compact "Replay demo" strip instead of the full player | **Functionality preference, same footing as the dismissed-ribbon key above.** It remembers the athlete's own interaction with a feature they chose to use, and is written only in direct response to their action (playing a demo), never on page view. It holds video IDs only (no health or account data), is capped at 500 entries and never leaves the device. It is disclosed in the Privacy Notice ("YouTube demonstration videos"). Deleting it only makes demos show at full size again. If this footing is ever doubted, the fallback is to keep the value in memory for the page session instead: it does not warrant a consent prompt. |
| YouTube player (`www.youtube-nocookie.com`, loaded by `components/exercise-demo.tsx`) | Plays an exercise demo video | **Consent by action.** Third-party storage set by YouTube, not by UNLXCK, and not strictly necessary for UNLXCK's own service. The player is never loaded on page view: the athlete sees a thumbnail and a "Watch on YouTube" link, and the player (and any storage it sets) loads only when they tap play. The Privacy Notice ("YouTube demonstration videos") explains this before the tap is needed, and says declining costs nothing: cues and stop rules work without the video. Preview thumbnails load from `i.ytimg.com` on view; that is an image request (IP address and browser headers), not device storage. **Re-check** if the player is ever preloaded or autoplayed without a tap. |
| Sentry error monitoring (`instrumentation-client.ts`) | Reports faults so they can be fixed | **No reg. 6 engagement.** Error events are transmitted, not stored on the device. Session Replay — which did store — has been removed. |

## Sentry Session Replay — removed

`web/instrumentation-client.ts` previously initialised `Sentry.replayIntegration()` at `replaysSessionSampleRate: 0.1` and `replaysOnErrorSampleRate: 1.0`, recording one in ten athlete sessions and every session that hit an error. Replay writes a session identifier to browser storage, engaging reg. 6, and session recording is not strictly necessary under reg. 6(4).

**It has been removed rather than put behind a consent banner.** That was the stronger option: it takes UNLXCK out of reg. 6 for this path completely, leaves no consent mechanism to build or maintain, and removes the question of how to ask a 13-year-old for consent to being recorded in a way that meets the Age Appropriate Design Code. Error events themselves require no storage consent, so fault diagnosis is unaffected.

The masking previously configured (`maskAllText`, `maskAllInputs`, `blockAllMedia`) was the right configuration and reduced what a recording contained — but it never removed the consent requirement, and URL paths, breadcrumbs and tags sat outside it.

`web/lib/legal-documents.test.ts` asserts that no replay option reappears in the client configuration, so the published claim that UNLXCK does not record your screen or session cannot silently stop being true.

**Reintroducing replay requires, before any code:** a consent mechanism defaulting to off, a DPIA covering recording of child sessions, and the AADC position on obtaining that consent from a minor.

**Note — this does not close the Sentry processor work.** Sentry still receives personal data as an error-monitoring processor. The DPA, data region and UK transfer safeguards remain outstanding and are tracked in `docs/data-map-processor-register.md`. PECR and UK GDPR are separate regimes; satisfying one does not satisfy the other.

## Client log capture — recommendation, not yet actioned

`instrumentation-client.ts` sets `enableLogs: true`, which ships browser console output to Sentry. The backend scrubs sensitive keys before send (`api/sentry_config.py`), but **no equivalent scrubber is configured on the client**, so a console statement carrying athlete data would reach Sentry unfiltered.

This is not a PECR matter — no device storage is involved — but it is a data-minimisation one. Either disable client log capture, or add a `beforeSendLog` scrubber mirroring the backend's. Left as-is pending a decision, since it is a live debugging capability.

## Rules

- No new cookie, `localStorage` or `sessionStorage` write ships without an entry in the table above and a reg. 6 assessment.
- Anything that is not strictly necessary requires a consent mechanism first — the absence of a banner is a consequence of the current storage set, not a standing decision.
- Analytics, advertising, A/B testing and heatmap tooling are all outside reg. 6(4). Treat any proposal to add them as requiring consent infrastructure as a precondition, not a follow-up.

## Review

Re-check when storage is added or removed, when a third-party script is introduced, when the Sentry replay decision is made, or when the ICO updates its guidance on the strictly-necessary exemption.

This is an internal compliance assessment, not legal advice.

## Private beta usage analytics

Usage events are automatic for beta athlete accounts. No analytics cookie, persistent browser identifier, fingerprint or session replay is added. Events contain named feature/action categories and request/flow UUIDs; the authenticated API supplies the account and server timestamp. Events expire after 180 days. See `docs/beta-usage-analytics.md` for exclusions, verification and rollout.
