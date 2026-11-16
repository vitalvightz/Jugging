"use client";
import { useState } from 'react';
import { addCalendarDays, calendarMonday, calendarWeekday, calendarWeekdays, laterWeeks } from '@/lib/custom-plan-calendar';
import { formatAppDate } from '@/lib/date-format';
import styles from './custom-plan-builder.module.css';

type Session = { id: string; name: string; scheduled_date?: string | null; session_type?: string; exercises: { incomplete?: boolean }[] };
export function CustomPlanCalendar({ sessions, start, end, selected, onSelect, onAdd, onMove, onOpen, onDuplicateWeek, onCopy, copying, onStopCopying }: {
  sessions: Session[]; start: string; end: string; selected: string; onSelect: (day: string) => void;
  onAdd: (day: string) => void; onMove: (id: string, day: string) => void; onOpen: (id: string) => void; onDuplicateWeek: () => void;
  onCopy?: (id: string, days: string[]) => void; copying?: Session; onStopCopying?: () => void;
}) {
  const [view, setView] = useState<'week' | 'month'>('week');
  const focus = selected < start || selected > end ? start : selected;
  const month = focus.slice(0, 7);
  const first = view === 'week' ? calendarMonday(focus) : calendarMonday(`${month}-01`);
  const monthEnd = new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 0)).getUTCDate();
  const last = view === 'week' ? addCalendarDays(first, 6) : addCalendarDays(calendarMonday(`${month}-${monthEnd}`), 6);
  const dates: string[] = [];
  for (let day = first; day <= last; day = addCalendarDays(day, 1)) dates.push(day);
  function navigate(direction: number) {
    const next = view === 'week' ? addCalendarDays(focus, direction * 7) : new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)) - 1 + direction, 1, 12)).toISOString().slice(0, 10);
    const bounded = next < start ? start : next > end ? end : next;
    onSelect(bounded);
  }
  return <section className={styles.datedCalendar} aria-label="Plan calendar">
    <div className={styles.calendarToolbar}>
      <div role="group" aria-label="Calendar view">{(['week', 'month'] as const).map(mode => <button type="button" key={mode} aria-pressed={view === mode} onClick={() => { setView(mode); }}>{mode === 'week' ? 'Week' : 'Month'}</button>)}</div>
      <strong>{view === 'week' ? `${formatAppDate(first)} – ${formatAppDate(last)}` : new Date(`${month}-01T12:00:00Z`).toLocaleDateString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' })}</strong>
      <div><button type="button" aria-label="Previous calendar period" disabled={view === 'week' ? first <= calendarMonday(start) : month <= start.slice(0, 7)} onClick={() => navigate(-1)}>Previous</button><button type="button" aria-label="Next calendar period" disabled={view === 'week' ? last >= end : month >= end.slice(0, 7)} onClick={() => navigate(1)}>Next</button></div>
    </div>
    {copying && onCopy ? <div className={styles.copyBanner} role="status">
      <p><strong>Copying {copying.name}</strong><span>Tap days to paste.</span></p>
      <div>
        <button type="button" disabled={!laterWeeks(copying.scheduled_date!, end, 1).length} onClick={() => onCopy(copying.id, laterWeeks(copying.scheduled_date!, end, 1))}>Next week</button>
        <button type="button" disabled={!laterWeeks(copying.scheduled_date!, end).length} onClick={() => onCopy(copying.id, laterWeeks(copying.scheduled_date!, end))}>Every week</button>
        <button type="button" className={styles.copyDone} onClick={onStopCopying}>Done</button>
      </div>
    </div> : null}
    <div className={styles.calendarGrid} data-view={view} data-copying={Boolean(copying)}>
      {calendarWeekdays.map(day => <span className={styles.calendarColumn} key={day} aria-hidden="true">{day.slice(0, 3)}</span>)}
      {dates.map(day => {
        const outside = day < start || day > end;
        const daily = sessions.filter(s => s.scheduled_date === day);
        return <div key={day} className={styles.calendarDate} data-selected={selected === day} data-outside={outside}
          onDragOver={event => { if (!outside) event.preventDefault(); }} onDrop={event => { event.preventDefault(); if (!outside) { const id = event.dataTransfer.getData('text/plain'); if (!sessions.some(s => s.id === id)) return; if ((event.altKey || event.ctrlKey || event.metaKey) && onCopy) onCopy(id, [day]); else onMove(id, day); } }}>
          <button type="button" className={styles.dateSelect} disabled={outside} aria-pressed={selected === day} aria-label={`Select ${formatAppDate(day)}`} onClick={() => { onSelect(day); }}><span>{day.slice(8).replace(/^0/, '')}</span><small>{daily.length ? `${daily.length} ${daily.length === 1 ? 'session' : 'sessions'}` : 'Rest'}</small></button>
          {daily.map(session => <button type="button" key={session.id} className={styles.calendarSession} data-type={session.session_type} draggable
            onDragStart={event => { event.dataTransfer.effectAllowed = 'copyMove'; event.dataTransfer.setData('text/plain', session.id); }} onClick={() => { onSelect(day); onOpen(session.id); }} aria-label={`Open ${session.name} on ${formatAppDate(day)}`}><strong>{session.name}</strong><small>{session.exercises.length} {session.exercises.length === 1 ? 'exercise' : 'exercises'}{session.exercises.some(e => e.incomplete) ? ' · Finish dose' : ''}</small></button>)}
          {!outside && copying && onCopy ? <button type="button" className={styles.calendarPaste} aria-label={`Paste on ${day}`} onClick={() => onCopy(copying.id, [day])}>Paste</button>
            : !outside ? <button type="button" className={styles.calendarAdd} aria-label={`Add session on ${day}`} onClick={() => { onSelect(day); onAdd(day); }}>Add session</button> : null}
        </div>;
      })}
    </div>
    <div className={styles.calendarFooter}><span>Drag to move. Hold Alt while dropping to copy.</span><button type="button" onClick={onDuplicateWeek}>Duplicate week</button></div>
    <p className={styles.selectedCalendarDay}>{formatAppDate(selected)}</p>
    {view === 'month' ? <ul className={styles.selectedDaySessions} aria-label="Sessions on selected day">
      {sessions.filter(s => s.scheduled_date === selected).map(s => <li key={s.id} data-type={s.session_type}>{s.name}<small>{s.exercises.length} {s.exercises.length === 1 ? 'exercise' : 'exercises'}</small></li>)}
    </ul> : null}
  </section>;
}
