import { expect, test } from '@playwright/test';
import { setup } from './custom-plan-fixture';
import { blankDose } from '../lib/custom-plan-dose';
import type { CustomPlan } from '../lib/api-schema.generated';

test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: 'wait' }); });
for (const [role, width] of [['athlete', 320], ['athlete', 390], ['athlete', 430], ['athlete', 1280], ['coach', 390]] as const) {
  test(`copy and cut exercises and sessions across the calendar (${role}, ${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.clock.install({ time: new Date('2026-11-18T12:00:00Z') });
    const { saved } = await setup(page, baseURL!, role);
    const originalId = crypto.randomUUID();
    const plan: CustomPlan = { id: crypto.randomUUID(), name: 'Copy practice', kind: 'self_directed', schedule_mode: 'dated', start_date: '2026-11-23', end_date: '2026-12-20', incomplete: false, created_at: '2026-11-18T12:00:00Z', updated_at: '2026-11-18T12:00:00Z', sessions: [
      { id: crypto.randomUUID(), name: 'Strength', weekday: 'Monday', scheduled_date: '2026-11-23', session_type: 'strength_power', exercises: [{ id: originalId, origin: 'manual', name: 'Squat', exercise_type: 'strength', dose: { ...blankDose(), sets: 3, reps: 5 }, equipment: null, notes: null, media_id: null, community_id: null, community_revision_id: null, incomplete: false, coaching_cues: null, stop_rules: null }] },
      ...['Boxing', 'Conditioning'].map(name => ({ id: crypto.randomUUID(), name, weekday: 'Tuesday' as const, scheduled_date: '2026-11-24', session_type: 'mixed' as const, exercises: [] })),
    ] };
    saved.set(plan.id, plan);
    if (role === 'coach') {
      const fighterId = '00000000-0000-4000-8000-000000000003';
      await page.route('**/rest/v1/rpc/**', route => route.fulfill({ json: new URL(route.request().url()).pathname.endsWith('coach_fighters') ? [{ id: fighterId, connection_id: 'connection', name: 'Test Fighter', today: '2026-11-18', timezone: 'Europe/London', plan_id: null, plan_name: null, fight_date: null, sport: 'boxing', share_recovery: false, share_injuries: false, share_weight: false, sessions: [], schedule: [], recovery: null, injuries: null }] : [] }));
      await page.route('**/api/coach/fighters/*/training/plan**', route => route.fulfill({ json: [] }));
      await page.route('**/api/coach/fighters/*/plans**', route => {
        const request = route.request();
        if (request.method() !== 'GET') saved.set(plan.id, { ...plan, ...request.postDataJSON() });
        const current = { ...saved.get(plan.id)!, fighter_id: fighterId, assignment_status: 'draft', coach_name: null, assigned_at: null, published_plan_id: null };
        return route.fulfill({ json: new URL(request.url()).pathname.endsWith('/plans') ? [current] : current });
      });
      await page.goto('/coach/plans');
      await page.getByRole('button', { name: 'Edit draft', exact: true }).click();
    } else await page.goto(`/plans/custom?plan=${plan.id}`);
    const calendar = page.getByRole('region', { name: 'Plan calendar', exact: true });
    await page.getByRole('button', { name: 'Copy Squat to calendar', exact: true }).click();
    await expect(calendar.getByRole('status')).toContainText('Copying Squat');
    await expect(page.getByRole('button', { name: 'Copy Squat to calendar', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(calendar.getByRole('status')).toBeInViewport({ ratio: 1 });
    const target = calendar.getByRole('button', { name: 'Paste exercise on 2026-11-24', exact: true });
    await expect(target).toBeInViewport({ ratio: 1 });
    expect(await target.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    expect(await target.evaluate(node => { const rect = node.getBoundingClientRect(); return node.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)); })).toBe(true);
    if (process.env.CUSTOM_QA_DIR) await page.screenshot({ path: `${process.env.CUSTOM_QA_DIR}/exercise-copy-${role}-${width}.png`, animations: 'disabled' });
    await calendar.getByRole('button', { name: 'Paste exercise on 2026-11-24', exact: true }).click();
    const chooser = page.getByRole('dialog', { name: 'Choose a session', exact: true });
    await expect(chooser.getByRole('button', { name: /Boxing/ })).toBeVisible();
    await expect(chooser.getByRole('button', { name: /Conditioning/ })).toBeVisible();
    if (process.env.CUSTOM_QA_DIR) await page.screenshot({ path: `${process.env.CUSTOM_QA_DIR}/exercise-destination-${role}-${width}.png`, animations: 'disabled' });
    await chooser.getByRole('button', { name: /Boxing/ }).click();
    await expect(page.getByRole('region', { name: 'Session 2' })).toContainText('Squat');
    await calendar.getByRole('button', { name: 'Paste exercise on 2026-11-25', exact: true }).click();
    await expect(page.getByRole('region', { name: 'Session 4' })).toContainText('Squat');
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: /Draft saved\.|Plan saved\./ })).toBeVisible();
    const movedId = saved.get(plan.id)!.sessions.find(s => s.scheduled_date === '2026-11-25')!.exercises[0].id;
    await page.getByRole('button', { name: 'Cut Squat to calendar', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Cut Squat to calendar', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByRole('button', { name: 'Copy Squat to calendar', exact: true })).toHaveAttribute('aria-pressed', 'false');
    await expect(page.getByRole('region', { name: 'Session 4' })).toContainText('Squat');
    await page.getByRole('button', { name: 'Edit dose for Squat', exact: true }).click();
    const editor = page.getByRole('dialog', { name: 'Squat', exact: true });
    await editor.getByLabel('Reps', { exact: true }).fill('7');
    await editor.getByRole('button', { name: 'Done', exact: true }).click();
    await calendar.getByRole('button', { name: 'Paste exercise on 2026-11-24', exact: true }).click();
    await page.keyboard.press('Escape');
    await expect(chooser).toHaveCount(0);
    await expect(calendar.getByRole('status')).toContainText('Moving Squat');
    await expect(page.getByRole('region', { name: 'Session 4' })).toContainText('Squat');
    await calendar.getByRole('button', { name: 'Paste exercise on 2026-11-24', exact: true }).click();
    await chooser.getByRole('button', { name: /Boxing/ }).click();
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: /Draft saved\.|Plan saved\./ })).toBeVisible();
    const stored = saved.get(plan.id)!;
    expect(stored.sessions.find(s => s.name === 'Strength')!.exercises[0].id).toBe(originalId);
    const copied = stored.sessions.find(s => s.name === 'Boxing')!.exercises;
    expect(copied).toHaveLength(2);
    expect(new Set([originalId, ...copied.map(e => e.id)]).size).toBe(3);
    expect(copied.map(e => e.id)).toContain(movedId);
    expect(stored.sessions.find(s => s.scheduled_date === '2026-11-25')!.exercises).toHaveLength(0);
    expect(copied.map(e => e.dose.reps)).toEqual([5, 7]);
    await calendar.getByRole('button', { name: /Select Mon 23 Nov 2026/ }).click();
    await page.getByRole('button', { name: 'Cut session Strength', exact: true }).click();
    await expect(calendar.getByRole('status')).toContainText('Moving Strength');
    await calendar.getByRole('button', { name: 'Move to 2026-11-26', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Strength', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Undo', exact: true }).click();
    await calendar.getByRole('button', { name: /Select Mon 23 Nov 2026/ }).click();
    await expect(page.getByRole('region', { name: 'Session 1' })).toContainText('Strength');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test('weekly plans can copy exercises and cut sessions to another day', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  const { saved } = await setup(page, baseURL!);
  const plan: CustomPlan = { id: crypto.randomUUID(), name: 'Weekly practice', kind: 'self_directed', schedule_mode: 'weekly', start_date: null, end_date: null, incomplete: false, created_at: '2026-11-18T12:00:00Z', updated_at: '2026-11-18T12:00:00Z', sessions: [
    { id: crypto.randomUUID(), name: 'Strength', weekday: 'Monday', scheduled_date: null, session_type: 'strength_power', exercises: [{ id: crypto.randomUUID(), origin: 'manual', name: 'Squat', exercise_type: 'strength', dose: { ...blankDose(), sets: 3, reps: 5 }, equipment: null, notes: null, media_id: null, community_id: null, community_revision_id: null, incomplete: false, coaching_cues: null, stop_rules: null }] },
    { id: crypto.randomUUID(), name: 'Boxing', weekday: 'Tuesday', scheduled_date: null, session_type: 'mixed', exercises: [] },
  ] };
  saved.set(plan.id, plan);
  await page.goto(`/plans/custom?plan=${plan.id}`);
  await page.getByRole('button', { name: 'Copy Squat to calendar', exact: true }).click();
  await page.getByRole('button', { name: 'Paste Squat on Tuesday', exact: true }).click();
  await page.getByRole('dialog', { name: 'Choose a session' }).getByRole('button', { name: /Boxing/ }).click();
  await expect(page.getByRole('region', { name: 'Session 2' })).toContainText('Squat');
  await page.getByRole('status').filter({ hasText: 'Copying Squat' }).getByRole('button', { name: 'Done' }).click();
  await page.getByRole('button', { name: 'Cut Squat to calendar', exact: true }).click();
  await page.getByRole('button', { name: 'Paste Squat on Wednesday', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Session 3' })).toContainText('Squat');
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  await page.getByRole('button', { name: /^Tuesday,/ }).click();
  await expect(page.getByRole('region', { name: 'Session 2' })).toContainText('Squat');
  await page.getByRole('button', { name: /^Monday,/ }).click();
  await page.getByRole('button', { name: 'Cut session Strength', exact: true }).click();
  await page.getByRole('button', { name: 'Move session to Friday', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Strength', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  await page.getByRole('button', { name: /^Monday,/ }).click();
  await expect(page.getByRole('region', { name: 'Session 1' })).toContainText('Strength');
});

test('a full session cannot accept a cut exercise and cancelling preserves it', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.clock.install({ time: new Date('2026-11-18T12:00:00Z') });
  const { saved } = await setup(page, baseURL!);
  const exercise = { id: crypto.randomUUID(), origin: 'manual' as const, name: 'Squat', exercise_type: 'strength' as const, dose: { ...blankDose(), sets: 3, reps: 5 }, equipment: null, notes: null, media_id: null, community_id: null, community_revision_id: null, incomplete: false, coaching_cues: null, stop_rules: null };
  const plan: CustomPlan = { id: crypto.randomUUID(), name: 'Capacity check', kind: 'self_directed', schedule_mode: 'dated', start_date: '2026-11-23', end_date: '2026-12-20', incomplete: false, created_at: '2026-11-18T12:00:00Z', updated_at: '2026-11-18T12:00:00Z', sessions: [
    { id: crypto.randomUUID(), name: 'Source', weekday: 'Monday', scheduled_date: '2026-11-23', session_type: 'strength_power', exercises: [exercise] },
    { id: crypto.randomUUID(), name: 'Full session', weekday: 'Tuesday', scheduled_date: '2026-11-24', session_type: 'strength_power', exercises: Array.from({ length: 100 }, (_, i) => ({ ...exercise, id: crypto.randomUUID(), name: `Movement ${i + 1}` })) },
  ] };
  saved.set(plan.id, plan);
  await page.goto(`/plans/custom?plan=${plan.id}`);
  await page.getByRole('button', { name: 'Cut Squat to calendar', exact: true }).click();
  await page.getByRole('button', { name: 'Paste exercise on 2026-11-24', exact: true }).click();
  const chooser = page.getByRole('dialog', { name: 'Choose a session' });
  await expect(chooser.getByRole('button', { name: /Full session/ })).toBeDisabled();
  await page.keyboard.press('Escape');
  await page.getByRole('status').filter({ hasText: 'Moving Squat' }).getByRole('button', { name: 'Done' }).click();
  await expect(page.getByRole('region', { name: 'Session 1' })).toContainText('Squat');
  expect(saved.get(plan.id)!.sessions[0].exercises[0].id).toBe(exercise.id);
});
