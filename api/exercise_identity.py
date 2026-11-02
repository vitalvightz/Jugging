"""Stamp planner-owned exercise identity (``SessionBlock.exercise_key``) onto blocks.

The planner decides identity; the model decides wording. Every
``selected_exercise_assignments`` entry carries the ``exercise_key`` of its
canonical bank exercise from the moment it is selected
(``fightcamp.exercise_identity``). Stage 2 then renders each session however it
likes ("4 x 5 explosive throws"), and this module stamps the planner's key back
onto the block that renders each assignment, using the plan's own locked
membership rather than rediscovering identity from copy:

1. **Session -> role.** Each converted session is tied to the planner role it
   renders by the shared session-identity matcher (deterministic session id,
   then the role's athlete-facing label), on the role's own countdown day (or
   weekday, for open plans).
2. **Block -> assignment.** Within that session a block naming an assignment
   takes its key; the remaining blocks take the remaining assignments in planner
   order when the counts agree exactly (warm-up/cool-down blocks the converter
   added are set aside first). A count mismatch stamps nothing rather than guess.
3. **Support inserts.** A gap-fill insert prescribes one known movement
   (``support_insert_exercise_key``); its session's dose-copy blocks take it.
4. **Legacy fallbacks.** A block named exactly like a candidate-pool option, or
   a key a deterministic builder stamped from the block's own name, is kept.

The model is never a source of a key: anything not derived here is cleared.
Non-physical blocks (mindset, nutrition, coach-led sparring) never carry one.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Callable, Iterable, Mapping

from fightcamp.exercise_identity import support_insert_exercise_key
from fightcamp.fight_date_utils import parse_fight_date
from fightcamp.structured_session_identity import match_sessions_to_roles

from .services.exercise_media import (
    NON_PHYSICAL_BLOCK_TYPES,
    normalize_exercise_key,
    strip_dose_suffix,
)
from .structured_plan_calendar_spine import (
    _authoritative_ownership_maps,
    _effective_dday,
    _resolve_fight_date,
)

# Blocks a converter commonly adds around the planner's membership; set aside
# when matching the remaining blocks to assignments by planner order.
_FRAMING_BLOCK_TYPES = frozenset({"preparation", "mobility_activation", "cooldown_recovery"})

# Words that describe how an insert is dosed or framed rather than which
# movement it is; a session title is matched to an insert on the rest.
_FRAMING_WORDS = frozenset({
    "a", "aerobic", "and", "easy", "flow", "flush", "light", "long", "low",
    "min", "minute", "minutes", "of", "off", "on", "pace", "recovery", "rest",
    "round", "rounds", "rpe", "sec", "seconds", "short", "solo", "the", "work",
    "x",
})
_WORD_RE = re.compile(r"[a-z]+")
_NAME_PART_SPLIT_RE = re.compile(r"\s+[-–—:]\s+|:\s+")

_Member = tuple[str, str]  # (normalized assignment name, exercise key)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def block_exercise_key_for_name(name: Any) -> str | None:
    """The identity a deterministic builder stamps for a bank exercise name."""
    key = normalize_exercise_key(strip_dose_suffix(str(name or "")))
    return key or None


def _is_physical(block: Mapping[str, Any]) -> bool:
    return str(block.get("block_type") or "").strip() not in NON_PHYSICAL_BLOCK_TYPES


def _name_forms(name: Any) -> set[str]:
    """Slugs a block name can identify an assignment by.

    The whole name (dose suffix dropped) and each side of a labelled name
    ("Strength microdose - Pallof Press", "Finisher: Sled Push"). Always exact
    after normalization, so "Box Jump (Stick Landing)" never reads as "Box Jump".
    """
    text = strip_dose_suffix(str(name or ""))
    forms = {normalize_exercise_key(text)}
    forms.update(normalize_exercise_key(part) for part in _NAME_PART_SPLIT_RE.split(text))
    return {form for form in forms if form}


def _role_members(role: Mapping[str, Any]) -> list[_Member]:
    """The role's assignments in planner order, each with its planner key.

    The key is the one stamped at selection. A plan generated before keys were
    stamped derives it from the assignment's canonical bank name, which is the
    same deterministic rule, so stored plans resolve too.
    """
    members: list[_Member] = []
    for assignment in _as_list(role.get("selected_exercise_assignments")):
        if not isinstance(assignment, Mapping):
            continue
        name_key = normalize_exercise_key(assignment.get("name"))
        key = normalize_exercise_key(assignment.get("exercise_key")) or name_key
        if key:
            members.append((name_key, key))
    return members


def _iter_roles(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if value.get("role_key"):
            yield value
        for child in value.values():
            yield from _iter_roles(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_roles(child)


def _pool_keys(planning_brief: Mapping[str, Any]) -> set[str]:
    """Normalized names of every strength/conditioning option offered to this plan."""
    pools = planning_brief.get("candidate_pools")
    keys: set[str] = set()
    if not isinstance(pools, Mapping):
        return keys
    for pool in pools.values():
        if not isinstance(pool, Mapping):
            continue
        for group in ("strength_slots", "conditioning_slots"):
            for slot in _as_list(pool.get(group)):
                if not isinstance(slot, Mapping):
                    continue
                for option in [slot.get("selected"), *_as_list(slot.get("alternates"))]:
                    if isinstance(option, Mapping):
                        key = normalize_exercise_key(option.get("name"))
                        if key:
                            keys.add(key)
    return keys


def _day_roles_lookup(
    planning_brief: Mapping[str, Any],
) -> Callable[[Mapping[str, Any]], tuple[list[dict[str, Any]], int]]:
    """day -> (the planner roles scheduled on it, its D-day or -1)."""
    role_map = planning_brief.get("weekly_role_map")
    if not isinstance(role_map, Mapping):
        return lambda day: ([], -1)
    fight_date = parse_fight_date(_resolve_fight_date(dict(planning_brief)))
    if fight_date is not None:
        roles_by_dday = _authoritative_ownership_maps(dict(role_map))[3]

        def by_countdown(day: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
            d_day = _effective_dday(dict(day), fight_date)
            return (roles_by_dday.get(d_day, []), d_day) if d_day is not None else ([], -1)

        return by_countdown

    # Open/renewable plans: a role's day is its weekday.
    roles_by_weekday: dict[str, list[dict[str, Any]]] = {}
    for role in _iter_roles(role_map):
        weekday = str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").strip().lower()[:3]
        if weekday:
            roles_by_weekday.setdefault(weekday, []).append(dict(role))

    def by_weekday(day: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
        return roles_by_weekday.get(str(day.get("weekday") or "").strip().lower()[:3], []), -1

    return by_weekday


def _identity_words(text: Any) -> set[str]:
    words = set(_WORD_RE.findall(str(text or "").lower()))
    # "skip"/"skipping", "shadow"/"shadowboxing": compare on stems.
    return {word[:5] for word in words - _FRAMING_WORDS if len(word) >= 4}


def _support_insert(role: Mapping[str, Any]) -> tuple[set[str], str] | None:
    label = role.get("athlete_facing_label")
    key = support_insert_exercise_key(role.get("role_key"), label)
    return (_identity_words(label), key) if key else None


def _insert_for_session(
    session: Mapping[str, Any],
    role: Mapping[str, Any] | None,
    day_roles: list[dict[str, Any]],
) -> tuple[set[str], str] | None:
    """(stems, key) of the one support insert this session renders, else None."""
    if role is not None:
        return _support_insert(role)
    # A retitled insert ("Skipping flush (short)" for "Light Skipping Flush")
    # that the label matcher could not claim: its movement word still names it.
    title_stems = _identity_words(session.get("title"))
    named = {
        (frozenset(stems), key)
        for stems, key in filter(None, (_support_insert(r) for r in day_roles))
        if stems and stems <= title_stems
    }
    if len(named) != 1:
        return None
    stems, key = next(iter(named))
    return set(stems), key


def _stamp_session(
    session: Mapping[str, Any],
    role: Mapping[str, Any] | None,
    day_roles: list[dict[str, Any]],
    day_members: list[_Member],
    pool_keys: set[str],
) -> list[tuple[dict[str, Any], str | None]]:
    blocks = [block for block in _as_list(session.get("blocks")) if isinstance(block, dict)]
    resolved: list[str | None] = [None] * len(blocks)
    physical = [index for index, block in enumerate(blocks) if _is_physical(block)]

    # 1. A block naming one of its role's assignments, else one of the day's.
    members = _role_members(role) if role is not None else []
    open_members = list(members)
    for index in physical:
        forms = _name_forms(blocks[index].get("display_name"))
        hit = next((m for m in open_members if m[0] in forms), None)
        if hit is not None:
            open_members.remove(hit)
            resolved[index] = hit[1]
            continue
        day_hits = {key for name_key, key in day_members if name_key in forms}
        if len(day_hits) == 1:
            resolved[index] = next(iter(day_hits))

    # 2. The rest of the role's membership, by planner order, when exact.
    if open_members:
        unresolved = [index for index in physical if resolved[index] is None]
        if len(unresolved) != len(open_members):
            unresolved = [
                index for index in unresolved
                if str(blocks[index].get("block_type") or "") not in _FRAMING_BLOCK_TYPES
            ]
        if len(unresolved) == len(open_members):
            for index, (_, key) in zip(unresolved, open_members):
                resolved[index] = key

    # 3. A support insert: one movement dosed in rounds. A lone block is that
    # movement however it is worded; beside other blocks, only a block that is
    # pure dose copy or names the movement takes the identity.
    insert = _insert_for_session(session, role, day_roles) if not members else None
    if insert is not None:
        stems, key = insert
        open_blocks = [index for index in physical if resolved[index] is None]
        for index in open_blocks:
            if len(open_blocks) == 1 or _identity_words(blocks[index].get("display_name")) <= stems:
                resolved[index] = key

    # 4. Legacy: an exact candidate-pool name, or a deterministic builder's stamp.
    for index in physical:
        if resolved[index] is not None:
            continue
        own = block_exercise_key_for_name(blocks[index].get("display_name"))
        existing = normalize_exercise_key(blocks[index].get("exercise_key"))
        if own and (own in pool_keys or existing == own):
            resolved[index] = own
    return list(zip(blocks, resolved))


def reconcile_exercise_keys(structured_plan: Any, planning_brief: Any) -> Any:
    """Stamp the planner's ``exercise_key`` on every block whose identity is known.

    Returns the input object unchanged when nothing changes, else a deep copy.
    """
    if not isinstance(structured_plan, dict):
        return structured_plan
    brief = planning_brief if isinstance(planning_brief, Mapping) else {}
    pool_keys = _pool_keys(brief)
    day_roles_for = _day_roles_lookup(brief)

    repaired = copy.deepcopy(structured_plan)
    changed = False
    for week in _as_list(repaired.get("weeks")):
        if not isinstance(week, dict):
            continue
        for day in _as_list(week.get("days")):
            if not isinstance(day, dict):
                continue
            sessions = _as_list(day.get("sessions"))
            day_roles, d_day = day_roles_for(day)
            session_roles = match_sessions_to_roles(sessions, day_roles, d_day) if day_roles else {}
            day_members = [member for role in day_roles for member in _role_members(role)]
            for index, session in enumerate(sessions):
                if not isinstance(session, dict):
                    continue
                stamped = _stamp_session(
                    session, session_roles.get(index), day_roles, day_members, pool_keys
                )
                for block, key in stamped:
                    if block.get("prescription_source") == "athlete":
                        continue
                    if block.get("exercise_key") == key or (key is None and "exercise_key" not in block):
                        continue
                    block["exercise_key"] = key
                    changed = True
    return repaired if changed else structured_plan


# ---------------------------------------------------------------------------
# Server-owned names
#
# The model decides wording, but a block the server has tied to a planner
# exercise by ``exercise_key`` is that exercise: its name is the planner's, not
# whatever the converter wrote ("Lower-body power", "3 x 2 min easy rounds").
# Names follow keys only; nothing is renamed on a fuzzy title match.
# ---------------------------------------------------------------------------

_SERVER_BUILT_ID_PREFIXES = ("locked-", "deterministic-")


def _canonical_day_identity(
    day_roles: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """(key -> canonical name, key -> owning role) for one day's planner roles.

    A key two roles name differently is ambiguous and left out, so a block is
    only renamed when the day gives exactly one answer.
    """
    names: dict[str, str] = {}
    owners: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for role in day_roles:
        entries: list[tuple[str, str]] = []
        for assignment in _as_list(role.get("selected_exercise_assignments")):
            if not isinstance(assignment, Mapping):
                continue
            name = str(assignment.get("name") or "").strip()
            key = normalize_exercise_key(assignment.get("exercise_key")) or normalize_exercise_key(name)
            if name and key:
                entries.append((key, name))
        insert = _support_insert(role)
        label = str(role.get("athlete_facing_label") or "").strip()
        if insert is not None and label:
            entries.append((normalize_exercise_key(role.get("exercise_key")) or insert[1], label))
        for key, name in entries:
            if key in names and (names[key] != name or owners[key] is not role):
                ambiguous.add(key)
            names.setdefault(key, name)
            owners.setdefault(key, role)
    for key in ambiguous:
        names.pop(key, None)
        owners.pop(key, None)
    return names, owners


def reconcile_canonical_names(structured_plan: Any, planning_brief: Any) -> Any:
    """Give every planner-keyed block its canonical name, and its session the role's label.

    * A converter block carrying a planner ``exercise_key`` takes the planner's
      name for that exercise, unless its name already contains it as one part
      ("Speed microdose - Reactive Start Burst" keeps its label).
    * A converter session whose keyed blocks all belong to one planner role on
      that day takes the role's athlete-facing label as its title.

    Server-built sessions and blocks (``locked-`` / ``deterministic-`` ids) are
    already canonical and are left alone. Returns the input unchanged when
    nothing changes, else a deep copy. Never raises.
    """
    try:
        return _reconcile_canonical_names(structured_plan, planning_brief)
    except Exception:  # naming is presentation; never block the card on it
        return structured_plan


def _reconcile_canonical_names(structured_plan: Any, planning_brief: Any) -> Any:
    if not isinstance(structured_plan, dict) or not isinstance(planning_brief, Mapping):
        return structured_plan
    day_roles_for = _day_roles_lookup(planning_brief)
    renamed = copy.deepcopy(structured_plan)
    changed = False
    for week in _as_list(renamed.get("weeks")):
        if not isinstance(week, dict):
            continue
        for day in _as_list(week.get("days")):
            if not isinstance(day, dict):
                continue
            day_roles, _ = day_roles_for(day)
            if not day_roles:
                continue
            names, owners = _canonical_day_identity(day_roles)
            if not names:
                continue
            for session in _as_list(day.get("sessions")):
                if not isinstance(session, dict) or str(session.get("session_id") or "").startswith(
                    _SERVER_BUILT_ID_PREFIXES
                ):
                    continue
                session_roles: list[dict[str, Any]] = []
                for block in _as_list(session.get("blocks")):
                    if not isinstance(block, dict) or not _is_physical(block):
                        continue
                    key = normalize_exercise_key(block.get("exercise_key"))
                    if not key or key not in names:
                        continue
                    if not any(role is owners[key] for role in session_roles):
                        session_roles.append(owners[key])
                    if str(block.get("block_id") or "").startswith(_SERVER_BUILT_ID_PREFIXES):
                        continue
                    if key not in _name_forms(block.get("display_name")):
                        block["display_name"] = names[key]
                        changed = True
                if len(session_roles) == 1:
                    label = str(session_roles[0].get("athlete_facing_label") or "").strip()
                    if label and session.get("title") != label:
                        session["title"] = label
                        changed = True
    return renamed if changed else structured_plan
