"""Checks a reading against the programme text: each exercise's numbers against
its own written line (fixed from that line when it settles them, else flagged),
and the written week numbers and days against what was placed."""
from __future__ import annotations

import re


from api.contracts.custom_plans import WEEKDAYS
from dataclasses import dataclass
from api.services.plan_import_reading import (
    MAX_WEEKS, ReadCorrection, ReadExercise, _clean, _days, _numbers, _same_word, _tokens, _unit,
)




class SourceCheck:
    """Checks the reader's numbers against the programme text itself.

    The reader's own correction list can't be the only safeguard: a model could
    change "3x8" into "3x10" without saying so. For each exercise, find the line
    of the source it came from (by its name, before any spelling fix) and check
    that every number in its dose is written on that line (or, for a heading
    line with no numbers, on the line or two after).
    A number that isn't, or an exercise whose line can't be found, is flagged."""

    def __init__(self, source: str, corrections: list[ReadCorrection]):
        self.lines = [" ".join(line.split()) for line in source.replace("\r", "").split("\n")]
        self.lines = [line for line in self.lines if line]
        self.undo = [(c.corrected.lower(), c.written.lower()) for c in corrections if c.corrected and c.written]
        # Where the last exercise was found. The same exercise is often written
        # in several weeks ("Trap Bar Deadlift: 4x5", later "4x3"), so the next
        # one is looked for after it first: each week's line, not week 1's.
        self.cursor = 0
        # A repeated week, or an every-week session, is the same reading: checked once.
        self.checked: set[int] = set()

    def window(self, name: str) -> str | None:
        """The source line that names this exercise."""
        written = name.lower()
        for corrected, original in self.undo:
            written = written.replace(corrected, original)
        wanted = set(_tokens(written)) | set(_tokens(name))
        if not wanted or not self.lines:
            return None
        scores = []
        for line in self.lines:
            have = _tokens(line)
            hits = sum(1 for word in set(_tokens(written) or _tokens(name)) if any(_same_word(word, other) for other in have))
            scores.append(hits / max(1, len(set(_tokens(written) or _tokens(name)))))
        top = max(scores, default=0.0)
        if top < 0.5:
            return None
        # Of the best lines, one that carries a dose beats a title or a coaching
        # note that only mentions the exercise. Then the first at or after the
        # last match; else the nearest before it (sessions are not always
        # checked in written order).
        tied = [index for index, score in enumerate(scores) if score == top]
        dosed = [index for index in tied if _has_dose(self.lines[index])]
        tied = dosed or tied
        ahead = [index for index in tied if index >= self.cursor]
        best = ahead[0] if ahead else tied[-1]
        if ahead and dosed:
            self.cursor = best + 1
        # The dose is on the exercise's own line. Only a line with no numbers
        # (a heading such as "WEDNESDAY - Pads") borrows the next line or two.
        window = [self._own_clauses(self.lines[best], set(_tokens(written) or _tokens(name)))]
        for line in self.lines[best + 1:best + 3]:
            if _numbers(" ".join(window)) and not window[-1].rstrip().endswith(":"):
                break
            window.append(line)
        # A dose that carries on to the next line ("Rest 1 minute between rounds. Complete 4 rounds.").
        after = best + len(window)
        if after < len(self.lines) and re.match(r"^\W*(?:rest|repeat|complete|then)\b", self.lines[after], re.I):
            window.append(self.lines[after])
        return " ".join(window)

    @staticmethod
    def _own_clauses(line: str, wanted: set[str]) -> str:
        """One line can hold several exercises ("squats 3x5 @ 100, pull ups 3x8"):
        keep this exercise's clause, and any clauses after it that carry no
        dose of their own ("..., rest 2-3 min")."""
        if len(DOSE_PAIR.findall(line)) < 2:
            return line
        clauses = [c.strip() for c in re.split(r"[,;]", line) if c.strip()]
        scores = [sum(1 for word in wanted if any(_same_word(word, other) for other in _tokens(c))) for c in clauses]
        if not scores or max(scores) == 0:
            return line
        start = scores.index(max(scores))
        end = start + 1
        while end < len(clauses) and not DOSE_PAIR.search(clauses[end]):
            end += 1
        return ", ".join(clauses[start:end])

    def check(self, item: ReadExercise, flag) -> None:
        """Every number in the dose, matched to what it is on the source line:
        sets or rounds, reps, work time, rest, distance and its unit, load and
        its unit, RPE. A value written as something else ("3 x 8" read as
        8 sets of 3) or not written at all is flagged. A role the line doesn't
        make clear is only checked for being written at all."""
        if id(item) in self.checked:
            return
        self.checked.add(id(item))
        name = _clean(item.name) or ""
        numeric = [item.sets, item.reps, item.rounds, item.rpe, item.distance, item.seconds, item.work_seconds, item.rest_seconds]
        if not any(isinstance(v, (int, float)) for v in numeric) and not _numbers(" ".join(filter(None, [item.load, item.prescription]))):
            return
        window = self.window(name)
        if window is None:
            flag("Couldn't find its line to check the numbers. Check the dose.")
            return
        # A circuit's rounds are written on its heading, not on each exercise's line.
        group = _clean(item.group)
        read = read_dose_line(f"{window} ({group})" if group else window)
        problems: list[str] = []
        # A rep range read without its sets ("8-10" from "Push-ups 4x8-10"): the sets are on the line.
        written_range = re.match(r"^\s*(\d+\s*[-\u2013]\s*\d+)\b", item.prescription or "")
        if item.sets is None and written_range:
            pair = re.search(rf"(\d+)\s*[x\u00d7*]\s*{re.escape(written_range.group(1))}", window, re.I)
            if pair:
                item.sets = int(pair.group(1))
        # The line after the exercise's own name: where its own numbers are.
        first = next((t for t in _tokens(name) if t in window.lower()), None)
        after = window[window.lower().index(first):] if first else ""

        def written_as(value: float, *skip: str) -> str | None:
            """What else this number is on the line, if it's anything."""
            for role, numbers in read.roles.items():
                if role not in skip and any(abs(value - n) < 1e-9 for n in numbers):
                    return role
            return None

        def verify(label: str, value, role: str, *, seconds: bool = False, also: tuple[str, ...] = (), field: str | None = None):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return

            def unwritten():
                # A number written nowhere on the line is fixed from the line when
                # the line gives exactly one value for it ("3x8" read as 3x10
                # becomes 3x8). Otherwise it is left as read and flagged.
                value = None
                if field:
                    written = read.roles.get(role) or set()
                    value = next(iter(written)) if len(written) == 1 else None
                    # Only a number written after the exercise's own name: a section
                    # heading's time ("Warm-up (10 min): jump rope, ...") is never given to it.
                    if value is not None and not any(f"{v:g}" in after for v in (value, value / 60)):
                        value = None
                if value is not None:
                    setattr(item, field, int(value) if value == int(value) else value)
                else:
                    problems.append(f"{shown} isn't written")
            expected = set().union(*(read.roles[r] for r in (role, *also)))
            if any(abs(value - n) < 1e-9 for n in expected):
                return
            shown = f"{label} {value:g}{'s' if seconds else ''}"
            # A time written in minutes is also checked as minutes.
            other = written_as(value, role, *also) or (written_as(value / 60, role, *also) if seconds else written_as(value * 60, "count", "reps", "rpe"))
            if expected and other:
                problems.append(f"{shown} is written as {ROLE_WORDS[other]}")
            elif expected:
                unwritten()
            elif not any(f"{value:g}" == n or (seconds and f"{value / 60:g}" == n) for n in read.bare):
                unwritten()
            elif other and not expected:
                # The line doesn't make this role clear, but the number is there as something else.
                problems.append(f"{shown} is written as {ROLE_WORDS[other]}")

        verify("sets", item.sets, "count", field="sets")
        verify("rounds", item.rounds, "count", field="rounds")
        verify("reps", item.reps, "reps", field="reps")
        verify("time", item.seconds, "work", seconds=True, field="seconds")
        verify("work time", item.work_seconds, "work", seconds=True, field="work_seconds")
        # With blocks, the written rest is the rest between blocks; the rounds' own
        # recovery ("15 sec on -> 45 sec recovery") reads as a time.
        verify("rest", item.rest_seconds, "rest", seconds=True, also=("work",) if not read.roles["rest"] or item.blocks else (), field="rest_seconds")
        # Repeated blocks: the repeat is written ("a second block", "x2 blocks"), never folded into rounds.
        repeats = re.search(r"\b(?:repeat\w*|blocks?)\b", re.sub(r"repeat\w*\s*\d+\s*x?\s*=\s*(?:1|one)\s+round", "", window, flags=re.I), re.I)
        if isinstance(item.blocks, int) and item.blocks > 1:
            words = {2: r"second|two|twice|2nd|double", 3: r"third|three|3rd|triple"}.get(item.blocks)
            if f"{item.blocks}" not in read.bare and not (words and re.search(rf"\b(?:{words})\b", window, re.I)):
                problems.append(f"{item.blocks} blocks isn't written")
            verify("rest between blocks", item.block_rest_seconds, "rest", seconds=True, also=("work",))
        elif repeats and (item.rounds or item.work_seconds):
            problems.append("the line repeats a block of rounds, but only one block was read")
        if isinstance(item.rpe, (int, float)) and not any(abs(item.rpe - n) < 1e-9 for n in read.roles["rpe"]):
            problems.append(f"RPE {item.rpe:g} isn't written as an RPE")
        # Distance: the number and its unit together.
        if isinstance(item.distance, (int, float)):
            unit = _unit(item.distance_unit)
            pairs = read.distances
            if pairs and not any(abs(item.distance - n) < 1e-9 and (unit is None or unit == u) for n, u in pairs):
                if any(abs(item.distance - n) < 1e-9 for n, _ in pairs):
                    problems.append(f"distance {item.distance:g} {unit} has a different unit")
                else:
                    problems.append(f"distance {item.distance:g} {unit or ''} isn't written".replace("  ", " "))
            elif not pairs and f"{item.distance:g}" not in read.bare:
                problems.append(f"distance {item.distance:g} isn't written")
        # Load: weights keep their unit; anything else just has to be written.
        for number, unit in WEIGHT.findall((item.load or "").replace(",", ".")):
            if (float(number), _weight_unit(unit)) not in read.weights:
                problems.append(f"load {number}{unit} isn't written")
        loose = [n for n in _numbers(" ".join(filter(None, [re.sub(WEIGHT, "", (item.load or "")), item.prescription])))]
        # Text doses ("4 × 8-10", "30-40 min") only have to be written on the line, in any role.
        on_line = {f"{float(n.replace(',', '.')):g}" for n in _numbers(window)}
        problems += [f"{n} isn't written" for n in loose if f"{float(n.replace(',', '.')):g}" not in on_line]
        # A single reps number read out of a written range collapses what the coach wrote.
        if isinstance(item.reps, int) and read.rep_ranges:
            problems.append(f"reps {item.reps} was written as the range {read.rep_ranges[0]}")
        # "each side" is only true if the line says so (and left out if it does).
        says_per_side = bool(PER_SIDE.search(window))
        # "each way" / "per direction" (neck holds) may be read either way.
        # The line decides: per side is set or cleared to match it.
        if item.per_side and not says_per_side and not EACH_WAY.search(window):
            item.per_side = None
        elif says_per_side and not item.per_side and (item.reps or item.seconds):
            item.per_side = True
        if problems:
            flag(f"Check the dose: {'; '.join(problems)}.")


