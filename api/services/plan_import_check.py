"""Checks a reading against the programme text: each exercise's numbers against
its own written line (fixed from that line when it settles them, else flagged),
and the written week numbers and days against what was placed."""
from __future__ import annotations

import re


from api.contracts.custom_plans import WEEKDAYS
from dataclasses import dataclass
from api.services.plan_import_patch import DOSE_FIELDS
from api.services.plan_import_reading import (
    MAX_WEEKS, ReadCorrection, ReadExercise, _clean, _days, _numbers, _same_word, _tokens, _unit, session_days,
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
        self.lines = []
        aligned = False
        for line in source.replace("\r", "").split("\n"):
            if re.match(r"^\s*(?:code\s{2,})?(?:exercise|movement|drill)\s{2,}", line, re.I):
                aligned = True
            if "\t" in line:
                line = line.replace("\t", " | ")
            elif aligned and re.search(r" {2,}", line):
                line = re.sub(r" {2,}", " | ", line)
            else:
                aligned = False
            line = " ".join(line.split())
            if line:
                self.lines.append(line)
        self.source_lines = self.lines
        self.undo = [(c.corrected.lower(), c.written.lower()) for c in corrections if c.corrected and c.written]
        self.reps_first = False
        # A repeated week, or an every-week session, is the same reading: checked once.
        self.checked: set[tuple[int, int, int]] = set()
        # Exercises whose numbers still don't match their line, for the second look.
        self.session = ""
        self.failed: list[dict] = []
        self.plan = None
        self.bounds = (0, len(self.lines))
        self.current = None
        self.row = None
        self.code = None

    def context(self, session) -> None:
        """Establish week, then session boundaries before matching any exercise.

        Positional context is the source reading's position, before repeats,
        references, weekday expansion or loading-row consolidation.
        """
        self.current = session
        self.bounds = None
        wi, si = session._source_position
        self.lines = self.source_lines
        column_week = False
        # Horizontal week tables carry identity in columns rather than sections.
        # Project the relevant column group before any name or number matching.
        if wi >= 0:
            for line in self.source_lines:
                if "|" not in line:
                    continue
                cells = line.split("|")
                groups = [(n, week_heading(cell)) for n, cell in enumerate(cells) if week_heading(cell)]
                if len(groups) < 2:
                    continue
                number = self.plan.weeks[wi].week_index
                selected = [n for n, (_, values) in enumerate(groups) if number in values] if number else [wi] if len(groups) == len(self.plan.weeks) else []
                if len(selected) != 1 or selected[0] >= len(groups):
                    return
                n = selected[0]
                first, last = groups[n][0], groups[n + 1][0] if n + 1 < len(groups) else len(cells)
                projected = []
                for original in self.source_lines:
                    row = original.split("|")
                    projected.append("|".join(row[:groups[0][0]] + row[first:last]) if len(row) == len(cells) else original)
                self.lines = projected
                column_week = True
                break
        weeks = [(i, week_heading(line)) for i, line in enumerate(self.lines) if week_heading(line)]
        if any(values != {-1} for _, values in weeks):
            weeks = [(i, values) for i, values in weeks if values != {-1}]
        lo, hi = 0, len(self.lines)
        if weeks:
            if wi == -1:
                hi = weeks[0][0]  # recurring overview, never a later week's workout
            else:
                week = self.plan.weeks[wi]
                number = week.week_index
                if number is not None and sum(w.week_index == number for w in self.plan.weeks) != 1:
                    return
                if number is None:
                    # Missing model week labels can use source order only if all sections survived.
                    if len(weeks) != len(self.plan.weeks):
                        return
                    picked = [wi]
                else:
                    picked = [n for n, (_, values) in enumerate(weeks) if number in values]
                    if not picked and all(values == {-1} for _, values in weeks) and len(weeks) == len(self.plan.weeks):
                        picked = [wi]
                if len(picked) != 1:
                    return
                n = picked[0]
                if week.repeat > 1 and any(values & set(range((number or min(weeks[n][1])) + 1,
                                                              (number or min(weeks[n][1])) + week.repeat))
                                           for other, (_, values) in enumerate(weeks) if other != n):
                    # Later explicit sections are not evidence that this template repeats.
                    return
                lo = weeks[n][0] + 1
                hi = weeks[n + 1][0] if n + 1 < len(weeks) else len(self.lines)
        elif wi >= 0 and len(self.plan.weeks) > 1 and not column_week:
            # No identifiable week: only an explicit unique session date can locate it.
            if not session.date or not any(session.date.lower() in line.lower() for line in self.lines):
                return
        # General coaching notes are never exercise evidence.
        hi = next((i for i in range(lo, hi) if re.fullmatch(r"(?:key |general |coaching )?notes:?", self.lines[i], re.I)), hi)
        titles = [(i, line) for i, line in enumerate(self.lines[lo:hi], lo) if session_heading(line)]
        if not titles:
            expected = self.plan.every_week if wi == -1 else self.plan.weeks[wi].sessions
            if len(expected) == 1:
                self.bounds = (lo, hi)
            return
        wanted = _name_words(session.name)
        days = set(session_days(session))
        ranks = []
        for n, (i, line) in enumerate(titles):
            heading_words = _name_words(line)
            named = wanted and wanted <= heading_words
            dated = session.date and session.date.lower() in line.lower()
            day_match = days and days <= set(_heading_days(line))
            label = re.search(r"\b(?:day\s*\d+|session\s*[a-z])\b", session.name, re.I)
            labelled = label and re.search(rf"\b{re.escape(label[0])}\b", line, re.I)
            detailed = labelled and re.match(r"^[# *]*(?:day\s*\d+|session\s*[a-z])\b", line, re.I)
            ranks.append((bool(dated), bool(labelled), bool(detailed), bool(named), bool(day_match)))
        best_rank = max(ranks, default=(False,) * 5)
        candidates = [n for n, rank in enumerate(ranks) if rank == best_rank] if any(best_rank) else []
        if candidates:
            # A detailed Day 1 section beats an overview row labelled Mon (Day 1).
            detailed = [n for n in candidates if not re.search(r"\b(?:day\s*\d+|session\s*[a-z])\b", session.name, re.I)
                        or re.match(r"^[# *]*(?:day\s*\d+|session\s*[a-z])\b", titles[n][1], re.I)]
            candidates = detailed or candidates
        if not candidates and len(titles) == 1:
            expected = self.plan.every_week if wi == -1 else self.plan.weeks[wi].sessions
            if len(expected) == 1:
                candidates = [0]
        if len(candidates) > 1:
            expected = self.plan.every_week if wi == -1 else self.plan.weeks[wi].sessions
            # Identical session titles/dates are distinct source occurrences, in order.
            if len(titles) == len(expected) and si in candidates:
                candidates = [si]
        if len(candidates) != 1:
            return
        n = candidates[0]
        self.bounds = (titles[n][0], titles[n + 1][0] if n + 1 < len(titles) else hi)

    def missing_sessions(self, plan) -> list[tuple[int, str]]:
        """Report source session headings absent from an identifiable programme week."""
        headings = [(i, week_heading(line)) for i, line in enumerate(self.source_lines) if week_heading(line)]
        date_ranges = bool(headings) and all(numbers == {-1} for _, numbers in headings)
        if headings and any(numbers == {-1} for _, numbers in headings) and not date_ranges:
            return []  # Mixed labels have no safe positional correspondence.
        if date_ranges and len(headings) != len(plan.weeks):
            return []
        if not headings and len(plan.weeks) != 1:
            return []
        missing = []
        for wi, week in enumerate(plan.weeks):
            if headings:
                if date_ranges:
                    index = wi
                elif week.week_index is None and len(headings) == len(plan.weeks):
                    index = wi
                else:
                    choices = [n for n, (_, numbers) in enumerate(headings) if week.week_index in numbers]
                    if len(choices) != 1:
                        continue
                    index = choices[0]
                lo = headings[index][0] + 1
                hi = headings[index + 1][0] if index + 1 < len(headings) else len(self.source_lines)
            else:
                lo, hi = 0, len(self.source_lines)
            hi = next((i for i in range(lo, hi) if re.fullmatch(
                r"(?:key |general |coaching )?notes:?", self.source_lines[i], re.I)), hi)
            remaining = list(week.sessions)
            for heading in (line for line in self.source_lines[lo:hi] if session_heading(line)):
                words, days = _name_words(heading), set(_heading_days(heading))
                named_heading = bool(re.search(r"(?:\s*[:|]\s*|\s+[-–—]\s*)\S", heading))
                named = [i for i, session in enumerate(remaining)
                         if _name_words(session.name) and _name_words(session.name) <= words]
                matched = next((i for i in named if not days or days <= set(session_days(remaining[i]))), None)
                if matched is None:
                    matched = next((i for i in named if not session_days(remaining[i])), None)
                if matched is None and days and not named_heading:
                    same_day = [i for i, session in enumerate(remaining) if days <= set(session_days(session))]
                    if len(same_day) == 1:
                        matched = same_day[0]
                if matched is None:
                    # The same session can be written twice (an overview row, then in full).
                    # Identity must agree: a written day must be one of that session's days.
                    named_any = any(_name_words(session.name) and _name_words(session.name) <= words
                                    and (not days or days <= set(session_days(session))) for session in week.sessions)
                    same_day_any = not named_heading and days and len([session for session in week.sessions if days <= set(session_days(session))]) == 1
                    # A rest day written by the coach needs no session. An optional
                    # session is still part of the programme, so its omission is flagged.
                    rest_day = re.search(r"\b(?:rest\s+day|full\s+rest|day\s+off)\b", heading, re.I)
                    if not (named_any or same_day_any or rest_day):
                        missing.append((wi, heading[:100]))
                else:
                    remaining.pop(matched)
        return missing

    def missing_exercises(self, session) -> list[str]:
        """Flag clear source entries absent from the model reading, not prose or loading continuations."""
        if self.bounds is None:
            return []
        lo, hi = self.bounds
        remaining = [_name_words(e.name) for e in session.exercises if e.name.strip() and not e.is_reference]
        missing = []
        name_column = None
        previous_table_name = None
        for line in self.lines[lo:hi]:
            cells = [cell.strip() for cell in line.split("|")]
            headings = [i for i, cell in enumerate(cells) if re.fullmatch(r"exercise|movement|drill", cell, re.I)]
            if len(cells) > 1 and headings:
                name_column = headings[0]
                previous_table_name = None
                continue
            if name_column is not None and "|" in line and len(cells) > name_column:
                candidate = cells[name_column]
                if not candidate or not _has_dose(line) or candidate == previous_table_name:
                    continue
                previous_table_name = candidate
                candidates = [candidate]
            else:
                name_column = None
                # A comma-separated warm-up or exercise line can name several
                # independent movements; rest/cues are not separate exercises.
                plain = re.sub(r"\*\*|__", "", line)
                content = re.sub(r"^\s*(?:warm[ -]?up[^:]*|[^:|]{1,60}\([^)]*\d[^)]*\))\s*:\s*", "", plain, flags=re.I)
                parts = [part.strip() for part in re.split(r"[,;]", content)]
                entries = [part for part in parts if _has_dose(part) and re.match(
                    r"^(?:[☐•*-]\s*)?(?:[A-Za-z][1-9]\d*[.):]\s*)?[A-Za-z]",
                    part) and not re.match(r"^(?:rest|recovery|tempo|sets?|reps?|rounds?|load|rpe|note)\b", part, re.I)]
                if len(entries) >= 2:
                    candidates = entries
                elif content == plain:
                    candidates = [line]
                elif re.match(r"\d", content):
                    # "Hand Gripper (10 mins): 3 sets x 15": the label names the exercise.
                    candidates = [plain[:len(plain) - len(content)]]
                else:
                    # A section label ("Mobility (15 mins): ...") checks only what it introduces.
                    candidates = [content]
            for candidate in candidates:
                match = re.match(r"^\s*(?:☐|[•*]|\d+[.)]|[A-Za-z][1-9]\d*[.):]|-)\s+(.+)$", candidate)
                if match:
                    candidate = re.sub(r"^\d+\s*(?:mins?|seconds?|secs?)\b\s*", "", match[1], flags=re.I)
                elif (not _has_dose(candidate) or session_heading(candidate) or week_heading(candidate)
                      or re.match(r"^(?:warm[ -]?up|cool[ -]?down|rest|repeat|note|focus|cue|rpe|sets?|reps?|rounds?|tempo|load)\b", candidate, re.I)):
                    continue
                candidate = re.split(
                    r"\b\d+\s*[x×*]\s*\d+|\b\d+(?:\s*[-–]\s*\d+)?\s*(?:kgs?|lbs?|mins?|secs?|seconds?|km|metres?)\b|\s+[-–—]\s+",
                    candidate, maxsplit=1, flags=re.I,
                )[0].strip(" :-–—")
                wanted = _name_words(candidate)
                if not wanted:
                    continue
                match = next((i for i, words in enumerate(remaining) if words and
                              sum(any(_same_word(word, other) for other in words) for word in wanted)
                              >= max(1, min(len(wanted), len(words)) * 0.8) and
                              (len(wanted) == len(words) or min(len(wanted), len(words)) > 1)), None)
                if match is None:
                    missing.append(candidate[:100])
                else:
                    remaining.pop(match)
        return missing

    def window(self, name: str) -> str | None:
        """The source line that names this exercise."""
        written = name.lower()
        self.code = None
        for corrected, original in self.undo:
            written = written.replace(corrected, original)
        wanted = _name_words(written) | _name_words(name)
        if not wanted or not self.lines or self.bounds is None:
            return None
        lo, hi = self.bounds
        scores = []
        for line in self.lines[lo:hi]:
            if re.match(r"^[\W]*(?:notes?|cue|focus|coaching)\s*:", line, re.I):
                scores.append((0.0, 0))
                continue
            clause = self._own_line(line, name, wanted)
            have = _name_words(clause)
            hits = sum(1 for word in wanted if any(_same_word(word, other) for other in have))
            prefix = re.split(r"\d|[:|]", clause)[0]
            extra = sum(not any(_same_word(w, other) for other in wanted) for w in _name_words(prefix))
            scores.append((hits / max(1, len(wanted)), -extra))
        top = max(scores, default=(0.0, 0))
        if top[0] < 0.5:
            return None
        # A dose beats a title that merely names the exercise, within this section.
        tied = [lo + index for index, score in enumerate(scores) if score == top]
        dosed = [index for index in tied if _has_dose(self.lines[index])]
        tied = dosed or tied
        # Name collisions are resolved by row occurrence *within this session*,
        # never by a cursor through the document or by prescription similarity.
        if len(tied) > 1:
            if self.current is None or self.row is None:
                return None
            siblings = [e for e in self.current.exercises if _name_words(e.name) == _name_words(name)]
            if len(siblings) != len(tied):
                return None
            ordinal = next((n for n, e in enumerate(siblings) if e._source_index == self.row), None)
            if ordinal is None:
                return None
            best = tied[ordinal]
        else:
            best = tied[0]
        headers = [line for line in self.lines[lo:best + 1] if re.search(r"\b(?:reps\s*[x×]\s*sets|sets\s*[x×]\s*reps)\b", line, re.I)]
        self.reps_first = bool(headers and re.search(r"\breps\s*[x×]\s*sets\b", headers[-1], re.I))
        # The dose is on the exercise's own line. Only a line with no numbers
        # (a heading such as "WEDNESDAY - Pads") borrows the next line or two.
        line = self.lines[best]
        code = re.match(r"^[\W]*([a-z][1-9]\d*)[.):\s|]+", line, re.I)
        self.code = code[1].upper() if code else None
        if "|" in line:
            cells = line.split("|")
            for header in reversed(self.lines[lo:best]):
                if "|" not in header:
                    break
                columns = header.split("|")
                if len(columns) == len(cells):
                    for n, role in enumerate(columns):
                        if re.fullmatch(r"\s*(?:rest|recovery|work|duration|time|wt|weight|load)\s*", role, re.I):
                            cells[n] = f"{role.strip()}: {cells[n].strip()}"
            line = "|".join(cells)
        window = [self._own_line(line, name, set(_tokens(written) or _tokens(name)))]
        for line in self.lines[best + 1:min(best + 3, hi)]:
            if _has_dose(" ".join(window)) and not window[-1].rstrip().endswith(":"):
                break
            if not re.match(r"^(?:\d|rest\b|work\b|sets\b|reps\b|rounds\b)", line.lstrip("•☐- *"), re.I):
                break
            window.append(line)
        # A dose that carries on to the next line ("Rest 1 minute between rounds. Complete 4 rounds.").
        after = best + len(window)
        if after < hi and re.match(r"^\W*(?:rest|repeat|complete|then)\b", self.lines[after], re.I):
            window.append(self.lines[after])
        return " ".join(window)

    def _own_line(self, line: str, name: str, wanted: set[str]) -> str:
        if self.current:
            positions = []
            for exercise in self.current.exercises:
                label = exercise.name.strip()
                if not label:
                    continue
                for match in re.finditer(rf"(?<!\w){re.escape(label)}(?!\w)", line, re.I):
                    positions.append((match.start(), match.end(), label.lower()))
            own = [p for p in positions if p[2] == name.lower()]
            if len(own) == 1:
                first, end, _ = own[0]
                following = [p[0] for p in positions if p[0] >= end]
                # Do not use a substring of a longer exercise name (plank / side plank).
                if not any(p[0] < first and p[1] >= end for p in positions):
                    prefix = line[:first].lstrip("☐•-* ").strip()
                    dose_prefix = re.sub(rf"^{DAY_WORD}\s*[-–—:]\s*", "", prefix, flags=re.I)
                    if TIME.fullmatch(dose_prefix) or DISTANCE.fullmatch(dose_prefix) or AXB.fullmatch(prefix.rsplit(":", 1)[-1].strip()):
                        first = 0
                    elif ":" in prefix and TIME.search(prefix.rsplit(":", 1)[-1]):
                        # "Warm-up: 5 mins jump rope" explicitly assigns that time.
                        first = line.index(":") + 1
                    selected = line[first:min(following) if following else len(line)].rstrip(" ,;:+")
                    # Still exclude unrecognised neighbouring exercises on a shared line.
                    parts = re.split(r"[,;](?![^()]*\))", selected)
                    for n, part in enumerate(parts[1:], 1):
                        if re.match(r"\s*[A-Za-z][^\d]*\d+\s*[x×*]", part):
                            selected = ",".join(parts[:n])
                            break
                    return self._own_clauses(selected, wanted)
        return self._own_clauses(line, wanted)

    @staticmethod
    def _own_clauses(line: str, wanted: set[str]) -> str:
        """One line can hold several exercises ("squats 3x5 @ 100, pull ups 3x8"):
        keep this exercise's clause, and any clauses after it that carry no
        dose of their own ("..., rest 2-3 min")."""
        if len(DOSE_PAIR.findall(line)) < 2:
            return line
        clauses = [c.strip() for c in re.split(r"[,;](?![^()]*\))", line) if c.strip()]
        scores = [sum(1 for word in wanted if any(_same_word(word, other) for other in _tokens(c))) for c in clauses]
        if not scores or max(scores) == 0:
            return line
        start = scores.index(max(scores))
        end = start + 1
        while end < len(clauses) and not DOSE_PAIR.search(clauses[end]):
            end += 1
        own = ", ".join(clauses[start:end])
        # A heading's dose belongs to the heading, not the exercise after its colon.
        if ":" in own and not any(_same_word(w, t) for w in wanted for t in _tokens(own.split(":", 1)[0])):
            own = own.split(":", 1)[1].strip()
        return own

    def check(self, item: ReadExercise, flag) -> None:
        """Every number in the dose, matched to what it is on the source line:
        sets or rounds, reps, work time, rest, distance and its unit, load and
        its unit, RPE. A value written as something else ("3 x 8" read as
        8 sets of 3) or not written at all is flagged. A role the line doesn't
        make clear is only checked for being written at all."""
        identity = (*self.current._source_position, item._source_index) if self.current else None
        if identity is not None and identity in self.checked:
            return
        if identity is not None:
            self.checked.add(identity)
        self.row = item._source_index
        name = _clean(item.name) or ""
        if item.prescribed_sets:
            self._check_stages(item, flag)
            return
        window = self.window(name)
        if window is None:
            flag("Couldn't find its line to check the numbers. Check the dose.")
            return
        source_window = window
        # Typography must not change the meaning of a written range.
        window = _range_dashes(window)
        # Source wording decides ambiguous m, never the model's extracted fields.
        window = _short_minutes(window)
        # A circuit's rounds are written on its heading, not on each exercise's line.
        group = _clean(item.group)
        if group and not any(group.lower() in line.lower() for line in self.lines[slice(*(self.bounds or (0, 0)))]):
            group = None
        read = read_dose_line(window, reps_first=self.reps_first)
        if group and re.search(r"\b(?:circuit|rounds?)\b", group, re.I):
            read.roles["count"].update(read_dose_line(group).roles["count"])
        problems: list[str] = []
        name_code = re.match(r"^\s*([a-z][1-9]\d*)[.):\s]+", item.name, re.I)
        expected_code = item.exercise_code or (name_code[1] if name_code else None)
        if expected_code and expected_code.upper() != self.code:
            problems.append("the exercise code doesn't match its source row")
        # A rep range read without its sets ("8-10" from "Push-ups 4x8-10"): the sets are on the line.
        written_range = re.match(r"^\s*(\d+\s*[-\u2013]\s*\d+)\b", _range_dashes(item.prescription or ""))
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
            if seconds and not expected and read.distances:
                # A distance misread as time must not pass the bare-number fallback.
                problems.append(f"{shown} isn't written as a time")
                return
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
            elif not pairs:
                problems.append(f"distance {item.distance:g} isn't written")
        # Load: weights keep their unit; anything else just has to be written.
        for number, unit in WEIGHT.findall((item.load or "").replace(",", ".")):
            if (float(number), _weight_unit(unit)) not in read.weights:
                problems.append(f"load {number}{unit} isn't written")
        if item.load and _numbers(item.load):
            allowed = {n for n, _ in read.weights}
            if re.search(r"\brpe\b", item.load, re.I):
                allowed |= read.roles["rpe"]
            if re.search(r"\bzone\b", item.load, re.I):
                allowed |= {float(n) for n in re.findall(r"\bzone\s*(\d+)", window, re.I)}
            if re.search(r"\bRIR\b", item.load, re.I):
                for reserve in re.findall(r"\bRIR\s*(\d+(?:-\d+)?)", window, re.I):
                    allowed |= {float(n) for n in _numbers(reserve)}
            for written_load in re.findall(r"(?:@|\badd\b|\bWT\s*[:=]?)\s*(\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?)", window, re.I):
                allowed |= {float(n) for n in _numbers(written_load)}
            load_numbers = _numbers(re.sub(r"\b1\s*RM\b", "", item.load, flags=re.I))
            if not WEIGHT.search(item.load) and any(float(n.replace(",", ".")) not in allowed for n in load_numbers):
                problems.append("the written load or intensity doesn't match")
        elif item.load:
            def intensity_words(value):
                value = re.sub(r"\b(?:body[ -]?weight|bw)\b", "bodyweight", value, flags=re.I)
                value = re.sub(r"\b(?:if able|if possible|optional)\b", "optional", value, flags=re.I)
                return set(re.findall(r"[a-z]+", value.lower()))
            if not intensity_words(item.load) <= intensity_words(window):
                problems.append("the written intensity doesn't match")
        prescription = item.prescription
        if prescription:
            prescription = _short_minutes(prescription, context=name)
            text_dose = read_dose_line(prescription)
            source_dose = read_dose_line(window)
            # Time equivalence is valid only within the same role. Do not turn
            # source minutes into bare numbers that could also validate reps.
            for role in ("work", "rest"):
                for value in sorted(text_dose.roles[role] - source_dose.roles[role]):
                    problems.append(f"{'rest' if role == 'rest' else 'time'} {value:g}s isn't written as {ROLE_WORDS[role]}")
        loose = _dose_numbers(" ".join(filter(None, [re.sub(WEIGHT, "", (item.load or "")), prescription])))
        # Text doses ("4 × 8-10", "30-40 min") only have to be written on the line, in any role.
        on_line = {f"{float(n.replace(',', '.')):g}" for n in _dose_numbers(window)}
        problems += [f"{n} isn't written" for n in loose if f"{float(n.replace(',', '.')):g}" not in on_line]
        # A single reps number read out of a written range collapses what the coach wrote.
        if isinstance(item.reps, int) and read.rep_ranges:
            problems.append(f"reps {item.reps} was written as the range {read.rep_ranges[0]}")
        if item.rpe is not None and re.search(r"\brpe\s*:?\s*\d+(?:\.\d+)?-\d+(?:\.\d+)?", window, re.I):
            problems.append("keep the written RPE range")
        for _, _, role in re.findall(r"(\d+)-(\d+)\s*(sets?|rounds?)\b", window, re.I):
            if (item.sets if role.lower().startswith("set") else item.rounds) is not None:
                problems.append("keep the written set or round range")
        if item.distance is not None and re.search(rf"\d+(?:\.\d+)?-\d+(?:\.\d+)?\s*{DIST_UNITS}\b", window, re.I):
            problems.append("keep the written distance range")
        for match in TIME.finditer(window):
            if not match[2]:
                continue
            after_time = window[match.end():match.end() + 12]
            before_time = window[max(0, match.start() - 10):match.start()]
            rest_range = REST_AFTER.match(after_time) or REST_BEFORE.search(before_time)
            if rest_range and item.rest_seconds is not None:
                problems.append("keep the written rest range")
            elif not rest_range and (item.seconds is not None or item.work_seconds is not None) and not item.prescription:
                problems.append("keep the written time range")
        # Text fields must agree by role and retain ranges, units and conditions.
        text = _range_dashes(" ".join(filter(None, [item.load, prescription])))
        text_dose = read_dose_line(text)
        if any(value not in read.rep_ranges for value in text_dose.rep_ranges) and all(n in on_line for n in _numbers(text)):
            problems.append("the written rep range doesn't match")
        for role in ("count", "reps", "work", "rest", "rpe"):
            unsupported = text_dose.roles[role] - read.roles[role]
            if unsupported:
                problems.append(f"written {ROLE_WORDS[role]} doesn't match")
        for pair in text_dose.weights - read.weights:
            problems.append(f"load {pair[0]:g}{pair[1]} isn't written")
        if text_dose.distances - read.distances:
            problems.append("written distance or unit doesn't match")
        for low, high, unit in re.findall(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\s*(kg|lb|%|min|s)\b", window, re.I):
            if unit.lower() in {"kg", "lb", "%"} and item.load and not re.search(rf"\b{re.escape(low)}\s*-\s*{re.escape(high)}\s*{unit}\b", text, re.I):
                problems.append("keep the written load range")
            if unit.lower() in {"min", "s"} and (item.seconds or item.work_seconds) and not item.prescription:
                problems.append("keep the written time range")
        if re.search(r"\b(?:if able|if possible|optional)\b", window, re.I) and item.load and not re.search(r"\b(?:if able|if possible|optional)\b", item.load, re.I):
            problems.append("the load is optional")
        preserved = " ".join(filter(None, [item.name, prescription, item.notes, item.load, item.tempo, item.equipment, item.group, *item.cues, *item.stages]))
        if re.search(r"\bor\b", window, re.I) and not re.search(r"\bor\b|/", preserved, re.I):
            problems.append("keep the written alternative")
        if item.tempo and re.sub(r"\s+", "", item.tempo.lower()) not in re.sub(r"\s+", "", window.lower()):
            problems.append("tempo isn't written")
        # Missing work is also a discrepancy: deleting an uncertain field must
        # not make a second look appear verified.
        if read.roles["count"] and not (item.sets or item.rounds or text_dose.roles["count"]):
            problems.append("the written sets or rounds are missing")
        range_text = any(value in _range_dashes(item.prescription or "") for value in read.rep_ranges)
        if (read.roles["reps"] or read.rep_ranges) and not (item.reps or text_dose.roles["reps"] or text_dose.rep_ranges or range_text):
            problems.append("the written reps are missing")
        preserved_dose = read_dose_line(preserved)
        if read.roles["work"] and not (item.seconds or item.work_seconds or read.roles["work"] & preserved_dose.roles["work"] or item.rest_seconds in read.roles["work"]):
            problems.append("the written time is missing")
        if read.roles["rest"] and item.rest_seconds is None and not read.roles["rest"] & preserved_dose.roles["rest"]:
            problems.append("the written rest is missing")
        if read.weights and not ((item.load and WEIGHT.search(item.load)) or read.weights & preserved_dose.weights or item.stages):
            problems.append("the written load is missing")
        if read.roles["rpe"] and item.rpe is None and not read.roles["rpe"] & preserved_dose.roles["rpe"]:
            problems.append("the written RPE is missing")
        if read.distances and item.distance is None and not read.distances & preserved_dose.distances:
            problems.append("the written distance is missing")
        for instruction in [item.notes, *item.cues, *item.stop_rules]:
            if instruction:
                instruction_dose = read_dose_line(instruction)
                if instruction_dose.weights - read.weights:
                    problems.append("a load in the notes or cues isn't written for this exercise")
        # "each side" is only true if the line says so (and left out if it does).
        says_per_side = bool(PER_SIDE.search(window))
        # "each way" / "per direction" (neck holds) may be read either way.
        # The line decides: per side is set or cleared to match it.
        if item.per_side and not says_per_side and not EACH_WAY.search(window):
            item.per_side = None
        elif says_per_side and not item.per_side and (item.reps or item.seconds):
            item.per_side = True
        if problems:
            problems = list(dict.fromkeys(problems))
            flag(f"Check the dose: {'; '.join(problems)}.")
            self.failed.append({"session": self.session, "name": item.name or "", "line": source_window, "problems": problems,
                                "read": {f: getattr(item, f) for f in DOSE_FIELDS}})

    def _check_stages(self, item: ReadExercise, flag) -> None:
        """Verify ordered loading rows against their own table cells, never a bag of numbers."""
        if self.bounds is None or self.current is None:
            flag("Couldn't find its loading rows. Compare the stages with the source.")
            return
        lo, hi = self.bounds
        headers = [(n, line.split("|")) for n, line in enumerate(self.lines[lo:hi], lo)
                   if "|" in line and re.search(r"\b(?:exercise|movement|drill)\b", line, re.I)
                   and re.search(r"\b(?:reps|sets|weight|wt)\b", line, re.I)]
        if len(headers) != 1:
            flag("Couldn't find its loading table. Compare the stages with the source.")
            return
        header_index, columns = headers[0]
        name_column = next(i for i, c in enumerate(columns) if re.search(r"\b(?:exercise|movement|drill)\b", c, re.I))
        rows = [line.split("|") for line in self.lines[header_index + 1:hi]
                if "|" in line and not re.fullmatch(r"[| :\-]+", line)]
        rows = [row for row in rows if len(row) == len(columns)]
        if len(rows) == len(self.current.exercises):
            selected = rows[item._source_index:item._source_index + 1]
        else:
            hits = [n for n, row in enumerate(rows) if _name_words(item.name) and
                    _name_words(item.name) <= _name_words(row[name_column])]
            if len(hits) != 1:
                flag("Couldn't identify its loading rows. Compare the stages with the source.")
                return
            start = hits[0]
            end = next((n for n in range(start + 1, len(rows)) if rows[n][name_column].strip()), len(rows))
            selected = rows[start:end]
        if len(selected) != len(item.prescribed_sets):
            flag("Check the dose: the loading stages don't match the source rows.")
            return
        for stage, cells in zip(item.prescribed_sets, selected):
            named = cells[name_column].strip()
            if named and (not item.name.strip() or not _name_words(item.name) <= _name_words(named)):
                flag("Check the dose: the loading row belongs to another exercise.")
                return
            raw = " | ".join(cells)
            stage_text = str(stage.model_dump(exclude_none=True))
            if any(float(n.replace(",", ".")) not in {float(v.replace(",", ".")) for v in _numbers(raw)} for n in _numbers(stage_text)):
                flag("Check the dose: a loading stage contains values absent from its source row.")
                return
            for heading, cell in zip(columns, cells):
                role = heading.strip().lower()
                dose = read_dose_line(cell)
                # A blank code cell continues the row above it.
                if role == "code" and item.exercise_code and cell.strip() and item.exercise_code.casefold() != cell.strip().casefold():
                    flag("Check the dose: the exercise code doesn't match its source row.")
                    return
                if role in {"%", "percent", "percentage"} and stage.percentage is not None:
                    if f"{stage.percentage:g}" not in _numbers(cell):
                        flag("Check the dose: a stage percentage doesn't match its source cell.")
                        return
                if role in {"%", "percent", "percentage"}:
                    if any(float(n) not in {float(v) for v in _numbers(cell)} for n in re.findall(r"(\d+(?:\.\d+)?)\s*%", stage.source_text or "")):
                        flag("Check the dose: a written stage percentage doesn't match its source cell.")
                        return
                if re.search(r"\bsets?\b", role) and re.search(r"\breps?\b", role):
                    if re.search(r"\breps\s*[x×]\s*sets\b", role, re.I):
                        dose = read_dose_line(cell, reps_first=True)
                    if dose.roles["count"] and stage.sets is not None and stage.sets not in dose.roles["count"]:
                        flag("Check the dose: a stage set count doesn't match its source cell.")
                        return
                    if dose.roles["reps"] and isinstance(stage.reps, int) and stage.reps not in dose.roles["reps"]:
                        flag("Check the dose: stage reps don't match their source cell.")
                        return
                    written = read_dose_line(stage.source_text or "")
                    if dose.roles["count"] and stage.sets is None and not written.roles["count"]:
                        flag("Check the dose: a loading stage is missing its written sets.")
                        return
                    if dose.roles["reps"] and stage.reps is None and not written.roles["reps"]:
                        flag("Check the dose: a loading stage is missing its written reps.")
                        return
                    if _numbers(cell) and not dose.roles["count"] and stage.reps is None and not any(n in _numbers(stage.source_text or "") for n in _numbers(cell)):
                        flag("Check the dose: keep the stage's original work notation.")
                        return
                    if any(dose.roles[key] and written.roles[key] - dose.roles[key] for key in ("count", "reps")):
                        flag("Check the dose: written stage sets or reps don't match their source cell.")
                        return
                if role in {"wt", "weight", "load"}:
                    written_weights = re.findall(r"\b(?:WT|weight)\s*[:=]?\s*(\d+(?:\.\d+)?)", stage.source_text or "", re.I)
                    if any(float(n) not in {float(v) for v in _numbers(cell)} for n in written_weights):
                        flag("Check the dose: a written stage weight doesn't match its source cell.")
                        return
                    weight = stage.weight or (stage.load if stage.load and stage.load.method == "absolute" else None)
                    if weight and (weight.value, _weight_unit(weight.unit)) not in dose.weights:
                        flag("Check the dose: a stage load or its unit isn't written in its source cell.")
                        return


ROLE_WORDS = {"count": "the number of sets or rounds", "reps": "reps", "work": "a work time", "rest": "a rest time", "rpe": "an RPE"}
TIME_UNITS = r"(?:hours?|hrs?|h|minutes?|mins?|seconds?|secs?|s)"
DIST_UNITS = r"(?:kms?|k|miles?|mi|metres?|meters?|m|yards?|yds?)"
DOSE_PAIR = re.compile(r"\d\s*(?:x|\u00d7|\*)\s*\d", re.I)
AXB = re.compile(rf"(?<![\d.])(\d+)\s*(?:x|\u00d7|\*)\s*(\d+(?:\.\d+)?)(?:\s*[-\u2013]\s*(\d+(?:\.\d+)?))?\s*(?:({TIME_UNITS})\b|({DIST_UNITS})\b)?", re.I)
TIME = re.compile(rf"(?<![\d.])(\d+(?:\.\d+)?)(?:\s*[-\u2013]\s*(\d+(?:\.\d+)?))?\s*({TIME_UNITS})\b", re.I)


def _range_dashes(text: str) -> str:
    return re.sub(r"(?<=\d)\s*[-\u2010-\u2015\u2212]\s*(?=\d)", "-", text)


def _short_minutes(text: str, *, context: str = "") -> str:
    """Resolve m only from explicit source activity or rest wording.

    Running and other distance activities keep metres. Unknown activities stay
    ambiguous and are reviewed rather than using the model's reading as proof.
    """
    activity = f"{context} {text}"
    timed = re.search(r"\b(?:stretch\w*|mobility|plank|skip\w*|jump[ -]+rope|warm[ -]?up|cool[ -]?down|breath\w*)\b", activity, re.I)
    distance = re.search(r"\b(?:run\w*|roadwork|sprint\w*|walk\w*|swim\w*|row\w*|cycl\w*|bike|distance|metres?|meters?)\b", activity, re.I)

    def replace(match: re.Match) -> str:
        before = text[max(0, match.start() - 12):match.start()]
        after = text[match.end():match.end() + 12]
        if re.search(r"\b(?:rest|recovery|work|duration|time)\s*[:=]?\s*$", before, re.I) or REST_AFTER.match(after) or (timed and not distance):
            return f"{match.group(1)} min"
        return match.group(0)

    return re.sub(r"(?<![\d.])(\d+(?:\.\d+)?(?:\s*[-\u2013]\s*\d+(?:\.\d+)?)?)\s*m\b", replace, text, flags=re.I)


def _dose_numbers(text: str) -> list[str]:
    """Non-time numbers only; explicit time quantities are checked by role."""
    return _numbers(TIME.sub(" ", _range_dashes(text)))


DISTANCE = re.compile(rf"(?<![\d.])(\d+(?:\.\d+)?)\s*({DIST_UNITS})\b", re.I)
WEIGHT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(kgs?|lbs?|pounds?|%)", re.I)
RPE_AT = re.compile(r"\brpe\s*:?\s*(\d+(?:\.\d+)?)(?:\s*[-\u2013]\s*(\d+(?:\.\d+)?))?", re.I)
# "/leg" counts only after a dose ("6 reps/leg", "8 sec max speed/side"), never in a name ("Knee/Leg Raises").
# "each way", "per direction" (neck holds) and a bare "2x30s each" (side plank) may be read either way.
EACH_WAY = re.compile(r"\b(?:each|per|every)\s+(?:way|direction)\b|/\s*direction\b|\d\w*\s+each\b(?!\s+[a-z])", re.I)
PER_SIDE = re.compile(r"\b(?:each|per|every)\s+(?:side|leg|arm|hand|foot)\b|\d\s*(?:(?:reps?|secs?|seconds?|s|m|meters?|metres?|mins?|minutes?|yds?|yards?)\b[\w .]{0,20}?)?/\s*(?:side|leg|arm|hand|foot)\b|\bboth\s+(?:sides|legs|arms)\b|\balternating\b|\bE/S\b|\b\d+\s*(?:e|ea)\b", re.I)
REST_AFTER = re.compile(r"^[ \t()\-\u2013\u2014]{0,3}(?:rest|off|easy|recover\w*|walk\w*|break)\b", re.I)
REST_BEFORE = re.compile(r"\b(?:rest|recovery)\W{0,3}(?:of\W{0,2})?$", re.I)


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


def read_dose_line(text: str, *, reps_first: bool = False) -> DoseLine:
    """What each number on a source line is, by position and unit:
    "3 x 8" is 3 sets of 8 reps; "6 x 3 min" is 6 rounds of 180s; "1 min rest"
    is a rest; "5 km" is a distance; "140kg" is a weight."""
    text = _range_dashes(text)
    text = re.sub(r"\btempo\s*[:=]?\s*(?:[\dXx](?:\s*-\s*[\dXx]){3}|[\dXx]{4})\b", "", text, flags=re.I)
    text = re.sub(r"\b\d\dX\d\b", "", text)
    # m is minutes only with an explicit rest/time label. Otherwise metres
    # remain metres and a proposed duration must be reviewed.
    original = text
    def rest_minutes(match):
        before = original[max(0, match.start() - 15):match.start()]
        after = original[match.end():match.end() + 12]
        timed = re.search(r"\b(?:rest|recovery|duration|time|work)\s*[:=]?\s*$", before, re.I) or re.match(r"\s*(?:rest|off)\b", after, re.I)
        return f"{match[1]} min" if timed else match[0]
    text = re.sub(r"(\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?)\s*m\b", rest_minutes, text, flags=re.I)
    # Reps kept in reserve are an effort cue, not the prescribed rep count.
    text = re.sub(r"\bstop\s+\d+\s+reps?\s+before\b", "stop before", text, flags=re.I)
    text = re.sub(r"\bleave\s+\d+(?:-\d+)?\s+reps?\s+in\s+(?:the\s+)?(?:reserve|tank)\b", "leave reps in reserve", text, flags=re.I)
    text = re.sub(r"(\d+)\s+sets?\s*[×x]\s*(\d+)", r"\1 x \2", text, flags=re.I)
    text = re.sub(r"(\d+)\s+rounds?\s+of\s+(\d+)", r"\1 x \2", text, flags=re.I)
    text = text.replace(",", ".") if re.search(r"\d,\d", text) else text
    roles: dict[str, set[float]] = {"count": set(), "reps": set(), "work": set(), "rest": set(), "rpe": set()}
    distances: set[tuple[float, str]] = set()
    weights: set[tuple[float, str]] = set()
    ranges: list[str] = []
    for first, second, upper, time_unit, dist_unit in AXB.findall(text):
        if reps_first and not time_unit and not dist_unit and not upper:
            first, second = second, first
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
        following_rest = REST_AFTER.match(after)
        if following_rest and re.match(r"\W*walk\b", after, re.I) and not roles["work"]:
            following_rest = None
        role = "rest" if following_rest or REST_BEFORE.search(before) else "work"
        for value in filter(None, (low, high)):
            roles[role].add(float(value) * factor)
    for number, unit in DISTANCE.findall(text):
        distances.add((float(number), _unit(unit) or unit.lower()))
    for number, unit in WEIGHT.findall(text):
        weights.add((float(number), _weight_unit(unit)))
    for low, high, unit in re.findall(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\s*(kgs?|lbs?|pounds?|%)", text, re.I):
        weights.add((float(low), _weight_unit(unit)))
    for low, high in RPE_AT.findall(text):
        for value in filter(None, (low, high)):
            roles["rpe"].add(float(value))
    bare = {f"{float(n):g}" for n in re.findall(r"(?<![\d.])\d+(?:\.\d+)?", text)}
    return DoseLine(roles, distances, weights, bare, ranges)


DAY_WORD = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?s?\.?"


def _name_words(text: str) -> set[str]:
    text = re.sub(r"^\W*[a-z]\d+[.):]?\s*", "", text, flags=re.I)
    text = re.sub(r"\bDB\b", "dumbbell", text, flags=re.I)
    words = set(_tokens(text))
    return words - {"rounds"} if len(words) > 1 else words


def _heading_days(text: str) -> list[str]:
    return [day for match in re.finditer(rf"\b{DAY_WORD}(?![a-z])", text, re.I) for day in _days(match[0])]


def session_heading(text: str) -> bool:
    text = text.lstrip("# *")
    return bool(re.match(rf"^(?:{DAY_WORD}(?![a-z])|day\s*\d+\b|session\s*[a-z]\b|\d{{4}}-\d{{2}}-\d{{2}}\b|\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?\b|\d{{1,2}}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b)", text, re.I))


def week_heading(text: str) -> set[int]:
    """Anchored structural headings, never a digit or W1 embedded in advice."""
    text = text.strip().strip("#*").strip()
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
    ordinal = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
    first = re.match(r"^(first|second|third|fourth|fifth)\s+week\b", text, re.I)
    if first:
        return {ordinal[first[1].lower()]}
    match = re.match(r"^(?:weeks?\s+|wk\s*|w(?=\d)|microcycle\s+|phase\s+)(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b(?:\s*[-–—]\s*(\d{1,2}))?(.*)$", text, re.I)
    if match:
        low = words.get(match[1].lower()) or int(match[1])
        high = int(match[2]) if match[2] else low
        # Prose mentioning a week is not a structural heading.
        tail = match[3].strip()
        if tail and not re.match(r"^[:—–\-(]", tail):
            return set()
        return set(range(low, high + 1)) if low <= high else set()
    # Standalone date ranges represent source sections, not calendar-week numbers.
    if re.fullmatch(r"\d{1,2}(?:/\d{1,2}|\s+[A-Za-z]{3,9})?\s*[-–—]\s*\d{1,2}(?:/\d{1,2}|\s+[A-Za-z]{3,9})?(?:\s+\d{4})?", text) and re.search(r"/\d{1,2}|\b[A-Za-z]{3,9}\b", text):
        return {-1}
    return set()
WRITTEN_DAYS = re.compile(rf"\b({DAY_WORD})(?:\s*(?:-|\u2013|\u2014|\bto\b|\bthrough\b|\bthru\b)\s*({DAY_WORD}))?(?![a-z])", re.I)
WEEK_NUMBERS = re.compile(r"\bweeks?\s+(\d{1,2})(?:\s*(?:-|\u2013|to)\s*(\d{1,2}))?\b", re.I)


def written_layout_flags(source: str, weeks: int, days: set[str], flag, imported_weeks: set[int] | None = None) -> None:
    """The plan's shape checked against the programme text: a week the text numbers
    but the reading doesn't have, and a training day the text never names."""
    numbers = [int(n) for pair in WEEK_NUMBERS.findall(source) for n in pair if n]
    explicit = set().union(*(week_heading(line) for line in source.splitlines())) - {-1}
    if imported_weeks and explicit - imported_weeks:
        missing = ", ".join(str(n) for n in sorted(explicit - imported_weeks))
        flag("Plan", f"Written week numbers {missing} are missing. Check the week assignments.")
    if numbers and weeks < max(numbers) <= MAX_WEEKS and not (imported_weeks and set(numbers) <= imported_weeks):
        flag("Plan", f"The programme mentions Week {max(numbers)}, but only {weeks} {'week was' if weeks == 1 else 'weeks were'} read.")
    written: set[str] = set()
    for first, last in WRITTEN_DAYS.findall(source):
        written.update(_days(f"{first}-{last}" if last else first))
    for day in sorted(days - written, key=WEEKDAYS.index) if written else []:
        flag("Plan", f"A session was placed on {day}, which the programme never names. Check its day.")


