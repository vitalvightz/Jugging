import assert from "node:assert/strict";
import test from "node:test";

import {
  BOTTOM_NAV_ITEMS,
  isStandaloneNutritionPath,
  showsMobileTabBar,
  SIDE_NAV_ITEMS,
} from "./beta-navigation.ts";

test("bottom nav shows Overview, Today, Plan, History and Timer", () => {
  assert.deepEqual(
    BOTTOM_NAV_ITEMS.map((item) => item.href),
    ["/", "/today", "/plans", "/history", "/timer"],
  );
});

test("Progress is not promoted into primary navigation", () => {
  assert.equal(BOTTOM_NAV_ITEMS.some((item) => item.label === "Progress"), false);
  assert.equal(SIDE_NAV_ITEMS.some((item) => item.label === "Progress"), false);
  // Camp Setup is occasional: it lives under + Create, not a tab.
  assert.equal(BOTTOM_NAV_ITEMS.some((item) => item.href === "/onboarding"), false);
});

test("bottom nav never links to the standalone Nutrition route", () => {
  assert.equal(
    BOTTOM_NAV_ITEMS.some((item) => isStandaloneNutritionPath(item.href)),
    false,
  );
});

test("side menu keeps Camp Setup without exposing Progress", () => {
  assert.deepEqual(
    SIDE_NAV_ITEMS.map((item) => item.label),
    ["Overview", "Today", "Plan", "History", "Round timer", "Weight tracker", "Camp Setup", "Settings"],
  );
});

test("side menu explains the Plan, Camp Setup and Settings destinations", () => {
  assert.deepEqual(
    SIDE_NAV_ITEMS.filter((item) => ["Plan", "Camp Setup", "Settings"].includes(item.label)),
    [
      { href: "/plans", label: "Plan", meta: "Active and saved plans" },
      { href: "/onboarding", label: "Camp Setup", meta: "Start or edit your camp" },
      { href: "/settings", label: "Settings", meta: "Account & preferences" },
    ],
  );
});

test("the phone bar shows across the signed-in workspace but not on gates or admin", () => {
  for (const path of ["/", "/today", "/plans/abc", "/history", "/timer", "/create", "/settings", "/onboarding", "/generate"]) {
    assert.equal(showsMobileTabBar(path, true), true, path);
  }
  for (const path of ["/admin", "/admin/athletes", "/consent", "/private-trial", "/get-started"]) {
    assert.equal(showsMobileTabBar(path, true), false, path);
  }
  assert.equal(showsMobileTabBar("/today", false), false);
});

test("side menu links Nutrition as the weight tracker", () => {
  assert.deepEqual(
    SIDE_NAV_ITEMS.filter((item) => isStandaloneNutritionPath(item.href)),
    [{ href: "/nutrition", label: "Weight tracker", meta: "Daily weights and trend to target" }],
  );
});

test("isStandaloneNutritionPath matches the workspace and its sub-pages only", () => {
  assert.equal(isStandaloneNutritionPath("/nutrition"), true);
  assert.equal(isStandaloneNutritionPath("/nutrition/bodyweight-log"), true);
  assert.equal(isStandaloneNutritionPath("/nutrition?tab=weight"), true);
  assert.equal(isStandaloneNutritionPath("/nutrition#readiness"), true);
  assert.equal(isStandaloneNutritionPath("/progress"), false);
  assert.equal(isStandaloneNutritionPath("/plans"), false);
  assert.equal(isStandaloneNutritionPath("/"), false);
  assert.equal(isStandaloneNutritionPath("/plans/abc#nutrition"), false);
});
