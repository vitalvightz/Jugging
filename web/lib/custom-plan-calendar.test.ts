import assert from 'node:assert/strict';
import test from 'node:test';
import { addCalendarDays, calendarMonday, calendarWeekday, copyCalendarSession, datedCopy, duplicateCalendarWeek, nextCalendarMonday } from './custom-plan-calendar';
const session = { id: 'session', name: 'Strength', weekday: 'Monday', scheduled_date: '2026-11-16', exercises: [{ id: 'exercise', dose: { sets: 3 } }] };
test('date-only calendar arithmetic handles month/year boundaries without timezone shifts', () => {
  assert.equal(addCalendarDays('2026-12-31', 1), '2027-01-01');
  assert.equal(calendarMonday('2027-01-01'), '2026-12-28');
  assert.equal(calendarWeekday('2027-01-01'), 'Friday');
  assert.equal(nextCalendarMonday('Pacific/Auckland', new Date('2026-11-15T12:30:00Z')), '2026-11-16');
});
test('a blank or unknown profile timezone still yields a start date instead of throwing', () => {
  for (const timezone of ['', null, undefined, 'Mars/Phobos']) assert.match(nextCalendarMonday(timezone, new Date('2026-11-18T12:00:00Z')), /^2026-11-23$/);
});
test('copying creates new occurrence IDs and preserves prescription content', () => {
  const copied = copyCalendarSession(session, '2026-11-24');
  assert.notEqual(copied.id, session.id); assert.notEqual(copied.exercises[0].id, session.exercises[0].id);
  assert.equal(copied.weekday, 'Tuesday'); assert.deepEqual(copied.exercises[0].dose, session.exercises[0].dose);
  assert.equal(session.scheduled_date, '2026-11-16');
});
test('weekly conversion creates only matching dates with independently identified exercises', () => {
  const converted = datedCopy([session], '2026-11-16', '2026-12-13');
  assert.deepEqual(converted.map(s => s.scheduled_date), ['2026-11-16', '2026-11-23', '2026-11-30', '2026-12-07']);
  assert.equal(new Set(converted.map(s => s.exercises[0].id)).size, 4);
});
test('week duplication retains originals and adds a separate week without overwriting', () => {
  const result = duplicateCalendarWeek([session], '2026-11-18', '2026-11-16', '2026-11-29');
  assert.equal(result.length, 2); assert.equal(result[1].scheduled_date, '2026-11-23'); assert.equal(result[0], session);
  assert.throws(() => duplicateCalendarWeek([session], '2026-11-16', '2026-11-16', '2026-11-22'), /Extend/);
});
