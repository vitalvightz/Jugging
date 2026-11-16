import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { grantSignupAccess, isolateFromNetwork } from "./support";

test("coach signup choice is accessible on mobile", async ({
  page,
  baseURL,
}, info) => {
  await isolateFromNetwork(page, baseURL!);
  await grantSignupAccess(page, baseURL!);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/signup");
  await page.getByRole("radio", { name: "Coach", exact: true }).check();
  await expect(
    page.getByRole("radio", { name: "Athlete", exact: true }),
  ).not.toBeChecked();
  await expect(
    page.getByText("Manage your fighters.", { exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("coach-signup-mobile.png"),
    fullPage: true,
  });
  const result = await new AxeBuilder({ page })
    .include("main")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(
    result.violations.filter((v) =>
      ["serious", "critical"].includes(v.impact ?? ""),
    ),
  ).toEqual([]);
});
