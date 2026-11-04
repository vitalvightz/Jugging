"""Deterministically render governed banked roles in structured cards.

Stage 1 owns a banked prescription once its role governance marks the selected
drill as locked. Structured conversion may enrich the surrounding card, but it
must not omit, rename, or paraphrase those bank-owned fields. The role's
countdown day is authoritative: converter slips in the day itself (the day split
across two rows, a blank label on a dated row, or the day dropped outright) are
repaired onto that day, and within it this module repairs, moves, or creates the
governed session and block without another model call. Only a drill the
converter placed on a different day stays unresolved, since moving it would be
guessing.

Two banked systems share this pathway (see ``_LOCKED_CONTENT_SPECS``): the
Tactical Watch and the Fight Visualisation countdown protocol. They differ only
in which role field carries the bank object, the fixed session title, and how the
bank record's own fields map onto the card's block -- so the repair, move and
create logic below is written once and driven by that spec.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping

from fightcamp.fight_date_utils import parse_fight_date

from .structured_plan_calendar_spine import _resolve_fight_date, _rest_day, _valid_phase
from .structured_plan_sparring_reconcile import reconcile_coach_led_sparring_days


@dataclass(frozen=True)
class LockedContentSpec:
    """How one banked system projects onto a structured card."""

    content_field: str
    session_title: str
    session_slug: str
    session_type: str
    block_type: str


# Order is precedence: a role carrying more than one bank object (never expected)
# resolves to the first match, deterministically.
_LOCKED_CONTENT_SPECS = (
    LockedContentSpec(
        content_field="tactical_watch",
        session_title="Tactical Focus",
        session_slug="tactical-watch",
        session_type="skill",
        block_type="mindset",
    ),
    LockedContentSpec(
        content_field="fight_visualization",
        session_title="Fight Visualisation",
        session_slug="fight-visualization",
        session_type="skill",
        block_type="mindset",
    ),
)


def _spec_for_role(role: Mapping[str, Any]) -> tuple[LockedContentSpec, Mapping[str, Any]] | None:
    for spec in _LOCKED_CONTENT_SPECS:
        content = role.get(spec.content_field)
        if isinstance(content, Mapping):
            return spec, content
    return None


@dataclass(frozen=True)
class LockedMergeApplication:
    countdown_label: str
    block_name: str


@dataclass(frozen=True)
class LockedMergeIssue:
    countdown_label: str | None
    block_name: str
    reason: str


@dataclass
class LockedMergeResult:
    plan: dict[str, Any]
    applied: list[LockedMergeApplication] = field(default_factory=list)
    unresolved: list[LockedMergeIssue] = field(default_factory=list)


def _locked_roles(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        governance = value.get("governance")
        if isinstance(governance, Mapping) and governance.get("selected_drill_locked") is True:
            yield value
        for child in value.values():
            yield from _locked_roles(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _locked_roles(child)


def _normalise(value: Any) -> str:
    return " ".join(
        str(value or "")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .lower()
        .split()
    )


def _is_named_variant(value: Any, name: str, *, session_title: str | None = None) -> bool:
    """Recognise converter labels that explicitly name the locked bank drill."""
    label = _normalise(value)
    drill = _normalise(name)
    if label in {
        drill,
        f"{drill} mental rehearsal",
        f"{drill} visualisation",
        f"{drill} visualization",
    }:
        return True
    return bool(
        session_title
        and label in {
            f"{_normalise(session_title)}: {drill}",
            f"{_normalise(session_title)} - {drill}",
        }
    )


def _countdown(value: Any) -> str | None:
    match = re.search(r"D-\s*(\d+)", str(value or ""), re.IGNORECASE)
    return f"D-{int(match.group(1))}" if match else None


def _role_day(role: Mapping[str, Any]) -> str | None:
    # This is the same precedence used by the existing locked faithfulness path.
    for key in ("scheduled_countdown_label", "countdown_label", "countdown_display_label"):
        label = _countdown(role.get(key))
        if label:
            return label
    return None


def _days(plan: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    for week in plan.get("weeks") or []:
        if not isinstance(week, Mapping):
            continue
        for day in week.get("days") or []:
            if isinstance(day, dict):
                yield day


def _day_dday(day: Mapping[str, Any], fight_date: date | None) -> int | None:
    """A day's countdown distance from its label, else from its ISO date."""
    label = _countdown(day.get("countdown_label"))
    if label is not None:
        return int(label[2:])
    if fight_date is None:
        return None
    parsed = parse_fight_date(str(day.get("date") or "").strip())
    if parsed is None:
        return None
    delta = (fight_date - parsed).days
    return delta if delta >= 0 else None


