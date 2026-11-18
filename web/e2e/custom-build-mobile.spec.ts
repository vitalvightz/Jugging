import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { setup } from './custom-plan-fixture';
import { blankDose } from '../lib/custom-plan-dose';
import type { CustomPlan } from '../lib/api-schema.generated';

test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: 'wait' }); });
for (const width of [320, 390, 430, 1280]) {
  test(`compact validation review preserves context at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error' && !/Failed to load resource|net::ERR_FAILED/.test(message.text())) errors.push(message.text()); });
    const { saved, mutations } = await setup(page, baseURL!);
    const plan: CustomPlan = {
      id: crypto.randomUUID(), name: 'Camp programme', kind: 'self_directed', incomplete: true,
      schedule_mode: 'weekly', start_date: null, end_date: null,
      created_at: '2026-11-18T12:00:00Z', updated_at: '2026-11-18T12:00:00Z',
      sessions: (['Monday', 'Friday'] as const).map((weekday, index) => ({
        id: crypto.randomUUID(), name: `Training ${index + 1}`, weekday, scheduled_date: null, session_type: 'strength_power',
        exercises: Array.from({ length: 4 }, (_, exercise) => ({
          id: crypto.randomUUID(), origin: 'manual', name: `Movement ${index + 1}-${exercise + 1}`,
          exercise_type: 'strength', equipment: null, notes: null, media_id: null,
          community_id: null, community_revision_id: null, coaching_cues: null, stop_rules: null,
          incomplete: true, dose: blankDose(),
        })),
      })),
    };
    saved.set(plan.id, plan);
    await page.goto(`/plans/custom?plan=${plan.id}`);
    await expect(page.getByRole('heading', { level: 1, name: plan.name })).toBeVisible();
    await expect(page.getByRole('status').filter({ hasText: '8 prescriptions incomplete' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Save & view plan', exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Draft saved.' })).toBeVisible();
    expect(saved.get(plan.id)!.incomplete).toBe(true);
    expect(mutations.some(path => /publish|activate/.test(path))).toBe(false);
    const save = page.getByRole('button', { name: 'Save draft', exact: true });
    const footer = save.locator('..').locator('..');
    expect((await footer.boundingBox())!.height).toBeLessThan(140);
    expect((await save.boundingBox())!.y + (await save.boundingBox())!.height).toBeLessThan(width < 768 ? 780 : 844);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (process.env.CUSTOM_QA_DIR) await page.screenshot({ path: `${process.env.CUSTOM_QA_DIR}/compact-programme-${width}.png` });
    const reviewAction = page.getByRole('button', { name: 'Review', exact: true });
    await reviewAction.click();
    const review = page.getByRole('dialog', { name: 'Review plan', exact: true });
    await expect(review.getByRole('region')).toHaveCount(2);
    await expect(review.getByRole('button', { name: /^Fix / })).toHaveCount(8);
    // The sheet traps focus in both directions and exposes no nested scroll regions.
    await page.keyboard.press('Shift+Tab');
    await expect(review.getByRole('button', { name: 'Back to programme' })).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(review.getByRole('button', { name: 'Close', exact: true })).toBeFocused();
    const fix = review.getByRole('button', { name: 'Fix Movement 2-4 in Training 2', exact: true });
    await fix.scrollIntoViewIfNeeded();
    const reviewScroll = await review.evaluate(node => node.scrollTop);
    await fix.click();
    const editor = page.getByRole('dialog', { name: 'Movement 2-4', exact: true });
    await expect(editor).toBeVisible();
    await expect(review).toHaveCount(0); // inactive review stays mounted but is hidden from assistive technology
    await editor.getByLabel('Sets', { exact: true }).fill('3');
    await editor.getByLabel('Reps', { exact: true }).fill('8');
    await editor.getByRole('button', { name: 'Done', exact: true }).click();
    await expect(review).toBeVisible();
    await expect(review.getByRole('button', { name: /^Fix / })).toHaveCount(7);
    await expect(review).toBeFocused(); // removed issue trigger falls back to its review sheet
    expect(await review.evaluate((node, before) => Math.abs(node.scrollTop - Math.min(before, node.scrollHeight - node.clientHeight)) < 2, reviewScroll)).toBe(true);
    expect((await new AxeBuilder({ page }).include('[role="dialog"]:not([aria-hidden="true"])').analyze()).violations).toEqual([]);
    if (process.env.CUSTOM_QA_DIR) await page.screenshot({ path: `${process.env.CUSTOM_QA_DIR}/compact-review-${width}.png` });
    await page.keyboard.press('Escape');
    await expect(review).toHaveCount(0);
    await expect(reviewAction).toBeFocused();
    await expect(page.getByRole('button', { name: /^Monday,/ })).toHaveAttribute('aria-pressed', 'true');
    await page.getByRole('button', { name: 'Undo', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: '8 prescriptions incomplete' })).toBeVisible();
    await page.getByRole('button', { name: 'Redo', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: '7 prescriptions incomplete' })).toBeVisible();
    expect(errors).toEqual([]);
  });
}
