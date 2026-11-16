/** Date-only arithmetic: never interpret a training date in the browser's timezone. */
export const calendarWeekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'] as const;
export function addCalendarDays(day: string, amount: number): string {
  const date = new Date(`${day}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + amount);
  return date.toISOString().slice(0, 10);
}
export function calendarWeekday(day: string) { return calendarWeekdays[(new Date(`${day}T12:00:00Z`).getUTCDay() + 6) % 7]; }
export function calendarMonday(day: string) { return addCalendarDays(day, -((new Date(`${day}T12:00:00Z`).getUTCDay() + 6) % 7)); }
export function nextCalendarMonday(timezone?: string | null, now = new Date()) {
  // Profiles can hold a blank or unknown timezone; Intl throws on those, so use the device's instead.
  const format = (timeZone?: string) => new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now);
  let parts: Intl.DateTimeFormatPart[];
  try { parts = format(timezone || undefined); } catch { parts = format(); }
  const part = (type: string) => parts.find(p => p.type === type)!.value;
  const today = `${part('year')}-${part('month')}-${part('day')}`;
  const monday = calendarMonday(today);
  return monday === today ? today : addCalendarDays(monday, 7);
}
export type CalendarSession = { id: string; weekday?: string | null; scheduled_date?: string | null; exercises: { id: string }[] };
export function copyCalendarSession<T extends CalendarSession>(session: T, day: string): T {
  return { ...session, id: crypto.randomUUID(), scheduled_date: day, weekday: calendarWeekday(day), exercises: session.exercises.map(e => ({ ...e, id: crypto.randomUUID() })) };
}
export function datedCopy<T extends CalendarSession>(sessions: T[], start: string, end: string): T[] {
  const result: T[] = [];
  for (let date = start; date <= end; date = addCalendarDays(date, 1))
    for (const session of sessions.filter(s => s.weekday === calendarWeekday(date))) result.push(copyCalendarSession(session, date));
  return result;
}
export function duplicateCalendarWeek<T extends CalendarSession>(sessions: T[], selected: string, start: string, end: string): T[] {
  const monday = calendarMonday(selected), sunday = addCalendarDays(monday, 6);
  const copied = sessions.filter(s => s.scheduled_date && s.scheduled_date >= monday && s.scheduled_date <= sunday)
    .map(s => copyCalendarSession(s, addCalendarDays(s.scheduled_date!, 7)));
  if (!copied.length) throw new Error('This week has no sessions to copy.');
  if (copied.some(s => s.scheduled_date! < start || s.scheduled_date! > end)) throw new Error('Extend the plan range before copying this week.');
  const result = [...sessions, ...copied];
  const counts = new Map<string, number>();
  for (const s of result) if (s.scheduled_date) { const week = calendarMonday(s.scheduled_date); counts.set(week, (counts.get(week) ?? 0) + 1); }
  if ([...counts.values()].some(n => n > 28)) throw new Error('A week can contain at most 28 sessions.');
  return result;
}