ROLE_WORDS = {"count": "the number of sets or rounds", "reps": "reps", "work": "a work time", "rest": "a rest time", "rpe": "an RPE"}
TIME_UNITS = r"(?:hours?|hrs?|h|minutes?|mins?|seconds?|secs?|s)"
DIST_UNITS = r"(?:kms?|k|miles?|mi|metres?|meters?|m|yards?|yds?)"
DOSE_PAIR = re.compile(r"\d\s*(?:x|\u00d7|\*)\s*\d", re.I)
AXB = re.compile(rf"(?<![\d.])(\d+)\s*(?:x|\u00d7|\*)\s*(\d+(?:\.\d+)?)(?:\s*[-\u2013]\s*(\d+(?:\.\d+)?))?\s*(?:({TIME_UNITS})\b|({DIST_UNITS})\b)?", re.I)
TIME = re.compile(rf"(?<![\d.])(\d+(?:\.\d+)?)(?:\s*[-\u2013]\s*(\d+(?:\.\d+)?))?\s*({TIME_UNITS})\b", re.I)
DISTANCE = re.compile(rf"(?<![\d.])(\d+(?:\.\d+)?)\s*({DIST_UNITS})\b", re.I)
WEIGHT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(kgs?|lbs?|pounds?|%)", re.I)
RPE_AT = re.compile(r"\brpe\s*:?\s*(\d+(?:\.\d+)?)(?:\s*[-\u2013]\s*(\d+(?:\.\d+)?))?|@\s*(\d+(?:\.\d+)?)\b(?!\s*(?:kgs?|lbs?|%))", re.I)
# "/leg" counts only after a dose ("6 reps/leg", "8 sec max speed/side"), never in a name ("Knee/Leg Raises").
# "each way", "per direction" (neck holds) and a bare "2x30s each" (side plank) may be read either way.
EACH_WAY = re.compile(r"\b(?:each|per|every)\s+(?:way|direction)\b|/\s*direction\b|\d\w*\s+each\b(?!\s+[a-z])", re.I)
PER_SIDE = re.compile(r"\b(?:each|per|every)\s+(?:side|leg|arm|hand|foot)\b|\d\s*(?:(?:reps?|secs?|seconds?|s|m|meters?|metres?|mins?|minutes?|yds?|yards?)\b[\w .]{0,20}?)?/\s*(?:side|leg|arm|hand|foot)\b|\bboth\s+(?:sides|legs|arms)\b|\balternating\b", re.I)
REST_AFTER = re.compile(r"^\W{0,3}(?:rest|off|easy|recover\w*|walk\w*|break)\b", re.I)
REST_BEFORE = re.compile(r"\brest\w*\W{0,3}(?:of\W{0,2})?$", re.I)


