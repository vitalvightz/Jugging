# Private beta usage analytics

This update replaces the separate default-off feature-analytics choice for the private beta, on the owner's instruction that participants have already agreed to usage measurement. Existing preference rows are preserved as historical records, not rewritten as consent.

## Reading the dashboard

- **Visited today / this week / this month** counts different people who opened or returned to the app. Opening five times still counts as one person.
- **Last seen in app** is the latest recorded visit, feature action or saved training record. No record means unknown.
- **Active athletes / feature actions** means someone did something, such as starting a timer or saving training. Simply browsing does not inflate these counts.
- **Tracking collector ready** confirms that the database retention scheduler is healthy. It does not prove every browser delivered events. Visits prove actual received events.
- Use **Refresh activity** to load the latest received events. UTC days begin at midnight UTC.

Automatic visits start with the new frontend deployment. Historical unrecorded visits cannot be recovered. Offline requests, closing the browser before delivery, exhausted retries and collection limits can still leave gaps. No product can promise to capture every browser interaction.

## Implementation and rollout

1. Apply `20261030102139_required_beta_product_analytics.sql` after the existing analytics migrations. It adds a service-only rollout timestamp, enables ingestion for eligible beta athletes without a stored choice, accepts app/page events and returns collector health. Existing internal account exclusions, per-profile exclusions, retention, rate limits, deduplication and service-only writes remain.
2. Deploy the API and frontend from this change. The backend schema gate requires the rollout table; reports return a clear 503 when the new facts projection is missing.
3. On a dedicated verification athlete, open the app, navigate to a plan/history/timer, then start a timer or save a check-in. Verify POST `/api/analytics/events` returns `accepted: true`, and verify the corresponding event rows using the same authenticated athlete ID. No preference PUT should be needed.
4. Refresh admin analytics. Confirm one visitor, the last-seen timestamp, named feature actions and the individual timeline. Duplicate retry IDs must create one row. Verify non-admin access is refused and health details/URLs are absent from events.

The frontend sends only named categories, actions and retry/flow UUIDs. The server owns the actor and timestamp. Three transport attempts use the same ID. App opens are recorded on authenticated load and return to a visible tab; page categories are recorded on navigation. Refresh-token changes can create another open event but do not duplicate visitor counts. Signup/profile/plan-generation timestamps do not masquerade as visits.

The beta notice replaces the optional checkbox in Settings and onboarding. This is an implementation of the beta owner's stated agreement, not a new record of individual consent. Legal notice text must stay consistent with the actual beta agreement. Existing support exclusions and account deletion remain available.

## Validation on 30 October 2026

- 48 targeted backend report/API/schema-gate tests passed.
- 9 targeted frontend analytics/workflow/notice tests and 17 legal-copy tests passed.
- Production Next.js build and TypeScript passed. Targeted ESLint: no errors, three existing effect-state warnings. Targeted Python lint and compile checks passed.
- PostgreSQL 17 with pg_cron: replayed the analytics migrations and the new migration; beta SQL assertions passed (new account, old disabled preference, retry uniqueness, report delivery, exclusions, privileges and unhealthy-retention rejection).
- Playwright: three tests passed, covering automatic beta requests without any preference request and populated admin analytics at 390px/1280px, filters, pagination, coverage, runtime errors and overflow. Reports in UI tests are synthetic fixtures; they do not claim production totals.
- Read-only production preflight found the retention scheduler healthy, one enabled preference and one existing product event. No live records, migrations or deployments were changed during this work. Production browser/API delivery checks remain a rollout step.

## Review follow-up

Background event transport failures and HTTP 502/503/504 responses no longer change the app-wide connection status. Foreground request failures still trigger recovery. Removed unused browser preference helpers/cache and test-only opt-in setup. SQL assertions now require explicit expected values, so missing health fields, NULL aggregates and missing coverage cannot pass silently.

Verification: 37 affected frontend tests and TypeScript passed. PostgreSQL beta assertions passed; injecting a missing health object and empty facts independently caused the expected assertion failures. Python lint passed. No production changes were made.

## Usage accuracy and admin directory follow-up (4 November 2026)

No additional migration is needed for this follow-up. A read-only production check confirmed that `product_analytics_rollout` exists, retention is healthy and browser events are arriving. Merging before migration has not made the migration impossible; it is already present now.

Visit counters now use a recorded page view or user feature action as well as `app_opened`, deduplicated by account and UTC day. A saved training record proves use on that date even when browser telemetry is missing. Generated plans, registration/profile timestamps and error records do not establish a visit. Feature engagement/retention metrics retain their existing definitions. Historical visit totals can therefore increase based on existing evidence; this does not invent historical page views.

Admin can search names, emails or IDs across the full eligible athlete directory before pagination, and filter to seen today, no recorded visit in seven days or no recorded visit ever. These directory filters do not change headline metrics. Each athlete now has a latest observed action and visit days, kept separate from days since a meaningful feature action. Recent use means a recorded action in the last 30 minutes, not live presence. Collector health is shown alongside the latest received browser-event timestamp and the number of accounts that reported browser events in the selected period.

The frontend records pages only when visible. Hidden initial loads and hidden navigation wait until the tab is shown; access-token refresh does not manufacture another view/open for the same authenticated athlete. Browser event POSTs use keepalive to improve delivery during navigation/close, with the existing stable retry identities and connectivity isolation. No URL, query, health answer or device identifier is collected. Page categories remain coarse; no session-duration or exact live-presence claim is made.

Validation for this follow-up: 50 backend/API/schema-gate tests, seven focused frontend tracking/transport tests, production Next.js build/TypeScript, Python lint/compile and three Playwright tests passed. Browser plugin was unavailable, so repository Playwright was used. Phone (390px) and desktop (1280px) checks covered meaningful render, no runtime errors/overlays, contained horizontal table scrolling, latest action display, directory search, visit-history filters, pagination and automatic beta event requests. Screenshots were captured outside the repository. Targeted ESLint had no errors and three existing effect-state warnings. New report logic and UI are not yet deployed; no production records or schema were changed.
