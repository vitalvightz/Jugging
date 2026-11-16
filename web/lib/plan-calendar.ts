import {
  completionForSession,
  dayCompletion,
  getSessionDisplayStatus,
  type CompletionIndex,
} from "@/lib/camp-map";
import { formatAppDate } from "@/lib/date-format";
import {
  cleanText,
  getDays,
  getPhysicalSessions,
  getSessions,
  isCoachLedPhysicalTrainingDay,
  isPlainRestDay,
} from "@/lib/structured-plan";
import type { StructuredDay, StructuredWeek } from "@/lib/types";

// ---------------------------------------------------------------------------
// Block calendar: lays a plan's days out on a Monday-first grid so the whole
// block reads at a glance. Dated plans (fight camps, projected open plans) use
// their real dates; legacy weekday-only open plans get one row per plan week,
// placed by weekday and dated from the server anchor when there is one.
// ---------------------------------------------------------------------------

export type CalendarCellState = "done" | "missed" | "planned" | "coach" | "rest" | "outside";

export type CalendarCell = {
  key: string;
  /** Calendar date of the cell, or null for an undated weekday-only slot. */
  iso: string | null;
  /** Day of month to print, or null when the cell has no date. */
  dayNumber: number | null;
  /** 0 = Monday … 6 = Sunday. */
  column: number;
  /** Plan week/day this cell opens, or null for a gap/outside cell. */
  weekPos: number | null;
  dayIndex: number | null;
  state: CalendarCellState;
  isToday: boolean;
  isFightDay?: boolean;
  /** Screen-reader description ("Tue 27 Oct 2026: app session planned"). */
  label: string;
};

export type CalendarRow = {
  key: string;
  /** Plan week this row belongs to (the first plan day in it), if any. */
  weekPos: number | null;
  label: string;
  cells: CalendarCell[];
};

export type PlanCalendarOptions = {
  completionIndex?: CompletionIndex;
  /** Server-authoritative athlete-local day (YYYY-MM-DD). */
  currentDayIso?: string | null;
  /** Day the plan was created; earlier sessions are never Missed. */
  planStartIso?: string | null;
  /** Monday the open block runs from (schedule_context.anchor_date). */
  anchorIso?: string | null;
  /** Plan week holding today, for undated weekday-only plans. */
  currentWeekPos?: number | null;
};

export const CALENDAR_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const DAY_MS = 86_400_000;

function isoOf(value: string | null | undefined): string | null {
  const iso = cleanText(value ?? null)?.slice(0, 10);
  return iso && ISO_DAY.test(iso) ? iso : null;
}

/** Local-noon Date for an ISO day, so day arithmetic is DST-safe. */
function noon(iso: string): Date {
  return new Date(`${iso}T12:00:00`);
}

