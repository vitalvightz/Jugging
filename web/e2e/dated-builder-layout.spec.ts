import { expect, test } from '@playwright/test';
import { setup } from './custom-plan-fixture';
import { blankDose } from '../lib/custom-plan-dose';
import type { CoachPlan } from '../lib/api-schema.generated';
const fighterId = '00000000-0000-4000-8000-000000000003';
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: 'wait' }); });
for (const width of [320, 390, 430, 768, 1024, 1280, 1600]) {
  test(`coach dated calendar layout at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.clock.install({ time: new Date('2026-11-18T12:00:00Z') });
    await setup(page, baseURL!, 'coach');
    const plan: CoachPlan = {
      id: crypto.randomUUID(), name: 'Week 1 fight camp', kind: 'self_directed', schedule_mode: 'dated',
      start_date: '2026-11-23', end_date: '2026-12-20', incomplete: true,
      fighter_id: fighterId, assignment_status: 'draft', coach_name: null, assigned_at: null, published_plan_id: null,
      created_at: '2026-11-18T12:00:00Z', updated_at: '2026-11-18T12:00:00Z',
      sessions: (['Monday', 'Tuesday', 'Wednesday', 'Friday'] as const).map((weekday, index) => ({
        id: crypto.randomUUID(), name: ['Strength and power', 'Boxing', 'Day 3 - conditioning', 'Hard sparring'][index],
        weekday, scheduled_date: `2026-11-${23 + (index === 3 ? 4 : index)}`, session_type: 'mixed',
        exercises: [0, 1].map(i => ({ id: crypto.randomUUID(), origin: 'manual', name: i === 0 ? `Exercise ${index + 1}-1 with a much longer name that wraps onto multiple lines` : `Exercise ${index + 1}-2`,
          exercise_type: 'strength', dose: blankDose(), incomplete: true, equipment: null, notes: null, media_id: null,
          community_id: null, community_revision_id: null, coaching_cues: null, stop_rules: null })),
      })),
    };
    await page.route('**/rest/v1/rpc/**', route => route.fulfill({ json: new URL(route.request().url()).pathname.endsWith('coach_fighters') ? [{
      id: fighterId, connection_id: 'connection', name: 'Test Fighter', today: '2026-11-18', timezone: 'Europe/London',
      fight_date: '2026-12-02', sport: 'boxing', share_recovery: false, share_injuries: false, share_weight: false,
      plan_id: null, plan_name: null, sessions: [], schedule: [], recovery: null, injuries: null,
    }] : [] }));
    await page.route('**/api/coach/fighters/*/training/plan**', route => route.fulfill({ json: [] }));
    await page.route('**/api/coach/fighters/*/plans**', route => route.fulfill({ json: new URL(route.request().url()).pathname.endsWith('/plans') ? [plan] : plan }));
    await page.goto('/coach/plans');
    await page.getByRole('button', { name: 'Create New Plan', exact: true }).click();
    await expect(page.getByLabel('Schedule mode', { exact: true })).toHaveValue('dated');
    await expect(page.getByRole('region', { name: 'Plan calendar', exact: true })).toBeVisible();
    await expect(page.getByRole('group', { name: 'Weekly training calendar', exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: '← Saved plans', exact: true }).click();
    await page.getByRole('button', { name: 'Edit draft', exact: true }).click();
    const builder = page.getByRole('region', { name: 'Custom training plan builder', exact: true });
    const calendar = page.getByRole('region', { name: 'Plan calendar', exact: true });
    await expect(page.getByLabel('Schedule mode', { exact: true })).toHaveValue('dated');
    await expect(builder.getByRole('status').filter({ hasText: '8 prescriptions incomplete' })).toBeVisible();
    for (const view of ['Week', 'Month']) {
      await calendar.getByRole('button', { name: view, exact: true }).click();
      await calendar.scrollIntoViewIfNeeded();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      expect(await calendar.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
      await expect(calendar.getByText('Mon', { exact: true })).toBeInViewport();
      if (width < 1024) {
        const toggleBounds = (await calendar.getByRole('group', { name: 'Calendar view', exact: true }).boundingBox())!;
        expect(toggleBounds.y).toBeGreaterThanOrEqual(64);
      }
      const controls = await calendar.getByRole('group', { name: 'Calendar view', exact: true }).getByRole('button').evaluateAll(buttons => buttons.map(button => { const rect = button.getBoundingClientRect(); return { left: rect.x, right: rect.right, top: rect.y, bottom: rect.bottom }; }));
      expect(controls[0].right).toBeLessThanOrEqual(controls[1].left);
      const session = builder.getByRole('region', { name: 'Session 1', exact: true });
      const calendarBounds = (await calendar.boundingBox())!, sessionBounds = (await session.boundingBox())!;
      if (width < 1600) expect(sessionBounds.y).toBeGreaterThanOrEqual(calendarBounds.y + calendarBounds.height + 16);
      else expect(sessionBounds.x).toBeGreaterThanOrEqual(calendarBounds.x + calendarBounds.width + 16);
      if (process.env.CUSTOM_QA_DIR) await page.screenshot({ animations: 'disabled', path: `${process.env.CUSTOM_QA_DIR}/coach-calendar-${view}-${width}.png` });
    }
    await builder.getByRole('button', { name: 'Review', exact: true }).click();
    const review = page.getByRole('dialog', { name: 'Review plan', exact: true });
    const fixes = review.getByRole('button', { name: /^Fix / });
    await expect(fixes).toHaveCount(8);
    const actionColumns = await fixes.evaluateAll(buttons => buttons.map(button => { const rect = button.getBoundingClientRect(); return { x: rect.x, width: rect.width, height: (button as HTMLElement).offsetHeight }; }));
    expect(new Set(actionColumns.map(rect => Math.round(rect.x))).size).toBe(1);
    expect(new Set(actionColumns.map(rect => Math.round(rect.width))).size).toBe(1);
    expect(actionColumns.every(rect => rect.height >= 44)).toBe(true);
    if (process.env.CUSTOM_QA_DIR) await page.screenshot({ animations: 'disabled', path: `${process.env.CUSTOM_QA_DIR}/uniform-fix-${width}.png` });
    await review.getByRole('button', { name: 'Fix Exercise 4-2 in Hard sparring', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Exercise 4-2', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(review).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(calendar.getByRole('button', { name: 'Month', exact: true })).toHaveAttribute('aria-pressed', 'true');
  });
}
