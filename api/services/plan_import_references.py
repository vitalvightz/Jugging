"""References to earlier training ("Same as Day 1", "Same workout as Nov 20",
"Repeat ankle + hip drills"): resolved into copies of the exercises they name,
with any exact written progression applied. Never guessed, never an exercise."""
from __future__ import annotations

import re
from datetime import date, timedelta



from api.services.plan_import_progression import apply_progression, read_progression
from api.services.plan_import_reading import (
    ReadExercise, ReadPlan, ReadSession, _clean, _days, _words, _written_dose, session_days,
)



def _names_session(target: str, session: ReadSession) -> bool:
    """Does "Day 1", "Monday" or "Session A" name this session?"""
    label = " ".join(re.findall(r"[a-z0-9]+", target.lower()))
    if not label:
        return False
    if _days(target):
        return set(_days(target)) <= set(session_days(session))
    return re.search(rf"(?<![a-z0-9]){re.escape(label)}(?![a-z0-9])", " ".join(re.findall(r"[a-z0-9]+", (session.name or "").lower()))) is not None


def resolve_references(read: ReadPlan, flag, *, before_copy=None) -> ReadPlan:
    """Replace each reference to earlier training ("Same as Day 1", "Warm-up: same
    as Day 1", "Repeat ankle + hip drills") with copies of the exercises it names,
    from the source programme as read. A change written with it ("increase reps
    slightly if easier") is kept on each copy as a progression note, never applied
    as numbers. A reference that names nothing, or more than one thing, is flagged
    and nothing is copied: it is never guessed and never becomes an exercise."""
    read = read.model_copy(deep=True)
    order = [s for week in read.weeks for s in week.sessions] + list(read.every_week)
    for session in order:
        for item in session.exercises:
            _implied_reference(item)
    if before_copy:
        before_copy(read)
    # Each session's week (every-week sessions count as after the last week).
    week_of = [n for n, week in enumerate(read.weeks) for _ in week.sessions] + [len(read.weeks)] * len(read.every_week)
    if not any(e.is_reference for s in order for e in s.exercises):
        return read
    for position, session in enumerate(order):
        if not any(e.is_reference for e in session.exercises):
            continue
        where = (f"Week {week_of[position] + 1} \u00b7 " if len(read.weeks) > 1 and week_of[position] < len(read.weeks) else "") + (_clean(session.name) or "Session")
        resolved: list[ReadExercise] = []
        for item in session.exercises:
            if not item.is_reference:
                resolved.append(item)
                continue
            written = _clean(item.name) or _clean(item.refers_to_day) or "the reference"
            target, section = _clean(item.refers_to_day), _clean(item.refers_to_section)
            earlier = order[:position][::-1]  # nearest first
            week_target = re.search(r"\bweek\s*(\d{1,2})\b", target or "", re.I)
            when = _written_date(target, read)
            if target and target.lower() in {"above", "as above", "previous", "the previous day", "last session"}:
                sources = earlier[:1]
            elif week_target or when or (target or "").lower() in SAME_SLOT or (not target and not section):
                # "as Week 1", "as Nov 20", or this session repeated from an earlier week.
                if when:
                    pool = [s for i, s in enumerate(order) if i != position and _session_date(s, read) == when]
                else:
                    weeks = [int(week_target.group(1)) - 1] if week_target else list(range(week_of[position] - 1, -1, -1))
                    pool = []
                    for week in weeks:
                        pool = [s for i, s in enumerate(order) if week_of[i] == week and i != position and _same_slot(session, s)]
                        if pool:
                            break
                if len(pool) > 1:
                    flag(where, f"\u201c{_clean(written, 60)}\u201d matches more than one earlier session. Nothing copied.")
                    continue
                sources, target = pool, target or "the earlier week"
                section = None if not section or _words(section) <= GENERIC_REFERENCE else section
            elif target:
                # "Same as Monday" means this week's Monday, else the nearest earlier
                # week's. Two matches in that week is ambiguous: flagged, not guessed.
                sources, tied = [], False
                for week in range(week_of[position], -1, -1):
                    named = [s for i, s in enumerate(order) if week_of[i] == week and i != position and _names_session(target, s)]
                    if named:
                        sources, tied = named[:1], len(named) > 1
                        break
                if tied:
                    flag(where, f"\u201c{_clean(written, 60)}\u201d matches more than one {target}. Nothing copied.")
                    continue
            else:
                sources = earlier
            if not sources:
                flag(where, f"Couldn't find {target or 'the earlier session'} for \u201c{_clean(written, 60)}\u201d. Nothing copied.")
                continue
            picked = _referenced(sources, section, item.group) if section or (not target) else sources[0].exercises
            if picked is None:
                flag(where, f"\u201c{_clean(written, 60)}\u201d matches more than one earlier part. Nothing copied.")
                continue
            if not picked:
                flag(where, f"Couldn't match \u201c{_clean(written, 60)}\u201d to earlier exercises. Nothing copied.")
                continue
            change = _clean(item.notes)
            progression = read_progression(change)
            applied: list[str] = []
            label = target or "earlier"
            for original in picked:
                copy = original.model_copy(deep=True)
                applied += [f"{copy.name}: {c}" for c in apply_progression(copy, progression)]
                copy.base_notes = original.base_notes if original.copied else _clean(original.notes)
                notes = [copy.base_notes, f"As {label}" + (f" ({section})" if section else ""), f"Progression: {change}" if change else None]
                copy.notes = "\n".join(n for n in notes if n)
                copy.group = _clean(item.group) or copy.group
                copy.refers_to_day = copy.refers_to_section = None
                copy.copied = True
                resolved.append(copy)
            if change and re.search(r"\d", change):
                done = f" Updated {len(applied)} {'dose' if len(applied) == 1 else 'doses'} to match." if applied else ""
                flag(where, f"Copied from {label}: \u201c{_clean(change, 80)}\u201d.{done} Check the rest by hand.")
        session.exercises = resolved
    return read