function toIso(date: Date): string {
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function addDays(iso: string, days: number): string {
  return toIso(new Date(noon(iso).getTime() + days * DAY_MS));
}

/** 0 = Monday … 6 = Sunday. */
function columnOf(iso: string): number {
  return (noon(iso).getDay() + 6) % 7;
}

function weekdayColumn(day: StructuredDay): number | null {
  const token = cleanText(day.weekday)?.slice(0, 3).toLowerCase();
  if (!token) {
    return null;
  }
  const column = CALENDAR_WEEKDAYS.findIndex((name) => name.toLowerCase() === token);
  return column >= 0 ? column : null;
}

function weekLabelFor(week: StructuredWeek | undefined, weekPos: number): string {
  const index =
    typeof week?.week_index === "number" && Number.isFinite(week.week_index)
      ? week.week_index
      : weekPos + 1;
  return `W${index}`;
}

const STATE_COPY: Record<CalendarCellState, string> = {
  done: "app session done",
  missed: "app session missed",
  planned: "app session planned",
  coach: "coach/gym day",
  rest: "rest",
  outside: "outside the plan",
};

/** What one plan day looks like on the calendar. App work wins over a coach
 * day; live completion rows win over the plan's static statuses, and a day is
 * only ever called missed once completions are loaded (mirrors the day cards). */
export function calendarDayState(
  day: StructuredDay,
  iso: string | null,
  options: PlanCalendarOptions = {},
): CalendarCellState {
  const physical = getPhysicalSessions(day);
  if (physical.length > 0) {
    const completion = dayCompletion(day, options.completionIndex, { includeCoachLed: false });
    if (completion.total > 0 && completion.done >= completion.total) {
      return "done";
    }
    const index = options.completionIndex;
    if (index) {
      const missed = physical.some((session) => {
        const state = getSessionDisplayStatus(
          completionForSession(index, day, session),
          iso,
          options.currentDayIso ?? null,
          options.planStartIso ?? null,
        ).state;
        return state === "missed" || state === "skipped";
      });
      if (missed) {
        return "missed";
      }
    }
    return "planned";
  }
  if (isCoachLedPhysicalTrainingDay(day)) {
    return "coach";
  }
  // Support-only work (a visualisation, a tactical review) is still something
  // to open, so it reads as planned rather than rest.
  return getSessions(day).length > 0 || !isPlainRestDay(day) ? "planned" : "rest";
}

function describe(iso: string | null, fallback: string, state: CalendarCellState): string {
  const date = iso ? formatAppDate(iso) : fallback;
  return `${date}: ${STATE_COPY[state]}`;
}

/** Rows of the block calendar for the given plan weeks. */
export function buildPlanCalendar(
  weeks: StructuredWeek[],
  options: PlanCalendarOptions = {},
): CalendarRow[] {
  const placed = weeks.flatMap((week, weekPos) =>
    getDays(week).map((day, dayIndex) => ({ day, weekPos, dayIndex, iso: isoOf(day.date) })),
  );
  if (placed.length === 0) {
    return [];
  }
  const today = isoOf(options.currentDayIso);

  // Dated plan: a true calendar from the Monday before the first day to the
  // Sunday after the last, with in-between days the plan skips shown as rest.
  if (placed.every((entry) => entry.iso)) {
    const byIso = new Map(placed.map((entry) => [entry.iso as string, entry]));
    const sorted = [...byIso.keys()].sort();
    const first = sorted[0];
    const last = sorted[sorted.length - 1];
    const start = addDays(first, -columnOf(first));
    const end = addDays(last, 6 - columnOf(last));
    const rows: CalendarRow[] = [];
    for (let rowStart = start; rowStart <= end; rowStart = addDays(rowStart, 7)) {
      const cells: CalendarCell[] = [];
      let rowWeekPos: number | null = null;
      for (let column = 0; column < 7; column += 1) {
        const iso = addDays(rowStart, column);
        const entry = byIso.get(iso);
        if (entry && rowWeekPos === null) {
          rowWeekPos = entry.weekPos;
        }
        const state: CalendarCellState = entry
          ? calendarDayState(entry.day, iso, options)
          : iso >= first && iso <= last
            ? "rest"
            : "outside";
        cells.push({
          key: iso,
          iso,
          dayNumber: noon(iso).getDate(),
          column,
          weekPos: entry?.weekPos ?? null,
          dayIndex: entry?.dayIndex ?? null,
          state,
          isToday: iso === today,
          isFightDay: entry?.day.day_type === "competition" || /^D[-−]0$/i.test(entry?.day.countdown_label?.trim() ?? ""),
          label: describe(iso, iso, state),
        });
      }
      rows.push({
        key: rowStart,
        weekPos: rowWeekPos,
        label: rowWeekPos !== null ? weekLabelFor(weeks[rowWeekPos], rowWeekPos) : "",
        cells,
      });
    }
    return rows;
  }

  // Weekday-only plan: one row per plan week, each day in its weekday column.
  const anchor = isoOf(options.anchorIso);
  const todayColumn = today ? columnOf(today) : null;
  return weeks.map((week, weekPos) => {
    const byColumn = new Map<number, { day: StructuredDay; dayIndex: number }>();
    getDays(week).forEach((day, dayIndex) => {
      const own = isoOf(day.date);
      const column = own ? columnOf(own) : weekdayColumn(day);
      if (column !== null && !byColumn.has(column)) {
        byColumn.set(column, { day, dayIndex });
      }
    });
    const label = weekLabelFor(week, weekPos);
    const cells = CALENDAR_WEEKDAYS.map((weekday, column): CalendarCell => {
      const entry = byColumn.get(column);
      const iso =
        (entry ? isoOf(entry.day.date) : null) ?? (anchor ? addDays(anchor, weekPos * 7 + column) : null);
      const state: CalendarCellState = entry ? calendarDayState(entry.day, isoOf(entry.day.date), options) : "rest";
      const isToday = iso
        ? iso === today
        : options.currentWeekPos === weekPos && todayColumn === column;
      return {
        key: `${weekPos}-${column}`,
        iso,
        dayNumber: iso ? noon(iso).getDate() : null,
        column,
        weekPos: entry ? weekPos : null,
        dayIndex: entry?.dayIndex ?? null,
        state,
        isToday,
        isFightDay: entry?.day.day_type === "competition" || /^D[-−]0$/i.test(entry?.day.countdown_label?.trim() ?? ""),
        label: describe(iso, `Week ${weekPos + 1} ${weekday}`, state),
      };
    });
    return { key: `week-${weekPos}`, weekPos, label, cells };
  });
}

/** "Oct – Nov 2026" style caption for a dated calendar, or null. */
export function calendarCaption(rows: CalendarRow[]): string | null {
  const dated = rows.flatMap((row) => row.cells).filter((cell) => cell.iso && cell.state !== "outside");
  if (dated.length === 0) {
    return null;
  }
  const monthYear = (iso: string) => {
    const [, , month, year] = formatAppDate(iso).split(" ");
    return { month, year };
  };
  const first = monthYear(dated[0].iso as string);
  const last = monthYear(dated[dated.length - 1].iso as string);
  if (first.year !== last.year) {
    return `${first.month} ${first.year} – ${last.month} ${last.year}`;
  }
  return first.month === last.month ? `${first.month} ${first.year}` : `${first.month} – ${last.month} ${first.year}`;
}
