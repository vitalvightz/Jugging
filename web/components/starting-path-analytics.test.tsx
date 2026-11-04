import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { AppRouterContext } from "next/dist/shared/lib/app-router-context.shared-runtime";

import { AppSessionContext } from "./auth-provider";
import { StartingPathScreen } from "./starting-path-screen";

const router = { push() {}, replace() {}, refresh() {}, back() {}, forward() {}, prefetch() {}, bfcacheId: "fixture" };
const me = {
  profile: {
    role: "athlete", starting_path: null, onboarding_draft: {}, technical_style: [],
    date_of_birth: "1990-01-01", meets_minimum_age: true, terms_accepted: true, is_minor: false,
  },
  plan_count: 0,
  latest_plan: null,
  latest_intake: null,
};

test("the starting path asks for no analytics opt-in and shows no analytics notice", () => {
  const html = renderToStaticMarkup(
    <AppRouterContext.Provider value={router as never}>
      <AppSessionContext.Provider value={{ session: { access_token: "t" }, me, isMeHydrated: true, hasTransientMeError: false } as never}>
        <StartingPathScreen />
      </AppSessionContext.Provider>
    </AppRouterContext.Provider>,
  );
  // The beta notice lives in Settings; onboarding has neither notice nor opt-in (see e2e/beta-usage-analytics.spec.ts).
  assert.doesNotMatch(html, /Private beta usage analytics/);
  assert.doesNotMatch(html, /Allow optional feature analytics/);
  assert.doesNotMatch(html, /type="checkbox"/);
});
