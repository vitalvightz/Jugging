import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { TodayDecisionPanel } from "./today-decision-panel";
import { resolveTodayDecision } from "../../lib/today-authoritative";
import type { TodayCommandView } from "../../lib/types";

test("the decision block removes repeated instructions and preserves distinct safety guidance", () => {
  const state: TodayCommandView = {
    active_plan: { id: "p" },
    today: { training_day: "2026-11-16", recommendation_state: "pull_back", decision_tier: "pull_back",
      warnings: [], next_session: { session_id: "s", session_relation: "today" }, session_scope: "today",
      session_label: "Today", completion_status: "not_started" },
    risk_watch: [], open_injuries: [], week_summary: {}, quick_actions: [],
  };
  const banner = resolveTodayDecision(state).banner!;
  const html = renderToStaticMarkup(<TodayDecisionPanel banner={{ ...banner, action: "Avoid contact.", detail: "Avoid contact.", safety: "Keep it covered." }} />);
  assert.equal((html.match(/Avoid contact\./g) || []).length, 1);
  assert.match(html, /Keep it covered/);
});