SAME_SLOT = {"previous week", "last week", "the previous week", "the earlier week", "same session", "earlier week"}
GENERIC_REFERENCE = {"workout", "exercise", "set", "rep", "session", "protocol", "round", "interval", "min", "minute",
                     "3", "and", "day", "week", "block", "plan", "routine", "same", "as", "to", "with", "the"}
REFERENCE_NAME = re.compile(r"^\s*(?:same\b|repeat(?:ed)?\b|as (?:above|before|last|per|week|day)\b)", re.I)
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _implied_reference(item: ReadExercise) -> None:
    """A reference the reader returned as a plain exercise ("Same workout as Nov 20",
    "Same exercises, sets, and reps as Week 1") is still a reference: an exercise
    with nothing written but a name that says "same as" or "repeat"."""
    name = _clean(item.name) or ""
    if item.is_reference or not REFERENCE_NAME.match(name) or _written_dose(item) or _clean(item.prescription):
        return
    week = re.search(r"\bweek\s*\d{1,2}\b", name, re.I)
    day = re.search(r"\bday\s*\d{1,2}\b", name, re.I)
    when = re.search(rf"\b(?:{'|'.join(MONTHS)})[a-z]*\.?\s+\d{{1,2}}\b|\b\d{{1,2}}\s+(?:{'|'.join(MONTHS)})[a-z]*\b|\d{{4}}-\d{{2}}-\d{{2}}", name, re.I)
    named = next((m.group(0) for m in (week, day, when) if m), None)
    # "Same workout, heaviest weights on lifts": what follows the first comma is the change.
    name, change = (re.split(r",|\.\s", name, maxsplit=1) + [""])[:2]
    if _clean(change) and not _clean(item.notes):
        item.notes = _clean(change)
    rest = re.sub(REFERENCE_NAME, "", name)
    rest = re.sub(r"\bas\b.*$", "", rest, flags=re.I) if named else rest
    item.refers_to_day = named or ("above" if re.search(r"\babove\b", name, re.I) else "previous week")
    words = _words(rest)
    if words and not words <= GENERIC_REFERENCE:
        item.refers_to_section = _clean(rest)


def _year(read: ReadPlan, month: int, day: int) -> int:
    """The written year if the programme gives one; otherwise the year that puts
    the date nearest the import (a December plan running into January)."""
    for value in [read.start_date, *[s.date for w in read.weeks for s in w.sessions]]:
        try:
            if value:
                return date.fromisoformat(value).year
        except ValueError:
            continue
    anchor = read.imported_on or date.today()
    return min((anchor.year + d for d in (-1, 0, 1)), key=lambda y: abs((date(y, month, min(day, 28)) - anchor).days))


