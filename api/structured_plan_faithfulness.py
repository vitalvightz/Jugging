"""Faithfulness gate: prove a structured_plan is a projection of the Stage 2 text.

The structured card the athlete actually sees is a *second* LLM conversion of the
validated Stage 2 ``final_plan_text``. Schema validity alone does not prove the
card reflects that text — the model can introduce exercises that were never
prescribed, invent countdown (D-day) markers, or move work into the wrong day.
This module compares the generated structured plan against the source markdown
and reports any content the card *introduced* or *misplaced*, so a card that can
not be proven faithful is rejected in favour of the validated text / raw-markdown
fallback. ``final_plan_text`` stays the single source of truth; the structured
plan is allowed through only as a verified projection of it.

Design rules (mirrors ``structured_plan_safety`` conventions):

* Fail-closed but low false-positive. Only CLEAR drift is flagged:
  - INTRODUCED: a training-block exercise whose name shares *no* meaningful token
    with the source text (a fabricated exercise), or a countdown marker absent
    from the source.
  - MISPLACED: an exercise the source assigns to exactly one D-day, placed by the
    card under a *different* D-day (e.g. Pallof moved out of its session).
  - LOCKED_CONTENT: wording from a governed ``selected_drill_locked`` role that
    appears in the approved source has been removed or rewritten in the card.
  - PRESCRIPTION: explicit rest, effort, or execution-cue data present on an
    exact source exercise was omitted by the structured conversion. This is an
    advisory consumed by the source-backed renderer, not grounds to discard an
    otherwise valid card; the other finding types remain blocking.
  - COACHING_DROPPED: an exact exercise on its source D-day lost an explicit
    coaching, progression, regression or stop field. This is blocking; the
    deterministic card retains the source-backed guidance without another call.
  A reworded exercise that keeps any meaningful token (``Back Squat`` ->
  ``Barbell Back Squat``) passes. Generic/contextual blocks (mindset, nutrition,
  recovery, mobility, preparation, cooldown) are wording the conversion owns, not
  exercise selection, so they are never exercise-name checked.
* The whole check only runs when the source looks like a real countdown plan
  (it carries at least one ``D-N`` day marker). Stubs / degenerate text are not
  evaluated, so the gate can never reject a card it has no basis to judge.
* Read-only: never mutates the plan. Returns prefixed violation strings.
* Fail-closed: the public entry point never propagates an exception, but an
  internal crash is reported as an INTERNAL violation so an unverifiable card is
  rejected (the raw-markdown fallback ships) rather than passed through.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import re
from typing import Any

# Violation prefixes so findings are greppable in debug metadata / logs.
INTRODUCED = "INTRODUCED"
MISPLACED = "MISPLACED"
COUNTDOWN = "COUNTDOWN"
# Raised internally by the checker -> treated as a violation so a crash rejects
# the card (fail-closed) rather than letting an unverified card through.
INTERNAL = "INTERNAL"
LOCKED_CONTENT = "LOCKED_CONTENT"
PRESCRIPTION = "PRESCRIPTION"
PRESCRIPTION_CHANGED = "PRESCRIPTION_CHANGED"
COACHING_DROPPED = "COACHING_DROPPED"
LOCKED_TACTICAL_WATCH_MISSING = "locked_tactical_watch_missing_from_stage2"

# ``block_type`` values that name a specific, app-owned exercise we can hold to
# the source text. Generic/contextual block types are deliberately excluded.
_EXERCISE_BLOCK_TYPES = {
    "strength",
    "strength_speed",
    "plyometric_power",
    "speed",
    "accessory",
    "conditioning",
    "rehab",
}

# Tokens too generic to identify a specific exercise or prove day placement.
_GENERIC_TOKENS = {
    "strength", "power", "speed", "session", "sessions", "training", "block",
    "blocks", "work", "warm", "cool", "down", "mobility", "recovery", "rest",
    "conditioning", "circuit", "drill", "drills", "primer", "accessory",
    "exercise", "exercises", "heavy", "light", "tempo", "effort", "rounds",
    "round", "reps", "sets", "left", "right", "front", "back", "upper", "lower",
    "main", "optional", "superset", "isometric", "dynamic", "static", "hold",
    "holds", "with", "from", "this", "that", "each", "side", "both", "core",
    "tactical", "watch", "note", "notes", "cue", "cues", "card", "film",
    "review", "visualization", "visualisation",
}

_TOKEN_RE = re.compile(r"[a-z]+")
_DDAY_RE = re.compile(r"D-(\d+)", re.I)
_ANY_DDAY_RE = re.compile(r"D-\s*\d+", re.I)
# A day header that leads with the countdown, optionally behind markdown hashes
# and/or a weekday, e.g. "D-32 (Wednesday) — Aerobic support" or "### D-30 ...".
_LEADING_DDAY_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(?:[A-Za-z]{3,9}\s+)?D-\d+\b", re.I)
# A "(D-N)" marker anywhere, e.g. "### Mon (D-30) — Strength".
_PAREN_DDAY_RE = re.compile(r"\(D-\d+\)", re.I)

_SOURCE_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")
_SOURCE_EFFORT_RE = re.compile(r"\b(?:RPE|RIR)\s*\d", re.I)
_SOURCE_REST_RE = re.compile(
    r"(?:\b(?:rest|recovery|reset)\b[^\n]{0,24}\d|"
    r"\d[^\n]{0,24}\b(?:rest|recovery|reset)\b)",
    re.I,
)
_SOURCE_DETAIL_LABEL_RE = re.compile(
    r"^\s*(?:\*\*)?(?:How|Cue|Coaching cues|Easier|Regression|Regress|Progress|Progression|"
    r"Stop|Stop rule|Quality stop|Swap|Swaps|Substitution|Purpose|Why today)(?:\*\*)?\s*:",
    re.I,
)
_SOURCE_CUE_RE = re.compile(r"^\s*Cue\s*:\s*\S", re.I | re.M)


def _source_block_segment(source: str, display_name: str) -> str:
    """Return one exact titled source block and its indented detail lines.

    This deliberately fails closed on renamed or ambiguous titles. The general
    faithfulness gate remains tolerant of rewording; prescription fidelity is
    enforced only when the source/block identity is exact enough to prove.
    """
    name_tokens = re.findall(r"[a-z0-9]+", str(display_name or "").casefold())
    if not name_tokens:
        return ""
    # A name's own closing bracket ("Jump Lunge (Alternating)") sits between its
    # last word and the delimiter; without it every parenthetical name misses.
    title = (
        r"^\s*(?:[-*•]\s*)?"
        + r"[\s\W]+".join(re.escape(token) for token in name_tokens)
        + r"[)\]]*"
    )
    delimiter = r"\s*(?:[.:,—–]|\s-\s)"
    exact_pattern = re.compile(title + delimiter, re.I)
    # The block's title may be one option of a source choice ("Short sprint
    # bounds or low box jumps"). That line is its source only when the plan
    # carries no exact title of its own: a plan holding BOTH lines prescribes
    # two different exercises, and the exact one owns this block's dose. So the
    # exact pattern sweeps every line before the choice pattern is consulted.
    choice_pattern = re.compile(title + r"\s+or\s+[^.:,—–]+" + delimiter, re.I)

    lines = str(source or "").splitlines()

    def _segment_at(index: int) -> str:
        segment = [lines[index]]
        for following in lines[index + 1 :]:
            if not following.strip():
                break
            if _SOURCE_HEADING_RE.match(following) or re.match(r"^\s*[-*•]\s+", following):
                break
            if following[:1].isspace() or _SOURCE_DETAIL_LABEL_RE.match(following):
                # Final-session text may write its labelled Cue/Progress/
                # Easier/Stop/How lines flush-left under the exercise.
                segment.append(following)
                continue
            break
        return "\n".join(segment)

    for pattern in (exact_pattern, choice_pattern):
        matches = [index for index, line in enumerate(lines) if pattern.match(line)]
        if matches:
            return _segment_at(matches[0]) if len(matches) == 1 else ""
    return ""


def _open_source_sessions(source: str) -> dict[tuple, list[list[str]]]:
    """Index weekday session cards without borrowing from repeated weeks."""
    from fightcamp.structured_session_identity import identity_tokens
    from fightcamp.weekly_schedule_view import normalize_weekday

    sections: dict[tuple, list[list[str]]] = {}
    current = None
    week = None
    in_cards = False
    for line in source.splitlines():
        heading = re.sub(r"^\s*#{1,6}\s*", "", line).strip()
        if re.match(r"Session Cards\b", heading, re.I):
            in_cards, current = True, None
            continue
        if re.match(r"(?:4[- ]Week Development Block|Progression Rules|Priority Hierarchy)\b", heading, re.I):
            in_cards, current = False, None
        if match := re.match(r"Week\s+(\d+)\b", heading, re.I):
            week, current = int(match[1]), None
        else:
            # Older Open Plans used "Strength. Monday" or "Monday. Strength"
            # instead of "Monday — Strength". Read those only inside cards;
            # weekly-rhythm summaries must never become exercise authority.
            match = re.match(r"([A-Za-z]+)\s*(?:[—–]|\s-\s)\s*(.+)$", heading)
            weekday, title = (normalize_weekday(match[1]), match[2]) if match else (None, None)
            if not weekday and in_cards:
                if match := re.match(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\.\s+(.+)$", heading, re.I):
                    weekday, title = normalize_weekday(match[1]), match[2]
                elif match := re.match(r"(.+?)\.\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)$", heading, re.I):
                    weekday, title = normalize_weekday(match[2]), match[1]
            if weekday:
                current = [line]
                key = (week, weekday, tuple(sorted(identity_tokens(title))))
                sections.setdefault(key, []).append(current)
            elif _SOURCE_HEADING_RE.match(line):
                current = None
            elif current is not None:
                current.append(line)
    return sections


def project_source_adjustments(plan: dict[str, Any], source: str) -> dict[str, Any]:
    """Project source coaching and recover missing prescription fields.

    Day, session and exact exercise identity must all match. Existing measured
    values remain subject to faithfulness validation; missing values come only
    from the approved source, never from another occurrence or a guessed dose.
    The same projection runs on generation and reads of older stored cards.
    """
    from .structured_plan_deterministic_fallback import _source_block, _source_lines_for_session
    from fightcamp.structured_session_identity import identity_tokens
    from fightcamp.weekly_schedule_view import normalize_weekday

    source_days = _source_day_section_lines(str(source or ""))
    open_sessions = _open_source_sessions(str(source or "")) if not source_days else {}
    for week_position, week in enumerate(plan.get("weeks") or [], 1):
        for day in (week.get("days") or []) if isinstance(week, dict) else []:
            if not isinstance(day, dict):
                continue
            day_lines = source_days.get(_dday_num(day.get("countdown_label")), [])
            for session in day.get("sessions") or []:
                if not isinstance(session, dict):
                    continue
                scoped = _source_lines_for_session(day_lines, str(session.get("title") or ""))
                candidates = []
                if not source_days:
                    identity = (normalize_weekday(day.get("weekday")), tuple(sorted(identity_tokens(session.get("title")))))
                    matches = open_sessions.get((week.get("week_index") or week_position, *identity))
                    if matches is None:
                        matches = open_sessions.get((None, *identity), [])
                    scoped = matches[0] if len(matches) == 1 else None
                    if not matches:
                        weekday = identity[0]
                        week_number = week.get("week_index") or week_position
                        candidates = [section for (source_week, source_weekday, _), sections in open_sessions.items()
                                      if source_week in (None, week_number) and source_weekday == weekday
                                      for section in sections]
                for block in session.get("blocks") or []:
                    if not isinstance(block, dict) or block.get("athlete_change") or block.get("prescription_source") == "athlete":
                        continue
                    if str(block.get("block_type")) not in _EXERCISE_BLOCK_TYPES | {"skill", "preparation", "mobility_activation", "cooldown_recovery"}:
                        continue
                    name = str(block.get("display_name") or "").strip()
                    block_lines = scoped
                    if not block_lines and source_days:
                        block_lines = _source_lines_for_session(day_lines, str(session.get("title") or ""), block_name=name)
                    elif not block_lines and candidates:
                        exact = [section for section in candidates if _source_block_segment("\n".join(section), name)]
                        block_lines = exact[0] if len(exact) == 1 else None
                    if not block_lines or not _source_block_segment("\n".join(block_lines), name):
                        continue
                    expected = _source_block(block_lines, name)
                    if not expected:
                        continue
                    block["regression_options"] = list(expected.get("regression_options") or [])
                    if expected.get("progression_rule"):
                        block["progression_rule"] = expected["progression_rule"]
                    else:
                        block.pop("progression_rule", None)
                    for key in ("sets", "reps", "rounds", "duration", "work", "rest", "distance", "effort", "load", "execution_instructions", "coaching_cues", "stop_rules"):
                        if expected.get(key) and not block.get(key):
                            block[key] = copy.deepcopy(expected[key])
                    # Cluster rows and qualitative rest are structure the
                    # conversion has no field for; the exact source line owns
                    # them, so they are projected here rather than authored.
                    if expected.get("components"):
                        block["components"] = copy.deepcopy(expected["components"])
                        block["rounds"] = expected["rounds"]
                    if expected.get("rest_note") and not block.get("rest"):
                        block["rest_note"] = expected["rest_note"]
    return plan


def _explicit_prescription_violations(plan: dict[str, Any], source: str) -> list[str]:
    """Reject only structured fields that demonstrably disappeared from raw.

    The raw plan remains authoritative. This never fills or invents a value; it
    reports a lossy second conversion so the caller can retain the source-backed
    rendering recovery while exposing the omission in diagnostics.
    """
    violations: list[str] = []
    from .structured_plan_deterministic_fallback import _source_block, _source_lines_for_session
    source_days = _source_day_section_lines(source)
    open_sessions = _open_source_sessions(source) if not source_days else {}
    from fightcamp.structured_session_identity import identity_tokens
    from fightcamp.weekly_schedule_view import normalize_weekday

    for position, week in enumerate(plan.get("weeks") or [], 1):
        if not isinstance(week, dict):
            continue
        for day in week.get("days") or []:
            if not isinstance(day, dict):
                continue
            for session in day.get("sessions") or []:
                if not isinstance(session, dict):
                    continue
                for block in session.get("blocks") or []:
                    if not isinstance(block, dict):
                        continue
                    name = str(block.get("display_name") or "").strip()
                    day_lines = source_days.get(_dday_num(day.get("countdown_label")), [])
                    scoped = _source_lines_for_session(day_lines, str(session.get("title") or ""))
                    if open_sessions:
                        weekday = normalize_weekday(day.get("weekday"))
                        title = set(identity_tokens(session.get("title")))
                        candidates = [
                            lines for (source_week, source_day, source_title), sections in open_sessions.items()
                            if source_week in (None, week.get("week_index") or position)
                            and source_day == weekday
                            and (not title or set(source_title) <= title or title <= set(source_title))
                            for lines in sections
                        ]
                        scoped = candidates[0] if len(candidates) == 1 else None
                    if day_lines and scoped is None:
                        continue  # A same-day source session cannot be identified.
                    segment = _source_block_segment("\n".join(scoped) if scoped else source, name)
                    if not segment:
                        continue
                    expected = _source_block(scoped, name) if scoped else None
                    if expected:
                        for key in ("coaching_cues", "regression_options", "progression_rule", "stop_rules"):
                            if key == "coaching_cues" and not _SOURCE_CUE_RE.search(
                                segment
                            ):
                                continue  # Dose remainder is not a labelled execution cue.
                            if expected.get(key) and not block.get(key):
                                violations.append(f"{COACHING_DROPPED}: {name!r} dropped source {key}")
                        if str(block.get("block_type")) not in _EXERCISE_BLOCK_TYPES:
                            continue
                        for key in ("sets", "reps", "rounds", "duration", "work", "rest", "distance", "effort", "load"):
                            if key in expected and key in block and block[key] is not None:
                                if _prescription_value(expected[key]) != _prescription_value(block[key]):
                                    violations.append(f"{PRESCRIPTION_CHANGED}: {name!r} changed source {key}")
                        tempo = block.get("tempo")
                        if isinstance(tempo, dict):
                            if "max speed" in segment.lower() and str(tempo.get("concentric") or "").lower() not in {"x", "0", "fast", "explosive"}:
                                violations.append(f"{PRESCRIPTION_CHANGED}: {name!r} replaced explosive execution with slow tempo")
                            if expected.get("duration") and re.search(r"\bholds?\b", segment, re.I):
                                violations.append(f"{PRESCRIPTION_CHANGED}: {name!r} added movement tempo to a hold")
                    if str(block.get("block_type")) not in _EXERCISE_BLOCK_TYPES:
                        continue
                    checks = (
                        (_SOURCE_REST_RE.search(segment), bool(block.get("rest") or block.get("rest_note")), "rest"),
                        (_SOURCE_EFFORT_RE.search(segment), bool(block.get("effort")), "effort"),
                        (_SOURCE_CUE_RE.search(segment), bool(block.get("coaching_cues")), "cue"),
                    )
                    for source_match, structured_present, field_name in checks:
                        if source_match and not structured_present:
                            violations.append(
                                f"{PRESCRIPTION}: {name!r} dropped explicit source {field_name}"
                            )
    return violations


def _prescription_value(value: Any) -> Any:
    if isinstance(value, dict):
        if "method" in value:
            quantity = (str(value.get("method") or "").upper(), _prescription_value(value.get("value")))
            if value.get("unit"):
                unit = str(value["unit"]).lower()
                unit = {"lbs": "lb", "%": "percent"}.get(unit, unit)
                return (*quantity, unit, str(value.get("ref") or "").upper())
            return quantity
        number = value.get("value")
        unit = str(value.get("unit") or "").lower()
        if isinstance(number, (int, float)):
            if unit in {"s", "sec", "seconds"}:
                return ("time", round(float(number), 6))
            if unit in {"min", "minutes"}:
                return ("time", round(float(number) * 60, 6))
            if unit in {"m", "meters", "metres"}:
                return ("distance", float(number))
        return value
    if isinstance(value, (int, float)) or re.fullmatch(r"\d+(?:\.\d+)?", str(value)):
        return float(value)
    return str(value)


def _day_header_dday(line: str) -> int | None:
    """Return the single D-day a line declares as a training-day header, else None.

    Supports both generated formats — countdown-leading
    (``D-32 (Wednesday) — Aerobic support``) and markdown headings carrying a
    parenthetical marker (``### Mon (D-30) — Strength``). A line with two or more
    D-day numbers is a week/range header (``GPP — Week 1 (D-33 to D-27)``) and is
    never a day section. Lines naming "week" are excluded unless their countdown
    marker leads the line: athlete-facing day titles such as ``D-9 — Fight-week
    freshness`` are valid day sections, not week/range headers.
    """
    nums = _DDAY_RE.findall(line)
    if len(nums) != 1:
        return None
    leading_countdown = bool(_LEADING_DDAY_RE.match(line))
    if "week" in line.lower() and not leading_countdown:
        return None
    is_header = (
        line.lstrip().startswith("#")
        or "—" in line
        or "–" in line
        or bool(_PAREN_DDAY_RE.search(line))
        or leading_countdown
    )
    return int(nums[0]) if is_header else None


def _tokens(text: str) -> set[str]:
    return {tok for tok in _TOKEN_RE.findall(text.lower()) if len(tok) >= 4}


def _meaningful(tokens: set[str]) -> set[str]:
    return {tok for tok in tokens if tok not in _GENERIC_TOKENS}


def _present_in_source(tok: str, source_tokens: set[str]) -> bool:
    """True when a card token matches a source token.

    Exact match, or a shared 5-char prefix so simple inflections (``squat`` vs
    ``squats``, ``lunge`` vs ``lunges``) are not mistaken for fabrication. Short
    tokens (< 5 chars) require an exact match to stay specific.
    """
    if tok in source_tokens:
        return True
    if len(tok) < 5:
        return False
    head = tok[:5]
    return any(
        len(src) >= 5 and (src.startswith(head) or tok.startswith(src[:5]))
        for src in source_tokens
    )


def _dday_num(label: Any) -> int | None:
    """Parse the countdown distance from a label like ``D-15`` (-> ``15``).

    Returns the magnitude after ``D-`` so it lines up with
    :func:`_day_header_dday` (which keys day sections by the same positive value);
    both must agree for the MISPLACED/COUNTDOWN comparisons to hold.
    """
    match = _DDAY_RE.search(str(label or ""))
    if not match:
        return None
    return int(match.group(1))


def _card_claims_countdown(plan: dict[str, Any]) -> bool:
    """True when the card asserts a countdown structure we would need to verify.

    A real athlete-facing card carries week countdown bounds and/or per-day
    countdown labels. When it does, the source text must carry at least one D-day
    marker for the card to be provable; otherwise the card's countdown claims are
    unverifiable. An empty/degenerate card makes no countdown claim, so there is
    nothing to project and the schema gate is the only authority.
    """
    weeks = plan.get("weeks") if isinstance(plan.get("weeks"), list) else []
    for week in weeks:
        if not isinstance(week, dict):
            continue
        if _dday_num(week.get("countdown_start")) is not None:
            return True
        if _dday_num(week.get("countdown_end")) is not None:
            return True
        days = week.get("days") if isinstance(week.get("days"), list) else []
        for day in days:
            if isinstance(day, dict) and _dday_num(day.get("countdown_label")) is not None:
                return True
    return False


def _source_day_sections(markdown: str) -> dict[int, str]:
    """Map each D-day number to the lowercased source text under its day header.

    Uses :func:`_day_header_dday` (which understands both the countdown-leading and
    parenthetical heading formats and excludes week/range headers) to find where
    each day section starts; lines accumulate under the most recent day.
    """
    sections: dict[int, list[str]] = {}
    current: int | None = None
    for line in markdown.splitlines():
        day = _day_header_dday(line)
        if day is not None:
            current = day
            sections.setdefault(current, [])
        elif _SOURCE_HEADING_RE.match(line):
            # A week/phase heading ("## Week 4 — TAPER") closes the previous
            # day; its lines and Focus copy never belong to that day's session.
            current = None
        if current is not None:
            sections[current].append(line)
    return {day: "\n".join(lines).lower() for day, lines in sections.items()}


def _source_day_section_lines(markdown: str) -> dict[int, list[str]]:
    """Map each D-day number to its raw source lines."""
    sections: dict[int, list[str]] = {}
    current: int | None = None
    for line in markdown.splitlines():
        day = _day_header_dday(line)
        if day is not None:
            current = day
            sections.setdefault(current, [])
        elif _SOURCE_HEADING_RE.match(line):
            # A week/phase heading ("## Week 4 — TAPER") closes the previous
            # day; its lines and Focus copy never belong to that day's session.
            current = None
        if current is not None:
            sections[current].append(line)
    return sections


def _source_day_header_indices(lines: list[str], day: int) -> list[int]:
    return [
        index
        for index, line in enumerate(lines)
        if _day_header_dday(line) == day
    ]


def _source_day_insert_index(lines: list[str], day: int) -> tuple[int | None, str | None]:
    """Resolve where a same-day support session can be appended.

    Multiple headers for the same D-day are valid when they represent separate
    sessions on one contiguous calendar day. A repeated D-day split by another
    D-day is treated as ambiguous because inserting into one island would guess
    at calendar ownership.
    """
    indices = _source_day_header_indices(lines, day)
    if not indices:
        headers = [
            (index, found)
            for index, line in enumerate(lines)
            if (found := _day_header_dday(line)) is not None
        ]
        if any(later > earlier for (_, earlier), (_, later) in zip(headers, headers[1:])):
            return None, "countdown days are not in descending order"
        return next((index for index, found in headers if found < day), len(lines)), None

    start = indices[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        next_day = _day_header_dday(lines[index])
        if next_day is not None and next_day != day:
            end = index
            break

    if any(index >= end for index in indices):
        return None, "authoritative day split across multiple calendar groups"
    return end, None


def _source_token_days(sections: dict[int, str]) -> dict[str, set[int]]:
    """Index: meaningful token -> the set of D-day sections it appears in."""
    index: dict[str, set[int]] = {}
    for day, text in sections.items():
        for tok in _meaningful(_tokens(text)):
            index.setdefault(tok, set()).add(day)
    return index


def check_structured_faithfulness(
    structured_plan: Any,
    source_markdown: str,
    planning_brief: Any = None,
) -> list[str]:
    """Return violation strings proving the card drifted from the source text.

    Empty list means the card is a faithful projection (or there is no basis to
    judge — no countdown markers in the source — in which case the schema gate is
    the only authority). Fail-closed: if the check itself crashes it returns a
    violation so the card is rejected rather than shipped unverified.
    """
    try:
        return _check(structured_plan, source_markdown, planning_brief)
    except Exception:  # fail-closed: an unverifiable card must not ship
        return [f"{INTERNAL}: faithfulness check raised; rejecting card"]


def _normalise_locked_text(value: Any) -> str:
    """Normalise presentation-only differences without permitting a rewrite."""
    return " ".join(
        str(value or "")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .lower()
        .split()
    )


def _locked_roles(value: Any) -> list[dict[str, Any]]:
    """Find governed roles without depending on a particular brief nesting."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        governance = value.get("governance")
        if (
            isinstance(governance, dict)
            and governance.get("selected_drill_locked") is True
            and isinstance(value.get("display_text"), str)
        ):
            found.append(value)
        for child in value.values():
            found.extend(_locked_roles(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_locked_roles(child))
    return found


def _card_strings(value: Any) -> list[str]:
    """Collect strings from a deliberately scoped piece of the card."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [
            text
            for child in value.values()
            for text in _card_strings(child)
        ]
    if isinstance(value, list):
        return [text for child in value for text in _card_strings(child)]
    return []


def _locked_source_day(source: str, role: dict[str, Any], drill_name: str) -> int | None:
    """Resolve the authoritative source day for one locked drill role."""
    sections = _source_day_sections(source)
    role_day = next(
        (
            _dday_num(role.get(key))
            for key in (
                "scheduled_countdown_label",
                "countdown_label",
                "countdown_display_label",
            )
            if _dday_num(role.get(key)) is not None
        ),
        None,
    )
    normalised_name = _normalise_locked_text(drill_name)
    if role_day is not None and normalised_name in _normalise_locked_text(
        sections.get(role_day, "")
    ):
        return role_day
    matching_days = [
        day
        for day, section in sections.items()
        if normalised_name in _normalise_locked_text(section)
    ]
    return matching_days[0] if len(matching_days) == 1 else None


def _authoritative_locked_day(role: dict[str, Any]) -> int | None:
    return next(
        (
            _dday_num(role.get(key))
            for key in (
                "scheduled_countdown_label",
                "countdown_label",
                "countdown_display_label",
            )
            if _dday_num(role.get(key)) is not None
        ),
        None,
    )


def _locked_source_drill_text(
    source: str, role: dict[str, Any], drill_name: str
) -> tuple[int | None, str, str | None]:
    """Return the authoritative source text for one locked drill.

    The check is intentionally scoped to the role's D-day and then to the named
    drill block under that day. Repeated overlay wording elsewhere in the plan is
    not evidence for this drill.
    """
    role_day = _authoritative_locked_day(role)
    if role_day is None:
        fallback_day = _locked_source_day(source, role, drill_name)
        if fallback_day is None:
            return None, "", "missing_authoritative_countdown_label"
        role_day = fallback_day

    sections = _source_day_section_lines(source)
    if role_day not in sections:
        return role_day, "", "authoritative_day_missing_from_stage2"

    normalised_name = _normalise_locked_text(drill_name)
    lines = sections[role_day]
    name_index = next(
        (
            index
            for index, line in enumerate(lines)
            if normalised_name in _normalise_locked_text(line)
        ),
        None,
    )
    if name_index is None:
        return role_day, "", LOCKED_TACTICAL_WATCH_MISSING

    start = name_index
    if name_index > 0 and _normalise_locked_text(lines[name_index - 1]).startswith("why:"):
        start = name_index - 1

    end = len(lines)
    for index in range(name_index + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped:
            continue
        if _day_header_dday(stripped) is not None:
            end = index
            break
        if stripped.lower().startswith("why:"):
            end = index
            break
        if stripped.startswith("- ") and not lines[index].startswith((" ", "\t")):
            end = index
            break

    return role_day, "\n".join(lines[start:end]), None


def _locked_drill_card_strings(
    plan: dict[str, Any], source: str, role: dict[str, Any], drill_name: str
) -> list[str]:
    """Collect only the matching block and its owning session's mapped fields.

    The day is taken from the role's own authoritative countdown label, falling
    back to the Stage 2 text only when the role carries none. Locating a
    server-owned card by searching model prose would make the card invisible
    precisely when Stage 2 omitted the drill -- which is the case the deterministic
    merge exists to cover.
    """
    source_day = _authoritative_locked_day(role)
    if source_day is None:
        source_day = _locked_source_day(source, role, drill_name)
    if source_day is None:
        return []
    normalised_name = _normalise_locked_text(drill_name)
    scoped: list[str] = []
    for week in plan.get("weeks") or []:
        if not isinstance(week, dict):
            continue
        for day in week.get("days") or []:
            if (
                not isinstance(day, dict)
                or _dday_num(day.get("countdown_label")) != source_day
            ):
                continue
            for session in day.get("sessions") or []:
                if not isinstance(session, dict):
                    continue
                matching_blocks = [
                    block
                    for block in session.get("blocks") or []
                    if isinstance(block, dict)
                    and _normalise_locked_text(block.get("display_name"))
                    == normalised_name
                ]
                for block in matching_blocks:
                    scoped.extend(_card_strings(block))
                    # These are the legitimate homes for Why and the session-level
                    # locked mindset. Other blocks and day/plan notes stay excluded.
                    scoped.extend(_card_strings(session.get("objective")))
                    scoped.extend(_card_strings(session.get("mindset_anchor")))
    return scoped


def _locked_content_violations(
    plan: dict[str, Any], source: str, planning_brief: Any
) -> list[str]:
    """Require every authoritative locked-drill line to survive in card fields."""
    violations: list[str] = []
    for role in _locked_roles(planning_brief):
        governance = role.get("governance") or {}
        drill_name = str(
            governance.get("selected_drill_name")
            or (role.get("preferred_exercise_names") or [""])[0]
            or "locked drill"
        )
        source_day, drill_source_text, drill_source_issue = _locked_source_drill_text(
            source, role, drill_name
        )
        if drill_source_issue == "missing_authoritative_countdown_label":
            # Without a day there is nothing to verify against on either side.
            if _is_mandatory_locked_role(role):
                violations.append(
                    f"{LOCKED_CONTENT}: {drill_name!r} {drill_source_issue} on unknown day"
                )
            continue

        if drill_source_issue is not None:
            # Stage 2 did not author this drill, and it was never its job to:
            # the deterministic role is the owner and merge_locked_structured_content
            # projects it into the card before this check runs. So verify the card
            # against the role itself. A genuinely missing card still fails below --
            # the requirement moves to the real owner, it does not disappear.
            scoped_source_text = _normalise_locked_text(role["display_text"])
        else:
            scoped_source_text = _normalise_locked_text(drill_source_text)
        card_texts = [
            _normalise_locked_text(text)
            for text in _locked_drill_card_strings(plan, source, role, drill_name)
        ]
        required: list[tuple[str, str]] = [("selected drill name", drill_name)]
        for raw_line in str(role["display_text"]).splitlines():
            line = raw_line.strip().lstrip("- ").strip()
            if not line:
                continue
            match = re.match(
                r"((?:Step\s+\d+)|Why|Intent|Focus|Reset|Anchor|Purpose|Progress):\s*(.+)",
                line,
                re.I,
            )
            if match:
                required.append((match.group(1), match.group(2)))

        missing: list[str] = []
        for label, expected in required:
            normalised = _normalise_locked_text(expected)
            # The approved plan text is the authority. A stale brief line that
            # did not reach it must not create a new requirement here.
            if not normalised or normalised not in scoped_source_text:
                continue
            if not any(normalised in card_text for card_text in card_texts):
                missing.append(label)
        if missing:
            violations.append(
                f'{LOCKED_CONTENT}: {drill_name!r} lost required source content: '
                + ", ".join(missing)
            )
    return violations


@dataclass(frozen=True)
class LockedSourceRepairIssue:
    countdown_label: str | None
    block_name: str
    reason: str


@dataclass
class LockedSourceRepairResult:
    source_markdown: str
    applied: list[str] = field(default_factory=list)
    unresolved: list[LockedSourceRepairIssue] = field(default_factory=list)


# Banked systems whose locked roles the server repairs into Stage 2 text.
# ``role_key`` -> (bank content field, plan-text session heading). Kept in step
# with ``structured_plan_locked_merge._LOCKED_CONTENT_SPECS``: same two systems,
# same headings, so the repaired text and the repaired card agree.
_LOCKED_SOURCE_SYSTEMS = {
    "tactical_watch": ("tactical_watch", "Tactical Focus"),
    "fight_visualization": ("fight_visualization", "Fight Visualisation"),
}
_LOCKED_MANDATORY_FLAGS = (
    "mandatory_tactical_watch",
    "mandatory_fight_visualization",
)


def _is_mandatory_locked_role(role: dict[str, Any]) -> bool:
    governance = role.get("governance") if isinstance(role.get("governance"), dict) else {}
    return governance.get("mandatory") is True or any(
        role.get(flag) is True for flag in _LOCKED_MANDATORY_FLAGS
    )


def _locked_source_system(role: dict[str, Any]) -> tuple[str, str] | None:
    """Resolve which banked system owns this role, by role_key then by payload."""
    role_key = str(role.get("role_key") or "").strip().lower()
    if role_key in _LOCKED_SOURCE_SYSTEMS:
        return _LOCKED_SOURCE_SYSTEMS[role_key]
    if role_key:
        return None
    # A role_key-less locked role is identified by the bank object it carries.
    for content_field, heading in _LOCKED_SOURCE_SYSTEMS.values():
        if isinstance(role.get(content_field), dict):
            return content_field, heading
    return None


def _is_active_locked_tactical_watch_role(role: dict[str, Any]) -> tuple[bool, str | None]:
    governance = role.get("governance") if isinstance(role.get("governance"), dict) else {}
    if role.get("active") is False or role.get("is_active") is False:
        return False, "locked role is inactive"
    if role.get("skip") is True or role.get("omit") is True:
        return False, "locked role is suppressed"
    if governance.get("selected_drill_locked") is not True:
        return False, "selected drill is not locked"
    if governance.get("render_selected_drill_exactly") is not True:
        return False, "selected drill is not exact-render locked"
    if not _is_mandatory_locked_role(role):
        return False, "locked role is not mandatory"

    system = _locked_source_system(role)
    if system is None:
        return False, "locked role is not a repairable banked system"
    content_field, _heading = system
    category = str(role.get("category") or "").strip().lower()
    is_banked_role = (
        _is_mandatory_locked_role(role)
        or isinstance(role.get(content_field), dict)
        or category in {"", "tactical_watch", "mental", "mindset", "combat", "support_insert"}
    )
    if not is_banked_role:
        return False, f"locked role is not {content_field}"
    return True, None


def strip_locked_sessions_for_conversion(source_markdown: str, planning_brief: Any) -> str:
    """Keep server-owned session bodies out of the structured model's input."""
    if not isinstance(planning_brief, dict):
        return source_markdown
    owned: dict[int, set[str]] = {}
    for role in _locked_roles(planning_brief):
        active, _issue = _is_active_locked_tactical_watch_role(role)
        day = _authoritative_locked_day(role)
        system = _locked_source_system(role)
        if active and day is not None and system:
            owned.setdefault(day, set()).add(_normalise_locked_text(system[1]))
    if not owned:
        return source_markdown

    kept: list[str] = []
    skipping = False
    for line in source_markdown.splitlines():
        day = _day_header_dday(line)
        if day is not None:
            title = re.split(r"\s+[—–-]\s+|:\s+", line.strip(), maxsplit=1)[-1]
            title = re.sub(r"\s*\([^)]*\)\s*$", "", title)
            title = _normalise_locked_text(title)
            skipping = any(
                title == name or title.startswith(f"{name} ")
                for name in owned.get(day, set())
            )
        elif re.match(r"^(?:#{1,6}\s*)?(?:GPP|SPP|TAPER|FIGHT_WEEK)\s*[—–-]\s*Week\b", line.strip(), re.I):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def _remove_generic_visualization_source_alias(source: str, day: int) -> str:
    """Remove only a same-day generic visualisation copy beside the bank drill."""
    lines = source.splitlines()
    remove: set[int] = set()
    for start, line in enumerate(lines):
        if _day_header_dday(line) != day:
            continue
        # Split once so dashes inside the title ("Fight Visualisation - ...") survive.
        title = _normalise_locked_text(re.split(r"\s+[—–-]\s+|:\s+", line.strip(), maxsplit=1)[-1])
        if title != "fight visualisation (mental)" and not title.startswith("fight visualisation - "):
            continue
        end = start + 1
        while end < len(lines) and _day_header_dday(lines[end]) is None:
            if re.match(r"^(?:#{1,6}\s*)?(?:GPP|SPP|TAPER|FIGHT_WEEK)\s*[—–-]\s*Week\b", lines[end].strip(), re.I):
                break
            end += 1
        activities = [item.strip() for item in lines[start + 1:end] if item.strip().startswith("- ")]
        if len(activities) == 1 and _normalise_locked_text(activities[0]).startswith("- fight visualisation:"):
            remove.update(range(start, end))
    return "\n".join(line for index, line in enumerate(lines) if index not in remove)


def repair_locked_tactical_watch_source_text(
    source_markdown: str, planning_brief: Any
) -> LockedSourceRepairResult:
    """Insert missing mandatory locked banked sessions into Stage 2 text.

    Covers every system in ``_LOCKED_SOURCE_SYSTEMS`` -- the Tactical Watch and
    the Fight Visualisation countdown protocol. The name is kept for its callers.

    This keeps the existing source-of-truth model: structured conversion sees
    the repaired Stage 2 text first, then the structured merge projects the same
    governed role into JSON. Repair is intentionally narrow and fail-closed: it
    only appends exact ``display_text`` to a uniquely resolved existing D-day.
    """
    source = str(source_markdown or "")
    if not source.strip():
        return LockedSourceRepairResult(source)
    roles = [
        role
        for role in _locked_roles(planning_brief)
        if _is_mandatory_locked_role(role)
    ]
    if not roles:
        return LockedSourceRepairResult(source)

    result = LockedSourceRepairResult(source)
    for role in roles:
        governance = role.get("governance") or {}
        drill_name = str(
            governance.get("selected_drill_name")
            or (role.get("preferred_exercise_names") or [""])[0]
            or "locked drill"
        )
        role_day = _authoritative_locked_day(role)
        day_label = f"D-{role_day}" if role_day is not None else None
        is_active_tactical_watch, role_issue = _is_active_locked_tactical_watch_role(role)
        if not is_active_tactical_watch:
            result.unresolved.append(LockedSourceRepairIssue(day_label, drill_name, role_issue or "invalid role"))
            continue
        if role_day is None:
            result.unresolved.append(
                LockedSourceRepairIssue(day_label, drill_name, "missing authoritative countdown label")
            )
            continue
        content_field, session_heading = _locked_source_system(role) or (
            "tactical_watch",
            "Tactical Focus",
        )
        banked = role.get(content_field) if isinstance(role.get(content_field), dict) else {}
        watch_name = str(banked.get("name") or "").strip()
        if watch_name and _normalise_locked_text(watch_name) != _normalise_locked_text(drill_name):
            result.unresolved.append(
                LockedSourceRepairIssue(
                    day_label,
                    drill_name,
                    f"{content_field} name conflicts with locked drill",
                )
            )
            continue
        display_text = str(role.get("display_text") or "").strip()
        if not display_text:
            result.unresolved.append(
                LockedSourceRepairIssue(day_label, drill_name, "missing locked display_text")
            )
            continue
        if _normalise_locked_text(drill_name) not in _normalise_locked_text(display_text):
            result.unresolved.append(
                LockedSourceRepairIssue(day_label, drill_name, "locked display_text does not contain drill name")
            )
            continue
        _source_day, _drill_text, issue = _locked_source_drill_text(
            result.source_markdown, role, drill_name
        )
        if issue is None:
            continue
        if issue not in {LOCKED_TACTICAL_WATCH_MISSING, "authoritative_day_missing_from_stage2"}:
            result.unresolved.append(LockedSourceRepairIssue(day_label, drill_name, issue))
            continue

        lines = result.source_markdown.splitlines()
        insert_index, insert_issue = _source_day_insert_index(lines, role_day)
        if insert_issue is not None or insert_index is None:
            result.unresolved.append(LockedSourceRepairIssue(day_label, drill_name, insert_issue or "invalid day"))
            continue

        header_indices = _source_day_header_indices(lines, role_day)
        day_prefix = (
            re.search(r"\bD-\s*\d+\b(?:\s*\([^)]+\))?", lines[header_indices[0]], re.I)
            if header_indices else None
        )
        header_prefix = day_prefix.group(0) if day_prefix else day_label
        repair_block = ["", f"{header_prefix} — {session_heading}", display_text, ""]
        lines[insert_index:insert_index] = repair_block
        result.source_markdown = "\n".join(lines)
        result.applied.append(f"{day_label}: {drill_name}")
    for role in roles:
        if str(role.get("role_key") or "").lower() != "fight_visualization":
            continue
        day = _authoritative_locked_day(role)
        if day is None:
            continue
        governance = role.get("governance") or {}
        name = str(governance.get("selected_drill_name") or (role.get("fight_visualization") or {}).get("name") or "")
        if name and _locked_source_drill_text(result.source_markdown, role, name)[2] is None:
            result.source_markdown = _remove_generic_visualization_source_alias(result.source_markdown, day)
    return result


# Sessions and blocks the server assembles from deterministic state. Their ids are
# stamped by the assemblers that own them (structured_plan_locked_merge,
# structured_plan_deterministic_fallback), so a card can say which content the
# model authored and which the server did.
_SERVER_ASSEMBLED_ID_PREFIXES = ("locked-", "deterministic-")

# Roles whose existence and content the server owns end to end: it inserts them
# into the structured card, restores them in the deterministic fallback, and
# repairs them into the plan text. Stage 2 is not their author.
_SERVER_OWNED_ROLE_KEYS = frozenset(
    {
        "hard_sparring_day",
        "light_combat_day",
        "tactical_watch",
        "fight_visualization",
        "fight_day_protocol",
    }
)


def _server_owned_ddays(planning_brief: Any) -> set[int]:
    """Countdown days carrying a deterministic, server-owned role.

    Read from the same role map the assemblers use, so this introduces no second
    classification. A day here may legitimately appear in the card without
    appearing in Stage 2's text: the server put it there.
    """
    days: set[int] = set()
    if not isinstance(planning_brief, dict):
        return days
    role_map = planning_brief.get("weekly_role_map")
    if not isinstance(role_map, dict):
        return days
    for week in role_map.get("weeks") or []:
        if not isinstance(week, dict):
            continue
        for role in week.get("session_roles") or []:
            if not isinstance(role, dict):
                continue
            if str(role.get("role_key") or "").strip() not in _SERVER_OWNED_ROLE_KEYS:
                continue
            day = _authoritative_locked_day(role)
            if day is not None:
                days.add(day)
    return days


def _is_server_assembled(entry: Any, key: str) -> bool:
    if not isinstance(entry, dict):
        return False
    return str(entry.get(key) or "").startswith(_SERVER_ASSEMBLED_ID_PREFIXES)


_CALENDAR_ONLY_HEADLINES = frozenset({"", "rest", "rest day", "off", "day off"})


def _is_calendar_only_day(day: dict[str, Any], day_num: int | None) -> bool:
    """True for a day that carries no converter-authored claim to verify.

    The calendar spine writes every countdown day of the camp, and it now runs
    before this gate so locked cards have their authoritative day to land on.
    The days it adds are rest rows (no sessions, empty headline) or rows holding
    only server-assembled sessions. Stage 2 text rarely spells out rest days, so
    holding such a day to the source's countdown markers rejected nearly every
    card. A day with any model-authored session, or a model headline naming
    work, is still checked.
    """
    for session in day.get("sessions") or []:
        if isinstance(session, dict) and not _is_server_assembled(session, "session_id"):
            return False
    card = day.get("today_card")
    headline = str(card.get("headline") or "") if isinstance(card, dict) else ""
    headline = re.sub(r"[\s.]+", " ", headline).strip().casefold()
    return headline in _CALENDAR_ONLY_HEADLINES or (day_num == 0 and headline == "fight day")


@dataclass
class _SourceIndex:
    """What the source text proves: its D-days and which D-day owns each token."""

    source_tokens: set[str]
    token_days: dict[str, set[int]]
    source_ddays: set[int]
    server_owned_ddays: set[int]


def _source_index(source: str, planning_brief: Any) -> _SourceIndex | None:
    """``None`` when the source carries no countdown or no meaningful tokens."""
    if not _ANY_DDAY_RE.search(source):
        return None
    source_tokens = _meaningful(_tokens(source))
    if not source_tokens:
        return None
    sections = _source_day_sections(source)
    # Every D-day the source actually mentions (day headers + any inline D-N).
    source_ddays: set[int] = set(sections)
    for match in _DDAY_RE.finditer(source):
        num = _dday_num(match.group(0))
        if num is not None:
            source_ddays.add(num)
    return _SourceIndex(
        source_tokens=source_tokens,
        token_days=_source_token_days(sections),
        source_ddays=source_ddays,
        server_owned_ddays=_server_owned_ddays(planning_brief),
    )


def _plan_weeks(plan: dict[str, Any]) -> list[Any]:
    return plan.get("weeks") if isinstance(plan.get("weeks"), list) else []


def _calendar_only_ddays(weeks: list[Any]) -> set[int]:
    # Calendar-only days (see _is_calendar_only_day) are the spine's, not the
    # converter's; a week boundary landing on one is derived from that calendar.
    return {
        day_num
        for week in weeks
        if isinstance(week, dict)
        for day in week.get("days") or []
        if isinstance(day, dict)
        and (day_num := _dday_num(day.get("countdown_label"))) is not None
        and _is_calendar_only_day(day, day_num)
    }


def _countdown_unbacked(num: int | None, index: _SourceIndex, calendar_only: set[int]) -> bool:
    # A server-owned day is exempt: the deterministic assemblers place it from
    # the role map, so Stage 2's text is not its authority.
    return (
        num is not None
        and num not in index.source_ddays
        and num not in index.server_owned_ddays
        and num not in calendar_only
    )


def _is_model_session(session: Any) -> bool:
    # A server-assembled session was written from deterministic state; it is
    # verified against that state, never against model prose.
    return isinstance(session, dict) and not _is_server_assembled(session, "session_id")


def _block_violation(block: Any, day_num: int | None, index: _SourceIndex) -> str | None:
    """INTRODUCED / MISPLACED finding for one converter block, else ``None``."""
    if not isinstance(block, dict) or _is_server_assembled(block, "block_id"):
        return None
    if str(block.get("block_type")) not in _EXERCISE_BLOCK_TYPES:
        return None
    name = str(block.get("display_name") or "")
    name_tokens = _meaningful(_tokens(name))
    if not name_tokens:
        return None

    # 1) INTRODUCED: shares no meaningful token with the source.
    if not any(_present_in_source(tok, index.source_tokens) for tok in name_tokens):
        return f"{INTRODUCED}: exercise {name!r} not present in source text"

    # 2) MISPLACED: a token the source assigns to exactly one D-day, placed by
    #    the card under a different D-day.
    if day_num is None:
        return None
    for tok in sorted(name_tokens):
        days = index.token_days.get(tok)
        if days and len(days) == 1 and day_num not in days:
            (src_day,) = tuple(days)
            return (
                f"{MISPLACED}: {name!r} (token {tok!r}) is in source D-{src_day} "
                f"but card placed it in D-{day_num}"
            )
    return None


def _open_plan_membership_violations(
    plan: dict[str, Any], source: str, planning_brief: Any,
) -> list[str]:
    """Block converter-added physical exercises on undated, generated Open Plans.

    A dated plan has its D-day gate; athlete-owned custom plans have their own
    authority. A deterministic four-week plan owns its exact week membership;
    otherwise the approved session cards are a reusable weekly prescription.
    Candidate pools and week-goal prose are never exercise authority.
    """
    if not isinstance(planning_brief, dict) or planning_brief.get("custom_plan"):
        return []
    spec = planning_brief.get("open_plan_spec") or {}
    if not isinstance(spec, dict) or spec.get("plan_type") != "open_ongoing_system":
        return []

    from .services.exercise_media import normalize_exercise_key
    from fightcamp.structured_session_identity import identity_tokens
    from fightcamp.weekly_schedule_view import normalize_weekday

    def identity(name: Any) -> str:
        # Only strip an explicit *dose* in parentheses. (Speed), (Heavy), and
        # other movement qualifiers are part of the exercise's identity.
        name = re.sub(
            r"\s*\(\s*\d+\s*[x×]\s*\d+\s*(?:s|sec(?:onds?)?|min(?:utes?)?)\s*\)$",
            "", str(name or "").strip(), flags=re.I,
        )
        return normalize_exercise_key(name)

    programmed = spec.get("weekly_session_plan") or {}
    authoritative = (
        isinstance(programmed, dict)
        and programmed.get("authority") == "deterministic_open_development_block"
        and isinstance(programmed.get("weeks"), list)
    )
    scheduled = {int(w["week"]): w for w in programmed["weeks"] if isinstance(w, dict) and isinstance(w.get("week"), int)} if authoritative else {}
    source_sessions = {} if authoritative else _open_source_sessions(source)
    violations: list[str] = []
    for position, week in enumerate(_plan_weeks(plan), 1):
        if not isinstance(week, dict):
            continue
        week_number = week.get("week_index") or position
        for day in week.get("days") or []:
            if not isinstance(day, dict):
                continue
            weekday = normalize_weekday(day.get("weekday"))
            for session in day.get("sessions") or []:
                if not _is_model_session(session):
                    continue
                title_tokens = set(identity_tokens(session.get("title")))
                allowed: set[str] = set()
                if authoritative:
                    for item in scheduled.get(week_number, {}).get("sessions") or []:
                        if normalize_weekday(item.get("weekday")) == weekday:
                            allowed.update(identity(e.get("name")) for e in item.get("exercises") or [] if isinstance(e, dict))
                else:
                    for (source_week, source_day, source_title), sections in source_sessions.items():
                        if source_week not in (None, week_number) or source_day != weekday:
                            continue
                        heading_tokens = set(source_title)
                        if title_tokens and heading_tokens and not (
                            title_tokens <= heading_tokens or heading_tokens <= title_tokens
                        ):
                            continue
                        for section in sections:
                            for line in section[1:]:
                                match = re.match(r"^\s*[-*•]\s+(.+?)\s*(?::|\.\s|\s+[—–]\s+)", line)
                                if match:
                                    allowed.add(identity(match[1]))
                for block in session.get("blocks") or []:
                    if (not isinstance(block, dict)
                            or str(block.get("block_type")) not in _EXERCISE_BLOCK_TYPES
                            or _is_server_assembled(block, "block_id")
                            or block.get("athlete_change")
                            or block.get("prescription_source") == "athlete"):
                        continue
                    name = str(block.get("display_name") or "").strip()
                    if not identity(name) or identity(name) not in allowed:
                        violations.append(
                            f"{INTRODUCED}: open week {week_number} {weekday} "
                            f"exercise {name!r} is not authorised by its source session"
                        )
    return list(dict.fromkeys(violations))


def _check(structured_plan: Any, source_markdown: str, planning_brief: Any = None) -> list[str]:
    plan = structured_plan if isinstance(structured_plan, dict) else {}
    source = str(source_markdown or "")
    if not plan:
        return []
    locked_violations = _locked_content_violations(plan, source, planning_brief)
    prescription_violations = _explicit_prescription_violations(plan, source)
    open_violations = _open_plan_membership_violations(plan, source, planning_brief)
    if not _ANY_DDAY_RE.search(source):
        # Card-first hard gate: a card that claims a countdown structure cannot be
        # proven faithful against source text carrying no D-day marker at all, so
        # it is unverifiable and must not ship as the athlete-facing artifact
        # (the raw plan_text fallback / review hold takes over). A card that makes
        # no countdown claim has nothing to project, so the schema gate stands.
        if _card_claims_countdown(plan):
            return locked_violations + prescription_violations + open_violations + [
                f"{COUNTDOWN}: source text carries no D-day marker; "
                "card countdown is unverifiable"
            ]
        return locked_violations + prescription_violations + open_violations

    index = _source_index(source, planning_brief)
    if index is None:
        return locked_violations + prescription_violations

    violations: list[str] = [*locked_violations, *prescription_violations, *open_violations]
    weeks = _plan_weeks(plan)
    calendar_only = _calendar_only_ddays(weeks)
    for week in weeks:
        if not isinstance(week, dict):
            continue
        for label in (week.get("countdown_start"), week.get("countdown_end")):
            if _countdown_unbacked(_dday_num(label), index, calendar_only):
                violations.append(f"{COUNTDOWN}: week countdown {label!r} absent from source text")

        for day in week.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_num = _dday_num(day.get("countdown_label"))
            if _countdown_unbacked(day_num, index, calendar_only):
                violations.append(
                    f"{COUNTDOWN}: day countdown {day.get('countdown_label')!r} absent from source text"
                )
            for session in day.get("sessions") or []:
                if not _is_model_session(session):
                    continue
                for block in session.get("blocks") or []:
                    finding = _block_violation(block, day_num, index)
                    if finding:
                        violations.append(finding)

    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for item in violations:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


# ---------------------------------------------------------------------------
# Salvage: drop only what the source does not back.
#
# One invented exercise, one misplaced drill or one model session on a day the
# text never mentions used to reject the whole card, so a single slip put the
# athlete on the rebuilt fallback. The pruner removes exactly the content the
# checks above reject, using the same rules, and leaves everything else. It is a
# last resort after the model repair, and it refuses when so much would go that
# the card would no longer represent the plan.
# ---------------------------------------------------------------------------

#: At most this share of the converter's exercise blocks may be dropped ...
SALVAGE_MAX_DROPPED_SHARE = 0.2
#: ... except that a small card may always lose this many ...
SALVAGE_MIN_DROP_ALLOWANCE = 2
#: ... and never more than half: a card that was mostly unbacked is not the plan.
SALVAGE_MAX_DROPPED_FRACTION_CEILING = 0.5


def _exercise_block_count(sessions: list[Any]) -> int:
    return sum(
        1
        for session in sessions
        if _is_model_session(session)
        for block in session.get("blocks") or []
        if isinstance(block, dict)
        and not _is_server_assembled(block, "block_id")
        and str(block.get("block_type")) in _EXERCISE_BLOCK_TYPES
    )


def prune_unfaithful_content(
    structured_plan: Any, source_markdown: str, planning_brief: Any = None
) -> tuple[dict[str, Any] | None, list[str]]:
    """``(pruned_plan, removed)`` with unbacked converter content removed.

    Removes an exercise block the source does not name or places on another
    D-day, and every converter session on a day the source never mentions.
    Server-assembled sessions and blocks are never touched. Week boundaries are
    re-derived from the surviving days. Returns ``(None, removed)`` when the
    source cannot verify the card at all or when more than the allowed share of
    exercise blocks would go.
    """
    plan = copy.deepcopy(structured_plan) if isinstance(structured_plan, dict) else None
    source = str(source_markdown or "")
    index = _source_index(source, planning_brief) if plan else None
    if plan is None or index is None:
        return None, []

    weeks = _plan_weeks(plan)
    calendar_only = _calendar_only_ddays(weeks)
    removed: list[str] = []
    total_blocks = 0
    dropped_blocks = 0
    for week in weeks:
        if not isinstance(week, dict):
            continue
        for day in week.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_num = _dday_num(day.get("countdown_label"))
            sessions = [s for s in day.get("sessions") or [] if isinstance(s, dict)]
            total_blocks += _exercise_block_count(sessions)

            if _countdown_unbacked(day_num, index, calendar_only):
                model_sessions = [s for s in sessions if _is_model_session(s)]
                dropped_blocks += _exercise_block_count(model_sessions)
                for session in model_sessions:
                    removed.append(
                        f"{COUNTDOWN}: dropped session {session.get('title') or 'untitled'!r} "
                        f"on {day.get('countdown_label')!r}, a day the source does not have"
                    )
                sessions = [s for s in sessions if not _is_model_session(s)]
                card = day.get("today_card")
                if isinstance(card, dict):
                    card["headline"] = ""
            else:
                kept_sessions: list[dict[str, Any]] = []
                for session in sessions:
                    if not _is_model_session(session):
                        kept_sessions.append(session)
                        continue
                    blocks = [b for b in session.get("blocks") or [] if isinstance(b, dict)]
                    kept_blocks = []
                    for block in blocks:
                        finding = _block_violation(block, day_num, index)
                        if finding:
                            dropped_blocks += 1
                            removed.append(f"{finding}; block dropped")
                        else:
                            kept_blocks.append(block)
                    if blocks and not kept_blocks:
                        # Every exercise was unbacked: the session is empty shell.
                        removed.append(
                            f"dropped session {session.get('title') or 'untitled'!r} on "
                            f"{day.get('countdown_label')!r} after its blocks were removed"
                        )
                        continue
                    session["blocks"] = kept_blocks
                    kept_sessions.append(session)
                sessions = kept_sessions
            day["sessions"] = sessions
            if not sessions and str(day.get("day_type") or "") not in {"rest", "competition"}:
                day["day_type"] = "rest"

        labels = [
            num
            for day in week.get("days") or []
            if isinstance(day, dict) and (num := _dday_num(day.get("countdown_label"))) is not None
        ]
        if labels:
            week["countdown_start"] = f"D-{max(labels)}"
            week["countdown_end"] = f"D-{min(labels)}"

    allowance = max(SALVAGE_MIN_DROP_ALLOWANCE, int(total_blocks * SALVAGE_MAX_DROPPED_SHARE))
    if dropped_blocks > allowance or dropped_blocks > total_blocks * SALVAGE_MAX_DROPPED_FRACTION_CEILING:
        return None, removed
    return plan, removed