def _drill_on_card(plan: Mapping[str, Any], names: set[str]) -> bool:
    return any(
        _is_named_variant(value, name)
        for day in _days(plan)
        for session in day.get("sessions") or []
        if isinstance(session, Mapping)
        for value in [session.get("title")]
        + [
            block.get("display_name")
            for block in session.get("blocks") or []
            if isinstance(block, Mapping)
        ]
        for name in names
    )


def _insert_day(plan: dict[str, Any], day: dict[str, Any], d_day: int, fight_date: date) -> None:
    """Place a new day in the week whose countdown span covers it, else the nearest."""
    best: tuple[int, list[Any]] | None = None
    for week in plan.get("weeks") or []:
        if not isinstance(week, dict) or not isinstance(week.get("days"), list):
            continue
        ddays = [
            value
            for value in (
                _day_dday(item, fight_date) for item in week["days"] if isinstance(item, Mapping)
            )
            if value is not None
        ]
        if not ddays:
            continue
        distance = 0 if min(ddays) <= d_day <= max(ddays) else min(abs(v - d_day) for v in ddays)
        if best is None or distance < best[0]:
            best = (distance, week["days"])
    days = best[1] if best else None
    if days is None:
        weeks = plan.get("weeks")
        if not isinstance(weeks, list) or not weeks or not isinstance(weeks[0], dict):
            return
        days = weeks[0].setdefault("days", [])
    index = next(
        (
            position
            for position, item in enumerate(days)
            if isinstance(item, Mapping)
            and (_day_dday(item, fight_date) is not None)
            and _day_dday(item, fight_date) < d_day
        ),
        len(days),
    )
    days.insert(index, day)