COUNTED = re.compile(r"\d+\s*(?:sets?|reps?|rounds?)\b", re.I)


def _has_dose(line: str) -> bool:
    """A real dose ("3x45s", "30 sec", "400m", "60kg"), not just any digit ("Day 1 - Plank")."""
    return any(pattern.search(line) for pattern in (AXB, TIME, DISTANCE, WEIGHT, COUNTED))


def _weight_unit(unit: str) -> str:
    unit = unit.lower()
    return "kg" if unit.startswith("kg") else "%" if unit == "%" else "lb"


@dataclass
class DoseLine:
    roles: dict[str, set[float]]
    distances: set[tuple[float, str]]
    weights: set[tuple[float, str]]
    bare: set[str]
    rep_ranges: list[str]


def read_dose_line(text: str) -> DoseLine:
    """What each number on a source line is, by position and unit:
    "3 x 8" is 3 sets of 8 reps; "6 x 3 min" is 6 rounds of 180s; "1 min rest"
    is a rest; "5 km" is a distance; "140kg" is a weight."""
    text = text.replace(",", ".") if re.search(r"\d,\d", text) else text
    roles: dict[str, set[float]] = {"count": set(), "reps": set(), "work": set(), "rest": set(), "rpe": set()}
    distances: set[tuple[float, str]] = set()
    weights: set[tuple[float, str]] = set()
    ranges: list[str] = []
    for first, second, upper, time_unit, dist_unit in AXB.findall(text):
        roles["count"].add(float(first))
        if dist_unit:
            continue  # the distance pattern below reads it
        if not time_unit:
            if upper:
                ranges.append(f"{second}-{upper}")
            else:
                roles["reps"].add(float(second))
    for number, kind in re.findall(r"(\d+)\s*(sets?|rounds?|reps?)\b", text, re.I):
        roles["reps" if kind.lower().startswith("rep") else "count"].add(float(number))
    for number in re.findall(r"sets?\s+of\s+(\d+)", text, re.I):
        roles["reps"].add(float(number))
    for match in TIME.finditer(text):
        low, high, unit = match.group(1), match.group(2), match.group(3).lower()
        factor = 3600 if unit.startswith(("h", "hr")) else 60 if unit.startswith("m") else 1
        after, before = text[match.end():match.end() + 12], text[max(0, match.start() - 10):match.start()]
        # The words straight after (or "rest" straight before) say whether it is rest.
        role = "rest" if REST_AFTER.match(after) or REST_BEFORE.search(before) else "work"
        for value in filter(None, (low, high)):
            roles[role].add(float(value) * factor)
    for number, unit in DISTANCE.findall(text):
        distances.add((float(number), _unit(unit) or unit.lower()))
    for number, unit in WEIGHT.findall(text):
        weights.add((float(number), _weight_unit(unit)))
    for low, high, at in RPE_AT.findall(text):
        for value in filter(None, (low, high, at)):
            roles["rpe"].add(float(value))
    bare = {f"{float(n):g}" for n in re.findall(r"(?<![\d.])\d+(?:\.\d+)?", text)}
    return DoseLine(roles, distances, weights, bare, ranges)


