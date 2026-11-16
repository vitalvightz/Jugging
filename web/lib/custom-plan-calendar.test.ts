import assert from 'node:assert/strict';
import test from 'node:test';
import { addCalendarDays, calendarMonday, calendarWeekday, copyCalendarSession, datedCopy, duplicateCalendarWeek, nextCalendarMonday, copySessionToDays, copySessionsToDays, moveSessionsToDay, laterWeeks } from './custom-plan-calendar';
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
test('copy mode pastes a session onto chosen days and every later week', () => {
  const end = '2026-12-13';
  assert.deepEqual(laterWeeks('2026-11-16', end), ['2026-11-23', '2026-11-30', '2026-12-07']);
  assert.deepEqual(laterWeeks('2026-11-16', end, 1), ['2026-11-23']);
  const copied = copySessionToDays([session], 'session', ['2026-11-18', ...laterWeeks('2026-11-16', end)], '2026-11-16', end, 336);
  assert.deepEqual(copied.map(s => s.scheduled_date), ['2026-11-16', '2026-11-18', '2026-11-23', '2026-11-30', '2026-12-07']);
  assert.equal(copied[1].weekday, 'Wednesday');
  assert.equal(new Set(copied.flatMap(s => s.exercises.map(e => e.id))).size, 5);
  assert.throws(() => copySessionToDays([session], 'session', ['2027-01-01'], '2026-11-16', end, 336), /inside this plan/);
  assert.throws(() => copySessionToDays([session], 'session', ['2026-11-17', '2026-11-18'], '2026-11-16', end, 2), /at most 2 sessions/);
  const full = Array.from({ length: 28 }, (_, i) => ({ ...session, id: `s${i}` }));
  assert.throws(() => copySessionToDays(full, 's0', ['2026-11-17'], '2026-11-16', end, 336), /28 sessions/);
});
test('a whole day copies or moves together', () => {
  const end = '2026-12-13';
  const day = [session, { ...session, id: 'second', name: 'Conditioning' }];
  const copied = copySessionsToDays(day, day, ['2026-11-20'], '2026-11-16', end, 336);
  assert.deepEqual(copied.map(s => s.scheduled_date), ['2026-11-16', '2026-11-16', '2026-11-20', '2026-11-20']);
  assert.deepEqual(copied.slice(2).map(s => s.name), ['Strength', 'Conditioning']);
  const moved = moveSessionsToDay(day, ['session', 'second'], '2026-11-19', '2026-11-16', end);
  assert.deepEqual(moved.map(s => [s.id, s.scheduled_date, s.weekday]), [['session', '2026-11-19', 'Thursday'], ['second', '2026-11-19', 'Thursday']]);
  assert.throws(() => moveSessionsToDay(day, ['session'], '2027-01-01', '2026-11-16', end), /inside this plan/);
  const full = Array.from({ length: 28 }, (_, i) => ({ ...session, id: `w${i}`, scheduled_date: '2026-11-23' }));
  assert.throws(() => moveSessionsToDay([...full, session], ['session'], '2026-11-24', '2026-11-16', end), /28 sessions/);
});
