"use client";

import {
  CALENDAR_WEEKDAYS,
  calendarCaption,
  type CalendarCellState,
  type CalendarRow,
} from "@/lib/plan-calendar";

const LEGEND: { state: CalendarCellState; label: string }[] = [
  { state: "done", label: "Done" },
  { state: "missed", label: "Missed" },
  { state: "planned", label: "Planned" },
  { state: "coach", label: "Coach / gym" },
  { state: "outside", label: "Outside plan" },
];

/** Whole-block calendar: one row per week, Monday first. Tapping a training
 * day selects it; the caller shows that day's sessions below. */
export function PlanCalendar({
  rows,
  selectedWeekPos,
  selected = null,
  phaseFor,
  onSelectDay,
  allowRestSelection = false,
}: {
  rows: CalendarRow[];
  selectedWeekPos: number;
  /** The day whose sessions are shown below the calendar. */
  selected?: { weekPos: number; dayIndex: number } | null;
  /** Phase (GPP / SPP / Taper) printed under each week label. */
  phaseFor?: (weekPos: number) => string | null;
  onSelectDay: (weekPos: number, dayIndex: number) => void;
  allowRestSelection?: boolean;
}) {
  if (rows.length === 0) {
    return <p className="sp-muted">No days scheduled yet.</p>;
  }
  const caption = calendarCaption(rows);
  return (
    <section className="sp-card cm-calendar">
      <table className="cm-cal-table">
        {/* The month range is printed in the header above. */}
        <caption className="sr-only">{caption ?? "Plan calendar"}</caption>
        <thead>
          <tr>
            <th scope="col" className="cm-cal-week-head">
              <span className="sr-only">Week</span>
            </th>
            {CALENDAR_WEEKDAYS.map((weekday) => (
              <th key={weekday} scope="col" className="cm-cal-weekday">
                <abbr title={weekday}>{weekday.slice(0, 1)}</abbr>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.key}
              className={`cm-cal-row${row.weekPos !== null && row.weekPos === selectedWeekPos ? " cm-cal-row-selected" : ""}`}
            >
              <th scope="row" className="cm-cal-week">
                <span className="cm-cal-week-label">{row.label}</span>
                {row.weekPos !== null && phaseFor?.(row.weekPos) ? (
                  <span className="cm-cal-week-phase">{phaseFor(row.weekPos)}</span>
                ) : null}
              </th>
              {row.cells.map((cell) => {
                const isSelected = selected !== null && cell.weekPos === selected.weekPos && cell.dayIndex === selected.dayIndex;
                const className = `cm-cal-cell${cell.isToday ? " cm-cal-today" : ""}${isSelected ? " cm-cal-selected" : ""}${cell.isFightDay ? " cm-cal-fight-day" : ""}`;
                const content = (
                  <>
                    <span className="cm-cal-num" aria-hidden="true">
                      {cell.dayNumber ?? ""}
                    </span>
                    <span className="cm-cal-dot" data-state={cell.state} aria-hidden="true" />
                    {cell.isFightDay ? <span className="cm-cal-fight-label">Fight day</span> : null}
                    <span className="sr-only">
                      {cell.label}
                      {cell.isToday ? " (today)" : ""}
                    </span>
                  </>
                );
                const openable =
                  cell.weekPos !== null && cell.dayIndex !== null && cell.state !== "outside" && (allowRestSelection || cell.state !== "rest");
                return (
                  <td key={cell.key} data-state={cell.state}>
                    {openable ? (
                      <button
                        type="button"
                        className={className}
                        aria-pressed={isSelected}
                        onClick={() => onSelectDay(cell.weekPos as number, cell.dayIndex as number)}
                      >
                        {content}
                      </button>
                    ) : (
                      <span className={className}>{content}</span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <ul className="cm-cal-legend" aria-label="Calendar key">
        <li><span className="cm-cal-fight-key" aria-hidden="true" />Fight day</li>
        {LEGEND.map((item) => (
          <li key={item.state}>
            <span className="cm-cal-dot" data-state={item.state} aria-hidden="true" />
            {item.label}
          </li>
        ))}
      </ul>
    </section>
  );
}