DAY_WORD = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?s?\.?"
WRITTEN_DAYS = re.compile(rf"\b({DAY_WORD})(?:\s*(?:-|\u2013|\u2014|\bto\b|\bthrough\b|\bthru\b)\s*({DAY_WORD}))?(?![a-z])", re.I)
WEEK_NUMBERS = re.compile(r"\bweeks?\s+(\d{1,2})(?:\s*(?:-|\u2013|to)\s*(\d{1,2}))?\b", re.I)


def written_layout_flags(source: str, weeks: int, days: set[str], flag) -> None:
    """The plan's shape checked against the programme text: a week the text numbers
    but the reading doesn't have, and a training day the text never names."""
    numbers = [int(n) for pair in WEEK_NUMBERS.findall(source) for n in pair if n]
    if numbers and weeks < max(numbers) <= MAX_WEEKS:
        flag("Plan", f"The programme mentions Week {max(numbers)}, but only {weeks} {'week was' if weeks == 1 else 'weeks were'} read.")
    written: set[str] = set()
    for first, last in WRITTEN_DAYS.findall(source):
        written.update(_days(f"{first}-{last}" if last else first))
    for day in sorted(days - written, key=WEEKDAYS.index) if written else []:
        flag("Plan", f"A session was placed on {day}, which the programme never names. Check its day.")