def _resolve_authoritative_day(
    plan: dict[str, Any],
    *,
    day_label: str,
    names: set[str],
    role: Mapping[str, Any],
    planning_brief: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Find the one card day for the role's countdown, repairing converter slips.

    The role's day is authoritative, so the card's day structure is repaired to
    it rather than rejecting the whole card:

    * a day split across two rows is coalesced into the first (no session lost),
    * a dated row with a blank/garbled label is matched by date and relabelled,
    * a dropped day is recreated as an empty day for the watch to land on.

    A dropped day is only recreated when the drill is nowhere else on the card.
    If the converter put it on another day, moving it would be guessing, so that
    stays unresolved and faithfulness rejects the card.
    """
    fight_date = parse_fight_date(_resolve_fight_date(dict(planning_brief)))
    d_day = int(day_label[2:])
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for week in plan.get("weeks") or []:
        if not isinstance(week, dict):
            continue
        for day in week.get("days") or []:
            if isinstance(day, dict) and _day_dday(day, fight_date) == d_day:
                matches.append((week, day))

    if matches:
        _, keep = matches[0]
        for week, duplicate in matches[1:]:
            keep.setdefault("sessions", []).extend(
                session for session in duplicate.get("sessions") or [] if isinstance(session, dict)
            )
            week["days"] = [item for item in week.get("days") or [] if item is not duplicate]
        if _countdown(keep.get("countdown_label")) is None:
            keep["countdown_label"] = day_label
        return keep, None

    if _drill_on_card(plan, names):
        return None, "locked drill is on a different structured day"
    if fight_date is None:
        return None, "structured day missing and fight date unknown"
    dated = [
        (distance, day)
        for day in _days(plan)
        if (distance := _day_dday(day, fight_date)) is not None
        and _valid_phase(day.get("phase_label"))
    ]
    neighbour_phase = (
        _valid_phase(min(dated, key=lambda pair: abs(pair[0] - d_day))[1].get("phase_label"))
        if dated
        else ""
    )
    day = _rest_day(d_day, fight_date, _valid_phase(role.get("camp_phase"), neighbour_phase))
    # It will carry the zero-load watch, so it is not a rest day.
    day["day_type"] = "low"
    _insert_day(plan, day, d_day, fight_date)
    return day, None


def _stable_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "watch"


def _duration(content: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise the bank's duration onto the card's single-value shape.

    The Tactical Watch bank stores one integer; the Fight Visualisation bank
    stores a ``[min, max]`` bound. The selected duration takes precedence on
    the card; older roles without that field retain the upper-bound fallback.
    """
    raw = content.get("prescribed_duration_min") or content.get("duration_min")
    if isinstance(raw, (list, tuple)) and raw:
        value = raw[-1]
    else:
        value = raw
    return {"value": value if isinstance(value, int) else 1, "unit": "minutes"}


def _mindset_anchor(content: Mapping[str, Any]) -> dict[str, Any]:
    """The card's coaching lines, one distinct claim per slot.

    A Tactical Watch carries a real four-part mindset block. The Fight
    Visualisation bank carries a single trusted ``cue`` instead, and filling
    every slot with it printed that one sentence three times on the card
    ("Focus", "Reset" and "Coach cue" all reading "Take the space, do not chase
    it."). A single cue is a single cue: it fills the coach-cue slot and the
    others stay empty rather than repeating it or inventing generic filler.
    """
    mindset = content.get("mindset")
    mindset = mindset if isinstance(mindset, Mapping) else {}
    cue = str(content.get("cue") or "").strip()
    intent = str(mindset.get("intent") or "").strip()
    focus = str(mindset.get("focus") or "").strip()
    reset = str(mindset.get("reset") or "").strip()
    anchor = str(mindset.get("anchor") or "").strip()
    if not (intent or focus or reset or anchor):
        return {
            "intent": cue,
            "focus_cue": "",
            "reset_cue": "",
            "confidence_anchor": None,
        }
    return {
        "intent": intent or cue,
        "focus_cue": focus,
        "reset_cue": reset,
        "confidence_anchor": anchor or None,
    }


def _restore_open_plan_tactical_watches(
    plan: dict[str, Any], planning_brief: Mapping[str, Any]
) -> None:
    """Restore open-plan Tactical Watch fields from the deterministic rotation."""
    spec = planning_brief.get("open_plan_spec")
    spec = spec if isinstance(spec, Mapping) else {}
    tactical = spec.get("tactical_watch")
    tactical = tactical if isinstance(tactical, Mapping) else {}
    rotation = tactical.get("weekly_rotation")
    if not isinstance(rotation, (list, tuple)):
        return

    for entry in rotation:
        if not isinstance(entry, Mapping):
            continue
        watch = entry.get("tactical_watch")
        if not isinstance(watch, Mapping):
            continue
        name = str(entry.get("name") or watch.get("name") or "").strip()
        if not name:
            continue

        targets = [
            (session, block)
            for day in _days(plan)
            for session in day.get("sessions") or []
            if isinstance(session, dict)
            for block in session.get("blocks") or []
            if isinstance(block, dict)
            and _normalise(block.get("display_name")) == _normalise(name)
        ]
        if len(targets) != 1:
            continue

        session, block = targets[0]
        session["session_type"] = "skill"
        session["objective"] = watch.get("why")
        session["mindset_anchor"] = _mindset_anchor(watch)
        block.update(
            {
                "block_type": "mindset",
                "display_name": watch.get("name") or name,
                "duration": _duration(watch),
                "coaching_cues": [
                    str(value) for value in watch.get("instructions") or []
                ],
                "execution_instructions": " ".join(str(value) for value in watch.get("instructions") or []) or None,
                "purpose": watch.get("why"),
            }
        )
        if watch.get("progress"):
            block["progression_rule"] = watch["progress"]


def _new_watch_block(
    *, day_label: str, name: str, watch: Mapping[str, Any], spec: LockedContentSpec
) -> dict[str, Any]:
    return {
        "block_id": f"locked-{_stable_id(day_label)}-{_stable_id(name)}",
        "block_type": spec.block_type,
        "display_name": name,
        "duration": _duration(watch),
        "coaching_cues": [],
        "regression_options": [],
        "substitutions": [],
    }


def _new_watch_session(
    *,
    day_label: str,
    title: str,
    name: str,
    watch: Mapping[str, Any],
    spec: LockedContentSpec,
) -> dict[str, Any]:
    return {
        "session_id": f"locked-{_stable_id(day_label)}-{spec.session_slug}",
        "session_type": spec.session_type,
        "title": title,
        "objective": str(watch.get("why") or "Review the tactical plan."),
        "completion_status": "not_started",
        "mindset_anchor": _mindset_anchor(watch),
        "blocks": [],
    }


def _is_safe_named_watch_shell(
    session: Mapping[str, Any], authoritative_names: set[str]
) -> bool:
    """Return whether a drill-titled session can safely become the Watch.

    The structured converter occasionally emits the locked drill name as its
    own otherwise-empty session. It is the same governed content, not a second
    athlete session. Only reuse or remove that shell when it has not been
    started and carries no independent blocks.
    """
    if not _is_unstarted(session):
        return False

    blocks = session.get("blocks") or []
    return isinstance(blocks, list) and all(
        isinstance(block, Mapping)
        and any(
            _is_named_variant(block.get("display_name"), name)
            for name in authoritative_names
        )
        for block in blocks
    )


def _is_unstarted(session: Mapping[str, Any]) -> bool:
    return _normalise(session.get("completion_status")) in {"", "not_started", "not started"}


def _is_generic_visualization_shell(session: Mapping[str, Any], name: str) -> bool:
    """Only remove an unstarted converter session explicitly titled as a visualisation."""
    if not _is_unstarted(session):
        return False
    title = _normalise(session.get("title"))
    if not title.startswith("fight visualisation -"):
        return False
    blocks = session.get("blocks") or []
    return isinstance(blocks, list) and all(
        isinstance(block, Mapping)
        and _normalise(block.get("block_type")) in {"", "mindset"}
        and (
            _normalise(block.get("display_name")) == "fight visualisation"
            or _is_named_variant(block.get("display_name"), name)
        )
        for block in blocks
    )


def merge_locked_structured_content(
    structured_plan: dict[str, Any], planning_brief: Any
) -> LockedMergeResult:
    """Apply locked fields on their authoritative day.

    The role's countdown day is authoritative: a split, unlabelled or dropped
    day is repaired onto it (see ``_resolve_authoritative_day``), while a drill
    the converter placed on another day stays unresolved. Inside that day,
    Stage 1 is authoritative, so harmless converter drift (renamed/moved/missing
    Tactical Watch structure) is repaired deterministically instead of rejecting
    the entire athlete card.
    """
    plan = copy.deepcopy(structured_plan)
    result = LockedMergeResult(plan=plan)
    # ``optional`` is server-owned: a locked optional role or trusted custom
    # recovery can set it; a model cannot make real training skippable.
    for day in _days(plan):
        for session in day.get("sessions") or []:
            if isinstance(session, dict):
                if (isinstance(planning_brief, Mapping) and planning_brief.get("custom_plan")
                        and session.get("prescription_source") == "athlete"
                        and session.get("session_type") == "recovery"):
                    session["optional"] = True
                else:
                    session.pop("optional", None)
    if not isinstance(planning_brief, Mapping):
        return result

    _restore_open_plan_tactical_watches(plan, planning_brief)

    for role in _locked_roles(planning_brief):
        governance = role.get("governance")
        governance = governance if isinstance(governance, Mapping) else {}
        resolved = _spec_for_role(role)
        spec, watch = resolved if resolved is not None else (None, None)
        name = str(
            governance.get("selected_drill_name")
            or (watch or {}).get("name")
            or "locked drill"
        )
        day_label = _role_day(role)
        if watch is None or spec is None:
            result.unresolved.append(
                LockedMergeIssue(day_label, name, "missing locked bank metadata")
            )
            continue
        if day_label is None:
            result.unresolved.append(LockedMergeIssue(None, name, "missing authoritative countdown label"))
            continue

        authoritative_names = {
            _normalise(governance.get("selected_drill_name")),
            _normalise(watch.get("name")),
        } - {""}
        target_day, day_issue = _resolve_authoritative_day(
            plan,
            day_label=day_label,
            names=authoritative_names,
            role=role,
            planning_brief=planning_brief,
        )
        if target_day is None:
            result.unresolved.append(LockedMergeIssue(day_label, name, day_issue or "structured day not resolved"))
            continue
        matching_days = [target_day]
        sessions = [
            session
            for session in matching_days[0].get("sessions") or []
            if isinstance(session, dict)
        ]
        # This is fixed product copy. The selected drill is the card's block,
        # never an alternate session title supplied by structured conversion.
        display_title = spec.session_title
        session_title = _normalise(display_title)
        watch_sessions = [
            session
            for session in sessions
            if _normalise(session.get("title")) == session_title
        ]
        if len(watch_sessions) > 1:
            result.unresolved.append(
                LockedMergeIssue(
                    day_label,
                    name,
                    f"{spec.session_title} session not uniquely resolved",
                )
            )
            continue

        named_watch_sessions = [
            session
            for session in sessions
            if any(
                _is_named_variant(session.get("title"), candidate, session_title=display_title)
                for candidate in authoritative_names
            )
            and _normalise(session.get("title")) != session_title
        ]
        if len(named_watch_sessions) > 1:
            result.unresolved.append(
                LockedMergeIssue(
                    day_label,
                    name,
                    f"named {spec.session_title} session not uniquely resolved",
                )
            )
            continue
        named_watch_session = named_watch_sessions[0] if named_watch_sessions else None
        reusable_named_watch = (
            named_watch_session
            if named_watch_session
            and _is_safe_named_watch_shell(named_watch_session, authoritative_names)
            else None
        )

        targets = [
            (session, block)
            for session in sessions
            if _is_unstarted(session) or any(session is watch_session for watch_session in watch_sessions)
            for block in session.get("blocks") or []
            if isinstance(block, dict)
            and any(
                _is_named_variant(block.get("display_name"), candidate)
                for candidate in authoritative_names
            )
        ]
        exact_targets = [
            pair for pair in targets
            if _normalise(pair[1].get("display_name")) in authoritative_names
        ]
        if len(exact_targets) > 1:
            result.unresolved.append(
                LockedMergeIssue(day_label, name, "locked block not uniquely resolved")
            )
            continue

        # The converter can emit both "Tactical Picture mental rehearsal" and
        # the exact bank title, or only qualified copies. Keep the exact block,
        # else the first qualified one; every other copy is the same locked
        # prescription with invented duration and cues, and the kept block is
        # overwritten from the bank below.
        emptied_alias_owners: list[dict[str, Any]] = []
        kept_targets = exact_targets or targets[:1]
        if len(targets) > len(kept_targets):
            for owner, duplicate in targets:
                if _is_unstarted(owner) and all(
                    duplicate is not kept_block for _, kept_block in kept_targets
                ):
                    owner["blocks"] = [
                        item for item in owner.get("blocks") or [] if item is not duplicate
                    ]
                    if not owner["blocks"] and _is_safe_named_watch_shell(owner, authoritative_names):
                        emptied_alias_owners.append(owner)
        targets = kept_targets

        if watch_sessions:
            session = watch_sessions[0]
        elif reusable_named_watch:
            # The converter used the locked drill name as the session title.
            # Reuse it as the canonical Watch instead of creating a twin card.
            session = reusable_named_watch
        elif targets:
            # Keep unrelated same-day work intact. Create the governed session,
            # then move only the authoritative block into it below.
            session = _new_watch_session(
                day_label=day_label,
                title=display_title,
                name=name,
                watch=watch,
                spec=spec,
            )
            matching_days[0].setdefault("sessions", []).append(session)
            sessions.append(session)
        else:
            session = _new_watch_session(
                day_label=day_label,
                title=display_title,
                name=name,
                watch=watch,
                spec=spec,
            )
            matching_days[0].setdefault("sessions", []).append(session)
            sessions.append(session)

        if targets:
            owner, block = targets[0]
            if owner is not session:
                owner["blocks"] = [item for item in owner.get("blocks") or [] if item is not block]
                session.setdefault("blocks", []).append(block)
        else:
            # A generic mindset block is not proof that it represents this
            # governed drill. Preserve ambiguous/unrelated content and append
            # the authoritative block instead of destructively repurposing it.
            block = _new_watch_block(
                day_label=day_label, name=name, watch=watch, spec=spec
            )
            session.setdefault("blocks", []).append(block)

        mindset = watch.get("mindset")
        mindset = mindset if isinstance(mindset, Mapping) else {}
        anchor = _mindset_anchor(watch)
        session["title"] = display_title
        session["objective"] = watch.get("why")
        if spec.content_field in {"fight_visualization", "tactical_watch"}:
            # Locked placement does not make mental work owed by the athlete.
            session["optional"] = True
            if watch.get("optional") is True:
                session["objective"] = f"Optional. {watch.get('why') or ''}".strip()
        else:
            session.pop("optional", None)
        session["mindset_anchor"] = anchor
        # ``coaching_cues`` carries the bank's instructions. The Fight
        # Visualisation bank's trusted ``cue`` (and its optional immediate
        # pre-bout version) are part of the prescription, so they ride along
        # rather than being dropped on the floor.
        coaching_cues = [str(value) for value in watch.get("instructions") or []]
        for extra_field, prefix in (("cue", "Cue"), ("pre_bout", "Pre-bout")):
            extra = str(watch.get(extra_field) or "").strip()
            if extra:
                coaching_cues.append(f"{prefix}: {extra}")
        block.update(
            {
                "display_name": watch.get("name") or governance.get("selected_drill_name"),
                "duration": _duration(watch),
                "coaching_cues": coaching_cues,
                "purpose": mindset.get("context") or watch.get("why"),
            }
        )
        # Only the Tactical Watch bank carries a progression rule. Writing a
        # null for a system that has no such concept would just blank a field.
        if watch.get("progress"):
            block["progression_rule"] = watch["progress"]

        if emptied_alias_owners:
            matching_days[0]["sessions"] = [
                item
                for item in matching_days[0].get("sessions") or []
                if item is session or all(item is not owner for owner in emptied_alias_owners)
            ]

        if spec.content_field == "fight_visualization":
            matching_days[0]["sessions"] = [
                item
                for item in matching_days[0].get("sessions") or []
                if item is session or not _is_generic_visualization_shell(item, name)
            ]

        if (
            watch_sessions
            and reusable_named_watch is not None
            and reusable_named_watch is not session
        ):
            # Remove the old converter shell only after its authoritative
            # content has been repaired on the canonical Watch. The safety
            # check above protects completed or independently populated work.
            matching_days[0]["sessions"] = [
                item
                for item in matching_days[0].get("sessions") or []
                if item is not reusable_named_watch
            ]
        result.applied.append(LockedMergeApplication(day_label, str(block["display_name"])))

    from .optional_sessions import mark_optional_support_inserts

    mark_optional_support_inserts(plan, planning_brief)
    return result



def _remove_repeated_tactical_physical_blocks(plan: dict[str, Any]) -> None:
    """Keep a physical prescription in its training session, not Tactical Focus."""
    dose_fields = ("sets", "reps", "rounds", "work", "duration", "distance", "load")
    coaching_fields = {"block_id", "coaching_cues", "stop_rules", "why_today"}
    for week in plan.get("weeks") or []:
        for day in week.get("days") or []:
            sessions = day.get("sessions") or []
            physical = [
                block for session in sessions
                if _normalise(session.get("title")) != "tactical focus"
                for block in session.get("blocks") or []
                if block.get("block_type") != "mindset"
            ]
            for session in sessions:
                if _normalise(session.get("title")) != "tactical focus":
                    continue
                blocks = session.get("blocks") or []
                # Do not leave an empty Tactical Focus or discard unique work.
                if not any(block.get("block_type") == "mindset" for block in blocks):
                    continue
                kept = []
                for block in blocks:
                    identity = block.get("exercise_key") or _normalise(block.get("display_name"))
                    host = next((
                        other for other in physical
                        if identity
                        and (other.get("exercise_key") or _normalise(other.get("display_name"))) == identity
                        and any(block.get(field) is not None for field in dose_fields)
                        and all(block.get(field) == other.get(field)
                                for field in block.keys() | other.keys() if field not in coaching_fields)
                    ), None)
                    if host is None or block.get("block_type") == "mindset":
                        kept.append(block)
                        continue
                    for name in ("coaching_cues", "stop_rules"):
                        if block.get(name):
                            values = list(host.get(name) or [])
                            host[name] = values + [item for item in block[name] if item not in values]
                session["blocks"] = kept


def merge_planner_owned_structured_content(
    structured_plan: dict[str, Any], planning_brief: Any
) -> LockedMergeResult:
    """Carry all planner-owned session and contact truth into one final card.

    Contact reconciliation must run after locked sessions are present. Otherwise
    adding Tactical Focus can turn a contact-only day into a session day while
    leaving its contact truth stranded in the now-hidden day headline.
    """
    from .structured_plan_deterministic_fallback import reconcile_open_development_sessions

    structured_plan = reconcile_open_development_sessions(structured_plan, planning_brief)
    result = merge_locked_structured_content(structured_plan, planning_brief)
    if not (isinstance(planning_brief, dict) and planning_brief.get("custom_plan")):
        _remove_repeated_tactical_physical_blocks(result.plan)

    reconcile_coach_led_sparring_days(result.plan, planning_brief)
    return result
