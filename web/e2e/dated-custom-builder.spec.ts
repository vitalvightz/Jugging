import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { setup } from './custom-plan-fixture';
import type { CustomPlan, CustomPlanInput } from '../lib/api-schema.generated';
const fighterId = '00000000-0000-4000-8000-000000000003';
for (const role of ['athlete', 'coach']) for (const width of [390, 1440]) {
  test(`dated ${role} builder edits, copies and saves distinct weeks at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.clock.install({ time: new Date('2026-11-16T12:00:00Z') });
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    const fixture = await setup(page, baseURL!, role, '2026-11-23');
    const coachSaved = new Map<string, CustomPlan>();
    if (role === 'coach') {
      await page.route('**/rest/v1/rpc/**', route => route.fulfill({ json: new URL(route.request().url()).pathname.endsWith('coach_fighters') ? [{
        id: fighterId, connection_id: 'connection', name: 'Test Fighter', today: '2026-11-16', timezone: 'Europe/London',
        plan_id: null, plan_name: null, fight_date: '2026-11-23', sport: 'boxing', share_recovery: false, share_injuries: false, share_weight: false, sessions: [], schedule: [], recovery: null, injuries: null,
      }] : [] }));
      await page.route('**/api/coach/fighters/*/training/plan**', route => route.fulfill({ json: [] }));
      await page.route('**/api/coach/fighters/*/plans**', async route => {
        const request = route.request(), url = new URL(request.url());
        const identity = url.pathname.endsWith('/plans') ? null : url.pathname.split('/').at(-1)!;
        if (request.method() === 'GET') return route.fulfill({ json: identity ? coachSaved.get(identity) : [...coachSaved.values()] });
        const body = request.postDataJSON() as CustomPlanInput;
        const id = identity ?? url.searchParams.get('request_id')!;
        const saved = { ...body, id, kind: 'self_directed', fighter_id: fighterId, assignment_status: 'draft', incomplete: false,
          sessions: body.sessions!.map(s => ({ ...s, exercises: s.exercises!.map(e => ({ ...e, incomplete: false })) })),
          created_at: '2026-11-16T12:00:00Z', updated_at: '2026-11-16T12:00:00Z' } as CustomPlan;
        coachSaved.set(id, saved);
        return route.fulfill({ status: identity ? 200 : 201, json: saved });
      });
      await page.goto('/coach/plans');
      await page.getByRole('button', { name: 'Create New Plan', exact: true }).click();
    } else await page.goto('/plans/custom');
    const calendar = page.getByRole('region', { name: 'Plan calendar', exact: true });
    await expect(calendar).toBeVisible();
    await expect(page.getByRole("button", { name: "Calendar", exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect(calendar.getByText('Fight day · Mon 23 Nov 2026')).toBeVisible();
    await expect(calendar.getByRole('button', { name: 'Select Tue 17 Nov 2026', exact: true })).toBeEnabled();
    const calendarBounds = await page.getByRole('region', { name: 'Plan calendar', exact: true }).boundingBox();
    const restBounds = await page.getByRole('heading', { name: 'Rest day', exact: true }).locator('..').boundingBox();
    if (width >= 1600) {
      expect(calendarBounds!.width / restBounds!.width).toBeGreaterThan(1.8);
      expect(restBounds!.x - calendarBounds!.x - calendarBounds!.width).toBeGreaterThanOrEqual(16);
    } else {
      expect(restBounds!.y - calendarBounds!.y - calendarBounds!.height).toBeGreaterThanOrEqual(16);
    }
    await page.screenshot({ path: test.info().outputPath(`dated-empty-${role}-${width}.png`), fullPage: true });
    await page.getByLabel('Plan name', { exact: true }).fill(`Dated ${role} block`);
    await page.getByLabel('Start date', { exact: true }).fill('2026-11-16');
    await page.getByRole('button', { name: width <= 600 ? 'Add session on Monday' : 'Add session on 2026-11-16', exact: true }).click();
    const session = page.getByRole('dialog', { name: 'New session', exact: true });
    await session.getByRole('button', { name: 'Light technical / skill', exact: true }).click();
    await session.getByLabel('Session name', { exact: true }).fill('Technical day A');
    await session.getByRole('button', { name: 'Add session', exact: true }).click();
    await page.getByRole('button', { name: 'Copy session', exact: true }).click();
    await page.getByRole('button', { name: 'Paste on 2026-11-18', exact: true }).click();
    await page.getByRole('button', { name: 'Done', exact: true }).click();
    const calendarSessions = page.getByRole('button', { name: /^Open Technical day A on/ });
    await expect(calendarSessions).toHaveCount(2);
    await page.getByRole('button', { name: 'Undo', exact: true }).click();
    await expect(calendarSessions).toHaveCount(1);
    await page.getByRole('button', { name: 'Redo', exact: true }).click();
    await expect(calendarSessions).toHaveCount(2);
    await page.getByRole('button', { name: 'Undo', exact: true }).click();
    await page.getByRole('button', { name: 'Edit session 1', exact: true }).click();
    await page.getByRole('dialog').getByLabel('Copy session to date', { exact: true }).fill('2026-11-21');
    await page.getByRole('dialog').getByRole('button', { name: 'Copy', exact: true }).click();
    await expect(page.getByRole('dialog').getByText('Copied to Sat 21 Nov 2026.')).toBeVisible();
    await page.getByRole('dialog').getByRole('button', { name: 'Done', exact: true }).click();
    await expect(calendarSessions).toHaveCount(2);
    await page.getByRole('button', { name: 'Undo', exact: true }).click();
    await page.getByRole('button', { name: 'Copy day', exact: true }).click();
    await page.getByRole('button', { name: 'Paste on 2026-11-19', exact: true }).click();
    await page.getByRole('button', { name: 'Done', exact: true }).click();
    await expect(calendarSessions).toHaveCount(2);
    await page.getByRole('button', { name: 'Undo', exact: true }).click();
    await page.getByRole('button', { name: 'Move day', exact: true }).click();
    await page.getByRole('button', { name: 'Move to 2026-11-20', exact: true }).click();
    await expect(page.getByRole('button', { name: /^Open Technical day A on Fri 20 Nov/ })).toHaveCount(1);
    await page.getByRole('button', { name: 'Undo', exact: true }).click();
    await page.getByRole('button', { name: /^Open Technical day A on Mon 16 Nov/ }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Done', exact: true }).click();
    await expect(calendarSessions).toHaveCount(1);
    await page.getByRole('button', { name: 'Edit session 1', exact: true }).click();
    const editor = page.getByRole('dialog');
    await editor.getByLabel('Session date', { exact: true }).fill('2026-11-17');
    await editor.getByRole('button', { name: 'Done', exact: true }).click();
    await page.getByRole('button', { name: 'Duplicate week', exact: true }).click();
    await page.getByRole('button', { name: 'Next calendar period', exact: true }).click();
    await expect(calendar.locator('[data-fight-day="true"]')).toBeVisible();
    await expect(calendar.getByRole('button', { name: 'Select Mon 23 Nov 2026 (Fight day)' })).toBeVisible();
    await expect(page.getByRole('region', { name: 'Session 2', exact: true })).toBeVisible();
    await page.getByRole('button', { name: /^Quick dose for/ }).click();
    const dose = page.getByRole('group', { name: /^Quick dose for/ });
    await dose.getByLabel('Rounds', { exact: true }).fill('5');
    await dose.getByRole('button', { name: 'Done', exact: true }).click();
    await page.getByRole('button', { name: 'Month', exact: true }).click();
    await expect(calendar.locator('[data-fight-day="true"]')).toBeVisible();
    await expect(page.getByRole('button', { name: /^Open Technical day A on/ })).toHaveCount(2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const builder = page.getByRole('region', { name: 'Custom training plan builder', exact: true });
    const a11y = await new AxeBuilder({ page }).include('[aria-label="Custom training plan builder"]').analyze();
    expect(a11y.violations).toEqual([]);
    await page.mouse.move(0, 0);
    await page.screenshot({ path: test.info().outputPath(`dated-builder-${role}-${width}.png`), fullPage: false });
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    if (role === 'coach') await expect(builder.getByText(/Draft saved/)).toBeVisible();
    else await expect(page).toHaveURL(/plan=/);
    const saved = [...(role === 'coach' ? coachSaved : fixture.saved).values()][0];
    expect(saved.schedule_mode).toBe('dated');
    expect(saved.sessions.map(s => s.scheduled_date)).toEqual(['2026-11-17', '2026-11-24']);
    expect(saved.sessions.map(s => s.exercises[0].dose.rounds)).toEqual([3, 5]);
    expect(new Set(saved.sessions.map(s => s.exercises[0].id)).size).toBe(2);
    if (role === 'athlete') {
      await page.reload();
      await expect(page.getByRole("button", { name: "Calendar", exact: true })).toHaveAttribute("aria-pressed", "true");
      await expect(page.getByRole('button', { name: /^Open Technical day A on/ })).toHaveCount(1);
    } else {
      await page.getByRole('button', { name: 'View saved plans', exact: true }).click();
      await page.getByRole('button', { name: 'Edit draft', exact: true }).click();
      await expect(page.getByRole("button", { name: "Calendar", exact: true })).toHaveAttribute("aria-pressed", "true");
      await page.getByRole('button', { name: '← Saved plans', exact: true }).click();
      coachSaved.set(saved.id, { ...saved, assignment_status: 'pending', assigned_at: '2026-11-16T12:00:00Z', coach_name: 'Test Coach' } as CustomPlan);
      await page.getByRole('button', { name: 'Refresh plans', exact: true }).click();
      const original = JSON.stringify(coachSaved.get(saved.id));
      await page.getByRole('button', { name: 'Create revised draft', exact: true }).click();
      await expect(page.getByLabel('Plan name', { exact: true })).toHaveValue('Dated coach block revision');
      await page.getByRole('button', { name: 'Save draft', exact: true }).click();
      await expect(page.getByText(/Draft saved/)).toBeVisible();
      expect(coachSaved.size).toBe(2);
      page.once('dialog', dialog => dialog.accept());
      await page.getByRole("group", { name: "Plan view" }).getByRole("button", { name: "Week", exact: true }).click();
      await expect(page.getByText('Fight day · Mon 23 Nov 2026')).toBeVisible();
      await page.getByRole('button', { name: 'Save draft', exact: true }).click();
      await expect.poll(() => coachSaved.size).toBe(3);
      expect([...coachSaved.values()].filter(plan => plan.schedule_mode === 'dated')).toHaveLength(2);
      expect(JSON.stringify(coachSaved.get(saved.id))).toBe(original);
    }
    expect(errors).toEqual([]);
  });
}

for (const [latestPlanDate, expected] of [['2026-12-19', 'Sat 19 Dec 2026'], ['', null]] as const) {
  test(`athlete builder uses latest saved plan fight date ${latestPlanDate || 'open camp'}`, async ({ page, baseURL }) => {
    await page.clock.install({ time: new Date('2026-11-16T12:00:00Z') });
    await setup(page, baseURL!, 'athlete', '2026-11-23', latestPlanDate);
    await page.goto('/plans/custom');
    const calendar = page.getByRole('region', { name: 'Plan calendar', exact: true });
    await expect(calendar).toBeVisible();
    if (!expected) {
      await expect(calendar.getByText(/Fight day ·/)).toHaveCount(0);
      return;
    }
    await expect(calendar.getByText(`Fight day · ${expected}`)).toBeVisible();
    await expect(calendar.getByText('Fight day · Mon 23 Nov 2026')).toHaveCount(0);
    await calendar.getByRole('button', { name: 'Month', exact: true }).click();
    await calendar.getByRole('button', { name: 'Next calendar period', exact: true }).click();
    await expect(calendar.locator('[data-fight-day="true"]')).toBeVisible();
    await expect(calendar.getByRole('button', { name: 'Select Sat 19 Dec 2026 (Fight day)' })).toBeDisabled();
  });
}