def _written_date(text: str | None, read: ReadPlan) -> date | None:
    """ "Nov 20", "20 November", "2026-11-20" as a date (year from the programme)."""
    text = (text or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    text = re.sub(r"(?<=\d)-(?=[a-z])|(?<=[a-z])-(?=\d)", " ", text.lower())
    months = "|".join(MONTHS)
    match = re.search(rf"\b({months})[a-z]*\.?\s+(\d{{1,2}})\b|\b(\d{{1,2}})\s+({months})[a-z]*\b", text)
    if not match:
        return None
    month = MONTHS.get(match.group(1) or match.group(4))
    try:
        day = int(match.group(2) or match.group(3))
        explicit_year = re.search(r"\b(\d{4})\b", text)
        year = int(explicit_year[1]) if explicit_year else _year(read, month, day)
        return date(year, month, day) if month else None
    except ValueError:
        return None


def _session_date(session: ReadSession, read: ReadPlan, anchor: date | None = None) -> date | None:
    text = session.date or session.name
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    numeric = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", text)
    if numeric:
        try:
            base = anchor or read.imported_on or date.today()
            years = [int(numeric[3])] if numeric[3] else [base.year - 1, base.year, base.year + 1]
            candidates = [date(y, int(numeric[2]), int(numeric[1])) for y in years]
            return min(candidates, key=lambda d: abs((d - base).days))
        except ValueError:
            return None
    written = _written_date(text, read)
    if written and anchor and not re.search(r"\b\d{4}\b", text):
        candidates = []
        for year in (anchor.year - 1, anchor.year, anchor.year + 1):
            try:
                candidates.append(written.replace(year=year))
            except ValueError:
                pass
        return min(candidates, key=lambda d: abs((d - anchor).days))
    if written:
        return written
    # A day-of-month alone requires a bounded programme-week anchor. Day 1 and
    # Session A are labels, never calendar dates.
    short = re.search(r"\b(?:mon|tues?|wed(?:s|nesday)?|thu(?:rs)?|fri|sat|sun)(?:day)?\.?\s+(\d{1,2})\b", text, re.I)
    if short and anchor:
        dates = [anchor + timedelta(days=n) for n in range(7)]
        return next((d for d in dates if d.day == int(short[1])), None)
    return None


def _slot_name(session: ReadSession) -> str:
    """A session's name without its week, date or round count: "Day 3: Conditioning (5 Rounds)" -> "day 3 conditioning"."""
    name = re.sub(r"\([^)]*\)", " ", (session.name or "").lower())
    name = re.sub(r"\bweek\s*\d+\b|\b\d{1,2}\s*rounds?\b", " ", name)
    return " ".join(re.findall(r"[a-z]+(?:\s*\d+)?", name))


def _same_slot(session: ReadSession, other: ReadSession) -> bool:
    """The same session in another week: the same written day, the same "Day N"
    label, or the same name once week and round counts are set aside."""
    days, theirs = session_days(session), session_days(other)
    if days and theirs:
        return days == theirs
    label = re.search(r"\bday\s*\d+\b", (session.name or "").lower())
    if label and re.search(rf"\b{re.escape(label.group(0))}\b", (other.name or "").lower()):
        return True
    return bool(_slot_name(session)) and _slot_name(session) == _slot_name(other)


def _referenced(sources: list[ReadSession], section: str | None, heading: str | None) -> list[ReadExercise] | None:
    """The exercises a section reference names, in the nearest session that has
    them: a section by its heading ("Warm-up"), else the exercises whose names
    carry its words ("ankle + hip drills"), narrowed by the heading the reference
    is written under ("Mobility Flow"). None when it still means several parts."""
    wanted = _words(section) - {"drill", "routine", "work"} or _words(section)
    for source in sources:
        plain = [e for e in source.exercises if not e.is_reference]
        groups: dict[str, list[ReadExercise]] = {}
        for e in plain:
            groups.setdefault(_clean(e.group) or "", []).append(e)
        by_heading = [g for g in groups if g and wanted and wanted <= _words(g)]
        if len(by_heading) == 1:
            return groups[by_heading[0]]
        if len(by_heading) > 1:
            return None
        hits = [g for g, items in groups.items() if wanted and any(wanted <= _words(e.name) | _words(g) or
                                                                    len(wanted & _words(e.name)) >= 2 for e in items)]
        if len(hits) > 1 and heading:
            narrowed = [g for g in hits if _words(g) & (_words(heading) - {"flow"})]
            hits = narrowed or hits
        if len(hits) == 1:
            return [e for e in groups[hits[0]] if wanted & _words(e.name)] if hits[0] == "" else groups[hits[0]]
        if len(hits) > 1:
            return None
    return []


