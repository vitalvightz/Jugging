"use client";
import { useState } from 'react';
import { addCalendarDays, calendarMonday, calendarWeekday, calendarWeekdays, laterWeeks } from '@/lib/custom-plan-calendar';
import { formatAppDate } from '@/lib/date-format';
import styles from './custom-plan-builder.module.css';

type Session = { id: string; name: string; scheduled_date?: string | null; session_type?: string; exercises: { incomplete?: boolean }[] };
export function CustomPlanCalendar({ sessions, start, end, selected, onSelect, onAdd, onMove, onOpen, onDuplicateWeek, onCopy, onMoveMany, copying, onStartCopying, onStopCopying, exerciseCopy, exerciseMoving, onPasteExercise, onStopExerciseCopy, readOnly = false, fightDate = null, dayLabels }: {
  sessions: Session[]; start: string; end: string; selected: string; onSelect: (day: string) => void;
  readOnly?: boolean; fightDate?: string | null; dayLabels?: Record<string, 'Rest' | 'Off' | 'Recovery'>;
  onAdd?: (day: string) => void; onMove?: (id: string, day: string) => void; onOpen?: (id: string) => void; onDuplicateWeek?: () => void;
  onCopy?: (ids: string[], days: string[]) => void; onMoveMany?: (ids: string[], day: string) => void;
  exerciseCopy?: string; exerciseMoving?: boolean; onPasteExercise?: (day: string) => void; onStopExerciseCopy?: () => void;
  copying?: { ids: string[]; move?: boolean; sessions: Session[] }; onStartCopying?: (ids: string[], move?: boolean) => void; onStopCopying?: () => void;
}) {
  const [view, setView] = useState<'week' | 'month'>('week');
  const fightDay = /^\d{4}-\d{2}-\d{2}$/.test(fightDate ?? '') ? fightDate : null;
  const [cursor, setCursor] = useState<{ day: string; selected: string; range: string } | null>(null);
  const range = `${start}:${end}:${fightDay ?? ''}`;
  const focus = cursor?.selected === selected && cursor.range === range ? cursor.day : selected < start || selected > end ? start : selected;
  const firstDay = fightDay && fightDay < start ? fightDay : start;
  const lastDay = fightDay && fightDay > end ? fightDay : end;
  const month = focus.slice(0, 7);
  const first = view === 'week' ? calendarMonday(focus) : calendarMonday(`${month}-01`);
  const monthEnd = new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 0)).getUTCDate();
  const last = view === 'week' ? addCalendarDays(first, 6) : addCalendarDays(calendarMonday(`${month}-${monthEnd}`), 6);
  const selectedInView = selected >= first && selected <= last;
  const selectedSessions = sessions.filter(s => s.scheduled_date === selected && s.session_type !== 'rest');
  const showSelectedDay = selectedInView && (!readOnly || selectedSessions.length > 0);
  const dates: string[] = [];
  for (let day = first; day <= last; day = addCalendarDays(day, 1)) dates.push(day);
  function navigate(direction: number) {
    const next = view === 'week' ? addCalendarDays(focus, direction * 7) : new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)) - 1 + direction, 1, 12)).toISOString().slice(0, 10);
    const bounded = next < firstDay ? firstDay : next > lastDay ? lastDay : next;
    const inPlan = bounded >= start && bounded <= end;
    setCursor({ day: bounded, selected: inPlan ? bounded : selected, range });
    if (inPlan) onSelect(bounded);
  }
  const label = copying ? (copying.sessions.length === 1 ? copying.sessions[0].name : `${copying.sessions.length} sessions`) : '';
  const from = copying?.sessions[0]?.scheduled_date ?? undefined;
  const selectedIds = sessions.filter(s => s.scheduled_date === selected).map(s => s.id);
  return <section className={styles.datedCalendar} aria-label="Plan calendar" data-readonly={readOnly}>
    <div className={styles.calendarHeader}>
    <div className={styles.calendarToolbar}>
      <div role="group" aria-label="Calendar view">{(['week', 'month'] as const).map(mode => <button type="button" key={mode} aria-pressed={view === mode} onClick={() => { setView(mode); }}>{mode === 'week' ? 'Week' : 'Month'}</button>)}</div>
      <strong>{view === 'week' ? `${formatAppDate(first)} – ${formatAppDate(last)}` : new Date(`${month}-01T12:00:00Z`).toLocaleDateString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' })}</strong>
      <div><button type="button" aria-label="Previous calendar period" disabled={view === 'week' ? first <= calendarMonday(firstDay) : month <= firstDay.slice(0, 7)} onClick={() => navigate(-1)}>Previous</button><button type="button" aria-label="Next calendar period" disabled={view === 'week' ? last >= lastDay : month >= lastDay.slice(0, 7)} onClick={() => navigate(1)}>Next</button></div>
    </div>
    {fightDay ? <p className={styles.calendarFightDate}>Fight day · {formatAppDate(fightDay)}</p> : null}
    {exerciseCopy ? <div className={styles.copyBanner} role="status"><p><strong>{exerciseMoving ? "Moving" : "Copying"} {exerciseCopy}</strong><span>Choose a date to paste.</span></p><div><button type="button" onClick={onStopExerciseCopy}>Done</button></div></div> : null}
    {copying?.sessions.length && onCopy ? <div className={styles.copyBanner} role="status">
      <p><strong>{copying.move ? 'Moving' : 'Copying'} {label}</strong><span>{copying.move ? 'Tap a day to move.' : 'Tap days to paste.'}</span></p>
      <div data-move={copying.move ? 'true' : undefined}>
        {!copying.move ? <><button type="button" disabled={!laterWeeks(from!, end, 1).length} onClick={() => onCopy(copying.ids, laterWeeks(from!, end, 1))}>Next week</button>
        <button type="button" disabled={!laterWeeks(from!, end).length} onClick={() => onCopy(copying.ids, laterWeeks(from!, end))}>Every week</button></> : null}
        <button type="button" className={styles.copyDone} onClick={onStopCopying}>{copying.move ? 'Cancel' : 'Done'}</button>
      </div>
    </div> : null}
    <div className={styles.calendarWeekdays} aria-hidden="true">{calendarWeekdays.map(day => <span className={styles.calendarColumn} key={day}>{day.slice(0, 3)}</span>)}</div>
    </div>
    <div className={styles.calendarGrid} data-view={view} data-copying={Boolean(copying || exerciseCopy)}>
      {dates.map(day => {
        const outside = day < start || day > end;
        const isFightDay = day === fightDay;
        const daily = sessions.filter(s => s.scheduled_date === day);
        const off = readOnly && (daily.length ? daily.every(s => s.session_type === 'rest') : dayLabels?.[day] === 'Rest' || dayLabels?.[day] === 'Off');
        const dayLabel = isFightDay ? 'Fight day' : outside ? '' : off ? dayLabels?.[day] ?? 'Rest' : daily.length ? daily.every(s => s.session_type === 'recovery') ? 'Recovery' : `${daily.length} ${daily.length === 1 ? 'session' : 'sessions'}` : dayLabels?.[day] ?? (readOnly ? 'No session planned' : 'Rest');
        const dateContent = <><span>{day.slice(8).replace(/^0/, '')}</span><small className={isFightDay ? 'cm-cal-fight-label' : undefined}>{dayLabel}</small></>;
        return <div key={day} className={`${styles.calendarDate}${isFightDay ? ' custom-plan-fight-day' : ''}`} data-selected={selected === day} data-outside={outside} data-fight-day={isFightDay} data-rest={Boolean(off) && !outside && !isFightDay}
          onDragOver={event => { if (!readOnly && !outside) event.preventDefault(); }} onDrop={event => { event.preventDefault(); if (!readOnly && !outside) { const id = event.dataTransfer.getData('text/plain'); if (!sessions.some(s => s.id === id)) return; if ((event.altKey || event.ctrlKey || event.metaKey) && onCopy) onCopy([id], [day]); else onMove?.(id, day); } }}>
          {readOnly && (!daily.length || off) ? <span className={styles.dateSelect}>{dateContent}</span> : <button type="button" className={styles.dateSelect} disabled={outside} aria-pressed={selected === day} aria-label={`Select ${formatAppDate(day)}${isFightDay ? ' (Fight day)' : ''}`} onClick={() => { setCursor(null); onSelect(day); }}>{dateContent}</button>}
          {(off ? [] : daily).map(session => <button type="button" key={session.id} className={styles.calendarSession} data-type={session.session_type} data-attention={session.exercises.some(e => e.incomplete) || undefined} draggable={!readOnly}
            onDragStart={event => { event.dataTransfer.effectAllowed = 'copyMove'; event.dataTransfer.setData('text/plain', session.id); }} onClick={() => { setCursor(null); onSelect(day); onOpen?.(session.id); }} aria-label={`${readOnly ? 'View' : 'Open'} ${session.name} on ${formatAppDate(day)}${session.exercises.some(e => e.incomplete) ? ", incomplete prescriptions" : ""}`}><strong>{session.name}</strong><small>{session.exercises.length} {session.exercises.length === 1 ? 'exercise' : 'exercises'}</small></button>)}
          {!readOnly && !outside && exerciseCopy && onPasteExercise ? <button type="button" className={styles.calendarPaste} aria-label={`Paste exercise on ${day}`} onClick={() => onPasteExercise(day)}>Paste</button> : !readOnly && !outside && copying?.sessions.length && onCopy ? (copying.move ? <button type="button" className={styles.calendarPaste} aria-label={`Move to ${day}`} disabled={day === from} onClick={() => onMoveMany?.(copying.ids, day)}>Move here</button> : <button type="button" className={styles.calendarPaste} aria-label={`Paste on ${day}`} onClick={() => onCopy(copying.ids, [day])}>Paste</button>)
            : !readOnly && !outside ? <button type="button" className={styles.calendarAdd} aria-label={`Add session on ${day}`} onClick={() => { onSelect(day); onAdd?.(day); }}>Add session</button> : null}
        </div>;
      })}
    </div>
    {!readOnly ? <div className={styles.calendarFooter}><span>Drag to move. Hold Alt while dropping to copy.</span><div className={styles.dayActions} role="group" aria-label="Selected day actions">
      {onStartCopying ? <><button type="button" disabled={!selectedIds.length} onClick={() => onStartCopying(selectedIds)}>Copy day</button>
      <button type="button" disabled={!selectedIds.length} onClick={() => onStartCopying(selectedIds, true)}>Move day</button></> : null}
      <button type="button" onClick={onDuplicateWeek}>Duplicate week</button></div></div> : null}
    {showSelectedDay ? <p className={styles.selectedCalendarDay}>{formatAppDate(selected)}{selected === fightDay ? ' · Fight day' : ''}</p> : null}
    {showSelectedDay ? <ul className={styles.selectedDaySessions} aria-label="Sessions on selected day">
      {selectedSessions.map(s => <li key={s.id} data-type={s.session_type}>{s.name}<small>{s.exercises.length} {s.exercises.length === 1 ? 'exercise' : 'exercises'}</small></li>)}
    </ul> : null}
  </section>;
}
