"""Low-cost support inserts for normal fight-camp weeks."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .calendar_context import CalendarLegalityView, weekly_role_map_legality
from .combat_load_policy import PlacementDirective
from .coordination_support_library import (
    build_coordination_display_text,
    coordination_support_metadata,
    select_coordination_support,
)
from .physical_occupancy_fill import fill_physical_occupancy
from .fight_visualization_library import (
    CAMP_VISUALIZATION_FIRST_DAY,
    OPTIONAL_FLAG,
    camp_visualization_role,
    choose_camp_visualization_days,
)
from .gap_fill_inserts import (
    PHYSICAL_INSERTS,
    camp_visualization_day_tier,
    _new_usage_ledger,
    _priority_contribution,
    _record_insert_usage,
    build_target_coverage_state,
    filler_target_capabilities,
    select_gap_fill_insert,
)
from .mental_training_policy import calendar_week_bucket, govern_mental_roles, mental_training_policy
from .normalization import WEEKDAY_ORDER, clean_list
from .tactical_watch_library import (
    build_watch_display_text,
    extract_tactical_style,
    TacticalWatchBankExhausted,
    select_tactical_watch,
    watch_metadata,
)

_FIGHT_PHASE_CAPS = {"GPP": 1, "SPP": 2, "TAPER": 1}
_LEGACY_PHASE_CAPS = {"SPP": 2, "TAPER": 1}
_MAX_SHARED_DAY_FILLERS = 1

# Representative coordination-support role used only to classify the coordination
# insert through combat_load_policy (low-load physical) when scoring candidate
# slots. combat_load_policy owns the resulting legality.
_COORDINATION_LEGALITY_ROLE = {
    "role_key": "coordination_support",
    "category": "support_insert",
    "stress_class": "support",
    "cost_class": "low",
    "governance": {"meaningful_stress": False},
}
_CANONICAL_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _canonical_day(value: Any) -> str:
    index = WEEKDAY_ORDER.get(str(value or "").strip().lower())
    return _CANONICAL_WEEKDAYS[index] if index is not None else ""


def _calendar_d_day(week: dict[str, Any], weekday: str) -> int | None:
    canonical = _canonical_day(weekday)
    for day in week.get("calendar_days") or []:
        if isinstance(day, dict) and _canonical_day(day.get("weekday")) == canonical:
            try:
                return int(day.get("d_day"))
            except (TypeError, ValueError):
                return None
    return None


def _week_is_compressed(week: dict[str, Any]) -> bool:
    compression = week.get("intentional_compression")
    return bool(compression.get("active")) if isinstance(compression, dict) else bool(compression)


def _has_future_fight(athlete_model: dict[str, Any]) -> bool:
    try:
        return int(athlete_model.get("days_until_fight")) > 0
    except (TypeError, ValueError):
        return False


def _seed_programme_coverage_roles(
    weekly_role_map: dict[str, Any],
    usage_ledger: dict[str, Any],
) -> list[dict[str, Any]]:
    """Seed one normal-camp coverage context from the authoritative programme.

    Normal filler placement is week-local, but target coverage is programme-level:
    a real role scheduled elsewhere in the visible programme still counts, and a
    filler inserted earlier must reduce remaining support need for later weeks.
    """
    coverage_roles = [
        role
        for week in weekly_role_map.get("weeks", []) or []
        if isinstance(week, dict)
        for role in week.get("session_roles", []) or []
        if isinstance(role, dict)
    ]
    usage_ledger["coverage_roles"] = coverage_roles
    return coverage_roles


def _role_day_counts(session_roles: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for role in session_roles:
        if not isinstance(role, dict) or role.get("camp_week_filler"):
            continue
        day = _canonical_day(role.get("scheduled_day_hint"))
        if day:
            counts[day] = counts.get(day, 0) + 1
    return counts


def _week_physical_filler_count(session_roles: list[Any]) -> int:
    return sum(
        1
        for role in session_roles
        if isinstance(role, dict)
        and role.get("camp_week_filler")
        and str(role.get("role_key") or "") in PHYSICAL_INSERTS
    )


def _decorate_filler(insert: dict[str, Any], day: str, d_day: int) -> None:
    day_title = str(day).strip().title()
    insert["session_index"] = 0
    insert["scheduled_day_hint"] = day_title
    insert["real_weekday"] = day_title
    insert["countdown_display_label"] = f"D-{d_day} ({day_title})"
    insert["camp_week_filler"] = True


def _select_filler_for_day(
    week: dict[str, Any],
    athlete_model: dict[str, Any],
    day: str,
    *,
    weekly_role_map: dict[str, Any],
    week_ordinal: int,
    usage_ledger: dict[str, Any],
    allow_physical: bool,
    prefer_conditioning: bool = False,
) -> tuple[dict[str, Any], int] | None:
    """Select the real generic filler candidate for one day without placing it."""
    d_day = _calendar_d_day(week, day)
    if d_day is None or d_day in set(week.get("late_fight_tail_days") or []):
        return None

    from .physical_session_frequency import requested_training_frequency, would_add_excess_training_day
    allow_physical = allow_physical and not would_add_excess_training_day(
        weekly_role_map, requested_training_frequency(athlete_model), d_day,
    )
    legality = weekly_role_map_legality(weekly_role_map, week, week_ordinal)
    on_hard_sparring_day = d_day in legality.contact_offsets()
    coverage_roles = usage_ledger.setdefault("coverage_roles", [])
    insert = select_gap_fill_insert(
        athlete_model,
        d_day,
        on_hard_sparring_day=on_hard_sparring_day,
        usage_ledger=usage_ledger,
        legality=legality,
        scheduled_roles=coverage_roles,
        force_conditioning=prefer_conditioning,
        allow_physical=allow_physical,
    )
    if insert is None:
        return None
    if not allow_physical:
        from .physical_session_frequency import is_physical_session_role
        if is_physical_session_role({**insert, "countdown_offset": d_day}):
            return None
    if legality.role_is_forbidden(insert, d_day):
        return None
    return insert, d_day


def _place_filler(
    week: dict[str, Any],
    session_roles: list[dict[str, Any]],
    athlete_model: dict[str, Any],
    day: str,
    *,
    weekly_role_map: dict[str, Any],
    week_ordinal: int,
    usage_ledger: dict[str, Any],
    allow_physical: bool,
    prefer_conditioning: bool = False,
) -> dict[str, Any] | None:
    candidate_ledger = deepcopy(usage_ledger)
    candidate_ledger["coverage_roles"] = list(usage_ledger.get("coverage_roles", []))
    selected = _select_filler_for_day(
        week,
        athlete_model,
        day,
        weekly_role_map=weekly_role_map,
        week_ordinal=week_ordinal,
        usage_ledger=candidate_ledger,
        allow_physical=allow_physical,
        prefer_conditioning=prefer_conditioning,
    )
    if selected is None:
        return None
    insert, d_day = selected
    usage_ledger.update(candidate_ledger)
    _decorate_filler(insert, day, d_day)
    session_roles.append(insert)
    usage_ledger.setdefault("coverage_roles", []).append(insert)
    _record_insert_usage(usage_ledger, str(insert.get("role_key") or ""), d_day)
    return insert


def _unused_day_is_fillable(day_entry: Any) -> bool:
    if not isinstance(day_entry, dict):
        return False
    day = str(day_entry.get("day") or "").strip()
    role = str(day_entry.get("role") or "").strip()
    return bool(
        day
        and role in {"off_day", "recovery_only_day"}
        and not day_entry.get("low_aerobic_cap_skipped")
    )


def _fill_week(
    week: dict[str, Any],
    athlete_model: dict[str, Any],
    cap: int,
    usage_ledger: dict[str, Any],
    *,
    weekly_role_map: dict[str, Any],
    week_ordinal: int,
) -> None:
    session_roles = week.get("session_roles")
    if not isinstance(session_roles, list) or cap <= 0:
        return

    # A build-phase week may spend one filler slot on the athlete's stated
    # conditioning goal rather than an unrelated recovery/tactical filler. It is
    # a ceiling, not a target: once the week holds an aerobic touch the
    # preference stops, non-conditioning athletes never see one, and taper and
    # fight-week filler behaviour is deliberately excluded.
    build_phase = str(week.get("phase") or "").strip().upper() in {"GPP", "SPP"}
    # Frequency is the role map's existing low-aerobic support allowance, not a
    # filler-local rule: the same cap and the same total count that govern the
    # allocator's aerobic touches also govern one placed here, so a week whose
    # allowance is already spent by an aerobic conditioning role gets no extra
    # filler touch.
    from .stage2_role_map import (
        _count_low_aerobic_support_roles,
        _low_aerobic_support_cap_for_week,
    )

    aerobic_allowance = _low_aerobic_support_cap_for_week(
        week,
        athlete_model,
        session_roles,
        hard_sparring_plan=week.get("hard_sparring_plan"),
    )

    added = 0
    kept_unused: list[Any] = []
    # ``intentionally_unused_days`` is written before placement completes, so it
    # can still name a day that a primary session now owns. The placed calendar
    # is the authority on occupancy: an occupied day is not a free day here, and
    # sharing one stays with the declared-day pass below.
    placed_day_counts = _role_day_counts(session_roles)
    injury_shared_added = False
    if athlete_model.get("has_active_injury") and not any(
            role.get("role_key") in {"mobility_rehab", "joint_prep"}
            for role in usage_ledger.get("coverage_roles", [])):
        # A full training-day budget can still host gentle injury support on
        # an existing day. Try that before a passive free-day filler spends
        # the entire week's allowance. Selection and calendar legality remain
        # with the normal filler owners.
        for day in clean_list(week.get("declared_training_days")):
            if placed_day_counts.get(_canonical_day(day), 0) != 1:
                continue
            probe = _select_filler_for_day(
                week, athlete_model, day, weekly_role_map=weekly_role_map,
                week_ordinal=week_ordinal, usage_ledger=deepcopy(usage_ledger),
                allow_physical=_week_physical_filler_count(session_roles) < 1,
            )
            if probe is None or probe[0].get("role_key") not in {"mobility_rehab", "joint_prep"}:
                continue
            insert = _place_filler(
                week, session_roles, athlete_model, day, weekly_role_map=weekly_role_map,
                week_ordinal=week_ordinal, usage_ledger=usage_ledger, allow_physical=True,
            )
            if insert is not None:
                added += 1
                injury_shared_added = True
                break
    for day_entry in week.get("intentionally_unused_days") or []:
        if added >= cap or not _unused_day_is_fillable(day_entry):
            kept_unused.append(day_entry)
            continue
        day = str(day_entry.get("day") or "").strip()
        if placed_day_counts.get(_canonical_day(day), 0) > 0:
            kept_unused.append(day_entry)
            continue
        unused_role = str(day_entry.get("role") or "").strip()
        insert = _place_filler(
            week,
            session_roles,
            athlete_model,
            day,
            weekly_role_map=weekly_role_map,
            week_ordinal=week_ordinal,
            usage_ledger=usage_ledger,
            allow_physical=_week_physical_filler_count(session_roles) < 1,
            prefer_conditioning=build_phase
            and _count_low_aerobic_support_roles(session_roles) < aerobic_allowance,
        )
        if insert is None:
            kept_unused.append(day_entry)
            continue
        insert["converted_from_unused_day"] = True
        insert["original_unused_day_role"] = unused_role
        added += 1
    week["intentionally_unused_days"] = kept_unused

    if added >= cap:
        return
    day_counts = _role_day_counts(session_roles)
    shared_added = int(injury_shared_added)
    for day in clean_list(week.get("declared_training_days")):
        if added >= cap or shared_added >= _MAX_SHARED_DAY_FILLERS:
            break
        normalized = _canonical_day(day)
        if day_counts.get(normalized, 0) != 1:
            continue
        insert = _place_filler(
            week,
            session_roles,
            athlete_model,
            day,
            weekly_role_map=weekly_role_map,
            week_ordinal=week_ordinal,
            usage_ledger=usage_ledger,
            allow_physical=_week_physical_filler_count(session_roles) < 1,
            prefer_conditioning=build_phase
            and _count_low_aerobic_support_roles(session_roles) < aerobic_allowance,
        )
        if insert is None:
            continue
        added += 1
        shared_added += 1
        day_counts[normalized] = day_counts.get(normalized, 0) + 1


def _probe_first_generic_filler(
    week: dict[str, Any],
    session_roles: list[dict[str, Any]],
    athlete_model: dict[str, Any],
    *,
    weekly_role_map: dict[str, Any],
    week_ordinal: int,
    usage_ledger: dict[str, Any],
    coverage_roles: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Dry-run the same first generic filler the real week fill would attempt."""
    probe_ledger = deepcopy(usage_ledger)
    probe_ledger["coverage_roles"] = list(coverage_roles)
    allow_physical = _week_physical_filler_count(session_roles) < 1

    for day_entry in week.get("intentionally_unused_days") or []:
        if not _unused_day_is_fillable(day_entry):
            continue
        selected = _select_filler_for_day(
            week,
            athlete_model,
            str(day_entry.get("day") or "").strip(),
            weekly_role_map=weekly_role_map,
            week_ordinal=week_ordinal,
            usage_ledger=probe_ledger,
            allow_physical=allow_physical,
        )
        if selected is not None:
            return selected[0]

    day_counts = _role_day_counts(session_roles)
    for day in clean_list(week.get("declared_training_days")):
        if day_counts.get(_canonical_day(day), 0) != 1:
            continue
        selected = _select_filler_for_day(
            week,
            athlete_model,
            day,
            weekly_role_map=weekly_role_map,
            week_ordinal=week_ordinal,
            usage_ledger=probe_ledger,
            allow_physical=allow_physical,
        )
        if selected is not None:
            return selected[0]
    return None


def _coordination_is_best_remaining_option(
    week: dict[str, Any],
    session_roles: list[dict[str, Any]],
    athlete_model: dict[str, Any],
    *,
    weekly_role_map: dict[str, Any],
    week_ordinal: int,
    usage_ledger: dict[str, Any],
    coverage_roles: list[dict[str, Any]],
) -> bool:
    """Compare banked coordination with the generic selector's real eligible winner."""
    coverage_state = build_target_coverage_state(athlete_model, coverage_roles)
    coordination_opportunity = _priority_contribution(
        "coordination_support",
        coverage_state,
    )
    if coordination_opportunity <= 0:
        return False

    generic = _probe_first_generic_filler(
        week,
        session_roles,
        athlete_model,
        weekly_role_map=weekly_role_map,
        week_ordinal=week_ordinal,
        usage_ledger=usage_ledger,
        coverage_roles=coverage_roles,
    )
    if generic is None:
        return True

    generic_opportunity = _priority_contribution(
        str(generic.get("role_key") or ""),
        coverage_state,
    )
    # Preserve the established coordination-bank materialisation on an exact
    # opportunity tie; otherwise the higher real eligible opportunity owns the slot.
    return coordination_opportunity + 1e-9 >= generic_opportunity


def _existing_training_day(week: dict[str, Any], session_roles: list[dict[str, Any]]) -> tuple[str, int] | None:
    for role in session_roles:
        if not isinstance(role, dict) or role.get("camp_week_filler"):
            continue
        day = str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").strip()
        d_day = _calendar_d_day(week, day)
        if day and d_day is not None and d_day > 0:
            return day, d_day
    return None


def _declared_training_day(week: dict[str, Any]) -> tuple[str, int] | None:
    """First declared, usable D>0 day that is not intentionally off/recovery-only."""
    intentionally_unused = {
        _canonical_day(entry.get("day"))
        for entry in week.get("intentionally_unused_days") or []
        if isinstance(entry, dict) and _canonical_day(entry.get("day"))
    }
    for day in clean_list(week.get("declared_training_days")):
        canonical = _canonical_day(day)
        d_day = _calendar_d_day(week, str(day))
        if canonical and canonical not in intentionally_unused and d_day is not None and d_day > 0:
            return str(day).strip(), d_day
    return None


def _ensure_tactical_watch(
    week: dict[str, Any],
    athlete_model: dict[str, Any],
    phase: str,
    used_watch_keys: set[str],
    usage_ledger: dict[str, Any],
) -> bool:
    session_roles = week.get("session_roles")
    if not isinstance(session_roles, list):
        return False

    existing = None
    for candidate in list(session_roles):
        if not isinstance(candidate, dict) or str(candidate.get("role_key") or "") != "tactical_watch":
            continue
        day = str(candidate.get("scheduled_day_hint") or candidate.get("real_weekday") or "").strip()
        d_day = _calendar_d_day(week, day)
        if existing is None and day and d_day is not None and d_day > 0:
            existing = candidate
            continue
        session_roles.remove(candidate)

    if existing is not None:
        day = str(existing.get("scheduled_day_hint") or existing.get("real_weekday") or "").strip()
        d_day = _calendar_d_day(week, day)
        slot = (day, d_day) if d_day is not None else None
    else:
        slot = _existing_training_day(week, session_roles) or _declared_training_day(week)
    if slot is None:
        return False
    day, d_day = slot

    try:
        watch = select_tactical_watch(
            extract_tactical_style(athlete_model), phase, used_watch_keys, allow_reuse=True
        )
    except TacticalWatchBankExhausted:
        # No eligible watch at all for this sport/style/phase: a zero-load
        # insert is skipped and recorded, never a generation failure.
        week["tactical_watch_skipped"] = {"reason_code": "tactical_watch_bank_empty", "phase": phase}
        return False
    metadata = watch_metadata(watch)
    watch_governance = dict(metadata.pop("governance"))
    role = existing if existing is not None else {
        "category": "support_insert",
        "role_key": "tactical_watch",
        "athlete_facing_label": "Tactical Focus",
        "rpe_max": 1,
        "support_insert_category": "tactical",
        "support_insert_cost_category": "zero_cost",
        "mechanical_load_regions": [],
        "countdown_offset": d_day,
        "countdown_label": f"D-{d_day}",
        "scheduled_countdown_label": f"D-{d_day}",
        "stress_class": "support",
        "cost_class": "low",
        "governance": {"authority": "camp_week_support_insert"},
    }
    role.update(metadata)
    role["display_text"] = build_watch_display_text(watch)
    role["duration_min"] = [watch.duration_minutes, watch.duration_minutes]
    role["mandatory_tactical_watch"] = True
    role["weekly_requirement"] = "fight_tactical_watch"
    role["camp_phase"] = phase
    role["governance"] = {
        **dict(role.get("governance") or {}),
        **watch_governance,
        "mandatory": True,
        "meaningful_stress": False,
    }
    _decorate_filler(role, day, d_day)
    if existing is None:
        session_roles.append(role)
    used_watch_keys.add(watch.key)
    _record_insert_usage(usage_ledger, "tactical_watch", d_day)
    return True


def _coordination_directive_rank(
    legality: CalendarLegalityView, d_day: int
) -> int | None:
    """Rank a coordination slot by the shared policy directive.

    0 == ALLOW (or no contact context), 1 == DEPRIORITIZE, ``None`` == FORBID
    (not a legal slot). Legality is the authority; the caller only orders by it.
    """
    decision = legality.decision_for_role(_COORDINATION_LEGALITY_ROLE, d_day)
    if decision is None:
        return 0
    if decision.directive is PlacementDirective.FORBID:
        return None
    return 1 if decision.directive is PlacementDirective.DEPRIORITIZE else 0


def _coordination_slot(
    week: dict[str, Any],
    session_roles: list[dict[str, Any]],
    athlete_model: dict[str, Any],
    *,
    legality: CalendarLegalityView,
) -> tuple[str, int] | None:
    intentionally_unused = {
        _canonical_day(entry.get("day"))
        for entry in week.get("intentionally_unused_days") or []
        if isinstance(entry, dict)
        and str(entry.get("role") or "").strip() in {"off_day", "recovery_only_day"}
        and _canonical_day(entry.get("day"))
    }

    support_days = clean_list(week.get("declared_support_work_days")) or clean_list(
        athlete_model.get("support_work_days", [])
    )
    training_days = clean_list(week.get("declared_training_days"))
    existing_days = [
        str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").strip()
        for role in session_roles
        if isinstance(role, dict)
        and not role.get("camp_week_filler")
        and str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").strip()
    ]

    ordered_days = list(dict.fromkeys([*support_days, *existing_days, *training_days]))
    support_canonical = {_canonical_day(day) for day in support_days if _canonical_day(day)}

    all_role_counts: dict[str, int] = {}
    for role in session_roles:
        if not isinstance(role, dict):
            continue
        canonical = _canonical_day(role.get("scheduled_day_hint") or role.get("real_weekday"))
        if canonical:
            all_role_counts[canonical] = all_role_counts.get(canonical, 0) + 1

    candidates: list[tuple[int, int, int, int, str, int]] = []
    tail_days = set(week.get("late_fight_tail_days") or [])
    for index, day in enumerate(ordered_days):
        canonical = _canonical_day(day)
        d_day = _calendar_d_day(week, day)
        if not canonical or d_day is None or d_day <= 1:
            continue
        if d_day in tail_days:
            continue
        if canonical in intentionally_unused:
            continue
        # Resolved contact legality, not raw declared hard days. A coordination
        # insert (low-load physical) may not own or share an effective contact
        # day (FORBID -> dropped); and an ALLOW day is preferred over a merely
        # legal DEPRIORITIZE day, so the shared policy's directive is the primary
        # ranking key ahead of the support-day / role-count heuristics.
        directive_rank = _coordination_directive_rank(legality, d_day)
        if directive_rank is None:
            continue
        is_support_day = canonical in support_canonical
        if not is_support_day and all_role_counts.get(canonical, 0) == 0:
            continue
        candidates.append(
            (
                directive_rank,
                0 if is_support_day else 1,
                all_role_counts.get(canonical, 0),
                index,
                str(day).strip(),
                d_day,
            )
        )

    if not candidates:
        return None
    _, _, _, _, day, d_day = min(candidates)
    return day, d_day


def _ensure_coordination_support(
    week: dict[str, Any],
    athlete_model: dict[str, Any],
    phase: str,
    used_coordination_keys: set[str],
    *,
    weekly_role_map: dict[str, Any],
    week_ordinal: int,
    usage_ledger: dict[str, Any],
) -> bool:
    if _week_is_compressed(week):
        return False

    session_roles = week.get("session_roles")
    if not isinstance(session_roles, list):
        return False

    # The banked coordination role is a materialisation path, not a second
    # priority engine. Exclude current-week coordination candidates from the
    # programme coverage ledger so they cannot satisfy themselves, then compare
    # the banked role against the real generic filler that the week would select.
    coverage_roles = usage_ledger.setdefault("coverage_roles", [])
    current_coordination_ids = {
        id(role)
        for role in session_roles
        if isinstance(role, dict)
        and str(role.get("role_key") or "") == "coordination_support"
    }
    roles_without_current_coordination = [
        role
        for role in coverage_roles
        if id(role) not in current_coordination_ids
    ]

    legality = weekly_role_map_legality(weekly_role_map, week, week_ordinal)
    tail_days = set(week.get("late_fight_tail_days") or [])
    existing = None
    for candidate in list(session_roles):
        if not isinstance(candidate, dict) or str(candidate.get("role_key") or "") != "coordination_support":
            continue
        day = str(candidate.get("scheduled_day_hint") or candidate.get("real_weekday") or "").strip()
        d_day = _calendar_d_day(week, day)
        if (
            existing is None
            and day
            and d_day is not None
            and d_day > 1
            and d_day not in tail_days
            and not legality.role_is_forbidden(_COORDINATION_LEGALITY_ROLE, d_day)
        ):
            existing = candidate
            continue
        session_roles.remove(candidate)

    # Keep the shared coverage list aligned when duplicate/invalid current-week
    # coordination roles were removed above. Previous-week coordination support
    # remains in the ledger and can legitimately satisfy remaining need.
    active_current_ids = {
        id(role)
        for role in session_roles
        if isinstance(role, dict)
    }
    coverage_roles[:] = [
        role
        for role in coverage_roles
        if id(role) not in current_coordination_ids or id(role) in active_current_ids
    ]

    if existing is not None:
        existing_day = str(existing.get("scheduled_day_hint") or existing.get("real_weekday") or "").strip()
        existing_d = _calendar_d_day(week, existing_day)
        slot = (existing_day, existing_d) if existing_d is not None else None
        existing_rank = (
            _coordination_directive_rank(legality, existing_d)
            if existing_d is not None
            else None
        )
        # Keep an existing ALLOW placement; but if it is only DEPRIORITIZE (or has
        # no usable day) and the shared policy offers a cleaner ALLOW slot, move it
        # there rather than leaving it on the merely-not-forbidden day.
        if existing_rank != 0:
            best = _coordination_slot(week, session_roles, athlete_model, legality=legality)
            if best is not None:
                best_rank = _coordination_directive_rank(legality, best[1])
                if best_rank is not None and (
                    slot is None or existing_rank is None or best_rank < existing_rank
                ):
                    slot = best
    else:
        slot = _coordination_slot(
            week, session_roles, athlete_model, legality=legality
        )
    if slot is None:
        return False

    session_roles_without_current_coordination = [
        role
        for role in session_roles
        if not (
            isinstance(role, dict)
            and id(role) in current_coordination_ids
        )
    ]
    if not _coordination_is_best_remaining_option(
        week,
        session_roles_without_current_coordination,
        athlete_model,
        weekly_role_map=weekly_role_map,
        week_ordinal=week_ordinal,
        usage_ledger=usage_ledger,
        coverage_roles=roles_without_current_coordination,
    ):
        session_roles[:] = session_roles_without_current_coordination
        coverage_roles[:] = roles_without_current_coordination
        return False

    day, d_day = slot
    drill = select_coordination_support(athlete_model, phase, used_coordination_keys)
    if drill is None:
        return False

    metadata = coordination_support_metadata(drill)
    coordination_governance = dict(metadata.pop("governance"))
    role = existing if existing is not None else {
        "category": "support_insert",
        "role_key": "coordination_support",
        "athlete_facing_label": "Coordination",
        "rpe_max": drill.rpe,
        "mechanical_load_regions": [],
        "countdown_offset": d_day,
        "countdown_label": f"D-{d_day}",
        "scheduled_countdown_label": f"D-{d_day}",
        "governance": {"authority": "camp_week_support_insert"},
    }
    role.update(metadata)
    role["support_target_capabilities"] = filler_target_capabilities(
        "coordination_support"
    )
    # Keep the countdown fields consistent with the chosen day for both a freshly
    # built role and an existing role relocated to a cleaner ALLOW slot.
    role["countdown_offset"] = d_day
    role["countdown_label"] = f"D-{d_day}"
    role["scheduled_countdown_label"] = f"D-{d_day}"
    role["coordination_support_key"] = drill.key
    role["display_text"] = build_coordination_display_text(drill)
    role["duration_min"] = [drill.duration_min, drill.duration_min]
    role["weekly_requirement"] = "coordination_target"
    role["camp_phase"] = phase
    role["governance"] = {
        **dict(role.get("governance") or {}),
        **coordination_governance,
        "meaningful_stress": False,
    }
    _decorate_filler(role, day, d_day)
    if existing is None:
        session_roles.append(role)
        coverage_roles.append(role)
    used_coordination_keys.add(drill.key)
    return True


def _role_d_day(week: dict[str, Any], role: dict[str, Any]) -> int | None:
    for key in ("countdown_offset",):
        try:
            value = role.get(key)
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    for key in ("scheduled_countdown_label", "countdown_label"):
        label = str(role.get(key) or "").strip().upper()
        if label.startswith("D-"):
            digits = "".join(char for char in label[2:] if char.isdigit())
            if digits:
                return int(digits)
    day = str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").strip()
    return _calendar_d_day(week, day)


def _week_for_d_day(weeks: list[dict[str, Any]], d_day: int) -> dict[str, Any] | None:
    for week in weeks:
        for day in week.get("calendar_days") or []:
            if isinstance(day, dict) and day.get("d_day") == d_day:
                return week
    return None


def _splice_late_fight_tail(
    weekly_role_map: dict[str, Any],
    athlete_model: dict[str, Any],
) -> bool:
    """Hand scheduled D-13..D-0 ownership to the existing late-fight planner.

    A plan generated at D-14 or further out keeps the normal planner for D-14+
    exactly as before. Its future D-13..D-0 tail is rebuilt from the existing
    composite late-fight allocator, rather than leaving normal-camp roles in that
    window and trying to repair them only at render time.
    """
    try:
        days_until_fight = int(athlete_model.get("days_until_fight"))
    except (TypeError, ValueError):
        return False
    if days_until_fight < 14:
        return False

    weeks = [week for week in weekly_role_map.get("weeks", []) or [] if isinstance(week, dict)]
    if not weeks or _week_for_d_day(weeks, 13) is None:
        return False

    from .stage2_payload_late_fight import (
        _late_fight_practical_allocation_plan,
        _shifted_segment_athlete_model,
    )

    tail_athlete = _shifted_segment_athlete_model(days_until_fight, 13, athlete_model)
    allocation = _late_fight_practical_allocation_plan(13, tail_athlete)
    tail_roles = [
        dict(role)
        for role in allocation.get("session_roles", []) or []
        if isinstance(role, dict)
        and isinstance(role.get("countdown_offset"), int)
        and 0 < int(role.get("countdown_offset")) <= 13
    ]
    if not tail_roles:
        return False

    tail_range = set(range(0, 14))
    for week in weeks:
        kept_roles: list[Any] = []
        calendar_d_days = {
            int(day.get("d_day"))
            for day in week.get("calendar_days") or []
            if isinstance(day, dict) and isinstance(day.get("d_day"), int)
        }
        owned_tail_days = sorted(calendar_d_days & tail_range)
        if owned_tail_days:
            week["late_fight_tail_days"] = owned_tail_days
        else:
            week.pop("late_fight_tail_days", None)

        for role in week.get("session_roles") or []:
            if not isinstance(role, dict):
                kept_roles.append(role)
                continue
            d_day = _role_d_day(week, role)
            if d_day is not None and 1 <= d_day <= 13:
                continue
            kept_roles.append(role)
        week["session_roles"] = kept_roles

        week["intentionally_unused_days"] = [
            entry
            for entry in week.get("intentionally_unused_days") or []
            if not isinstance(entry, dict)
            or (
                (entry_d := _calendar_d_day(week, str(entry.get("day") or ""))) is None
                or entry_d >= 14
            )
        ]

    for role in tail_roles:
        d_day = int(role["countdown_offset"])
        week = _week_for_d_day(weeks, d_day)
        if week is None:
            continue
        role["late_fight_tail_owned"] = True
        governance = dict(role.get("governance") or {})
        governance["authority"] = "late_fight_tail_allocator"
        role["governance"] = governance
        week.setdefault("session_roles", []).append(role)

    for week in weeks:
        if not week.get("late_fight_tail_days"):
            continue
        week["session_roles"] = sorted(
            week.get("session_roles") or [],
            key=lambda role: (
                -int(_role_d_day(week, role) if isinstance(role, dict) and _role_d_day(week, role) is not None else -999),
                int(role.get("session_index") or 0) if isinstance(role, dict) else 0,
            ),
        )

    weekly_role_map["late_fight_tail_handoff"] = {
        "active": True,
        "normal_planner_through_d": 14,
        "late_fight_planner_from_d": 13,
        "source": "existing_late_fight_composite_allocator",
    }
    return True


def _week_day_slots(week: dict[str, Any]) -> list[tuple[int, str]]:
    slots: list[tuple[int, str]] = []
    for entry in week.get("calendar_days") or []:
        if not isinstance(entry, dict):
            continue
        d_day = entry.get("d_day")
        if not isinstance(d_day, int):
            continue
        slots.append((d_day, str(entry.get("weekday") or "")))
    return slots


def _unused_day_identity(week: dict[str, Any], entry: Any) -> tuple[str, Any] | None:
    """The calendar instance an ``intentionally_unused_days`` entry refers to.

    A countdown offset identifies one day. A weekday does not: this PR exists
    because a planner week can span eight days and repeat a weekday, so keying
    on "thursday" alone would let a D-14 record and a D-7 record collapse into
    one and silently drop an explanation the calendar owes. Weekday is the
    fallback identity only for legacy records that carry no offset, and it is
    kept in a separate namespace so it can never collide with an offset key.
    """
    if not isinstance(entry, dict):
        return None
    offset = entry.get("countdown_offset")
    if not isinstance(offset, int) or isinstance(offset, bool):
        label = str(entry.get("countdown_label") or "").strip().upper()
        if label.startswith("D-") and label[2:].isdigit():
            offset = int(label[2:])
    if isinstance(offset, int) and not isinstance(offset, bool):
        return ("offset", offset)
    day = _canonical_day(str(entry.get("day") or ""))
    if not day:
        return None
    # A legacy weekday-only record still resolves to a single instance when the
    # week holds exactly one of that weekday; when it repeats, the record is
    # genuinely ambiguous and only blocks the occurrence the calendar resolves it
    # to, rather than every occurrence.
    resolved = _calendar_d_day(week, day)
    if resolved is not None:
        return ("offset", resolved)
    return ("weekday", day)


def _merge_unused_day_records(
    week: dict[str, Any], records: list[dict[str, Any]]
) -> None:
    """Record empty declared days without displacing an existing explanation."""
    merged = list(week.get("intentionally_unused_days") or [])
    seen = {
        identity
        for entry in merged
        if (identity := _unused_day_identity(week, entry)) is not None
    }
    for record in records:
        identity = _unused_day_identity(week, record)
        if identity is not None and identity in seen:
            continue
        if identity is not None:
            seen.add(identity)
        merged.append(record)
    week["intentionally_unused_days"] = merged


def _complete_week_physical_occupancy(
    week: dict[str, Any],
    athlete_model: dict[str, Any],
    usage_ledger: dict[str, Any],
    *,
    weekly_role_map: dict[str, Any] | None = None,
) -> None:
    """Give every declared training day a session, or a recorded reason for not.

    ``training_frequency`` is a physical-session count, so a declared training
    day that survives the allocators with nothing on it is a contract breach —
    not a taper decision. Taper caps own *dose*; this owns *occupancy*.
    """
    session_roles = week.get("session_roles")
    if not isinstance(session_roles, list):
        return
    day_slots = _week_day_slots(week)
    if not day_slots:
        return
    added, unused = fill_physical_occupancy(
        day_slots=day_slots,
        roles=[role for role in session_roles if isinstance(role, dict)],
        athlete_model=athlete_model,
        usage_ledger=usage_ledger,
        frequency_role_map=weekly_role_map or {"weeks": [week]},
    )
    for role in added:
        role["camp_week_filler"] = True
        if week.get("late_fight_tail_days") and role.get("countdown_offset") in set(
            week.get("late_fight_tail_days") or []
        ):
            # Stay inside the finished-tail ownership contract: a day D-13 and
            # closer remains owned by the late-fight allocator, so occupancy work
            # placed there is tagged as its own rather than as a normal-planner
            # role that survived the splice.
            role["late_fight_tail_owned"] = True
            governance = dict(role.get("governance") or {})
            governance["authority"] = "late_fight_tail_allocator"
            role["governance"] = governance
        session_roles.append(role)
        usage_ledger.setdefault("coverage_roles", []).append(role)
    _merge_unused_day_records(week, unused)


def _ensure_camp_visualizations(week: dict[str, Any], athlete_model: dict[str, Any]) -> int:
    """Offer the week's optional Fight Visualisation(s).

    One a week by default, two when mental preparation is prioritised (see
    ``mental_training_policy``). Only D-42..D-14: the late-fight tail owns D-13
    onward and places its own, and those tail sessions still count toward this
    calendar week's allowance. Days are ranked by ``camp_visualization_day_tier``
    (low-load days, then other non-contact days); rest days and days already
    holding a mental task are never used.
    """
    session_roles = week.get("session_roles")
    if not isinstance(session_roles, list):
        return 0
    by_day: dict[int, tuple[str, list[dict[str, Any]]]] = {}
    existing: list[int] = []
    for role in session_roles:
        if not isinstance(role, dict):
            continue
        day = str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").strip()
        d_day = _calendar_d_day(week, day)
        if role.get(OPTIONAL_FLAG) and d_day is not None:
            existing.append(d_day)
        if not day or d_day is None or not 14 <= d_day <= CAMP_VISUALIZATION_FIRST_DAY:
            continue
        by_day.setdefault(d_day, (day, []))[1].append(role)
    candidates = {
        d_day: tier
        for d_day, (_day, roles) in by_day.items()
        if (tier := camp_visualization_day_tier(roles)) is not None
    }
    added = 0
    chosen = choose_camp_visualization_days(
        candidates,
        limit=max(0, mental_training_policy(athlete_model).visualisations_per_week - len(existing)),
        taken=tuple(existing),
    )
    for ordinal, d_day in enumerate(chosen, start=len(existing)):
        day = by_day[d_day][0]
        role = camp_visualization_role(athlete_model, d_day=d_day, weekday=day, ordinal=ordinal)
        if role is None:
            continue
        # Not a discretionary filler (no ``camp_week_filler``): its own layer,
        # rendered exactly through its bank lock and outside the filler caps.
        role["session_index"] = 0
        session_roles.append(role)
        added += 1
    return added


def _place_camp_visualizations(weekly_role_map: dict[str, Any], athlete_model: dict[str, Any]) -> None:
    """Runs last, over the final calendar, so day tiers see every placed role."""
    for week in weekly_role_map.get("weeks", []) or []:
        if (
            isinstance(week, dict)
            and str(week.get("phase") or "").strip().upper() in _FIGHT_PHASE_CAPS
            # A compressed week is already crowded: leave it alone.
            and not _week_is_compressed(week)
        ):
            _ensure_camp_visualizations(week, athlete_model)


def apply_camp_week_fillers(
    weekly_role_map: dict[str, Any],
    athlete_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(weekly_role_map, dict):
        return weekly_role_map

    athlete_model = athlete_model or {}
    fight_dated = _has_future_fight(athlete_model)
    _splice_late_fight_tail(weekly_role_map, athlete_model)
    usage_ledger = _new_usage_ledger()
    _seed_programme_coverage_roles(weekly_role_map, usage_ledger)
    used_watch_keys: set[str] = set()
    used_coordination_keys: set[str] = set()

    for week_ordinal, week in enumerate(weekly_role_map.get("weeks", []) or [], start=1):
        if not isinstance(week, dict):
            continue
        phase = str(week.get("phase") or "").strip().upper()

        if fight_dated and phase in _FIGHT_PHASE_CAPS:
            _ensure_tactical_watch(week, athlete_model, phase, used_watch_keys, usage_ledger)
            discretionary_cap = max(0, _FIGHT_PHASE_CAPS[phase] - 1)
            coordination_added = False
            if discretionary_cap > 0:
                coordination_added = _ensure_coordination_support(
                    week,
                    athlete_model,
                    phase,
                    used_coordination_keys,
                    weekly_role_map=weekly_role_map,
                    week_ordinal=week_ordinal,
                    usage_ledger=usage_ledger,
                )
            if not _week_is_compressed(week):
                _fill_week(
                    week,
                    athlete_model,
                    max(0, discretionary_cap - int(coordination_added)),
                    usage_ledger,
                    weekly_role_map=weekly_role_map,
                    week_ordinal=week_ordinal,
                )
            continue

        coordination_added = False
        if phase in {"GPP", "SPP", "TAPER"}:
            coordination_added = _ensure_coordination_support(
                week,
                athlete_model,
                phase,
                used_coordination_keys,
                weekly_role_map=weekly_role_map,
                week_ordinal=week_ordinal,
                usage_ledger=usage_ledger,
            )

        cap = _LEGACY_PHASE_CAPS.get(phase)
        if cap and not _week_is_compressed(week):
            _fill_week(
                week,
                athlete_model,
                max(0, cap - int(coordination_added)),
                usage_ledger,
                weekly_role_map=weekly_role_map,
                week_ordinal=week_ordinal,
            )

    if fight_dated:
        for week in weekly_role_map.get("weeks", []) or []:
            if isinstance(week, dict):
                _complete_week_physical_occupancy(
                    week, athlete_model, usage_ledger, weekly_role_map=weekly_role_map,
                )
        # Settle Tactical Focus / reflection first so the camp visualisations
        # land on days that keep no other mental task, then govern the finished
        # calendar: nothing placed above can outlive the shared policy.
        govern_weekly_mental_training(weekly_role_map, athlete_model)
        _place_camp_visualizations(weekly_role_map, athlete_model)
        govern_weekly_mental_training(weekly_role_map, athlete_model)
    return weekly_role_map


def govern_weekly_mental_training(
    weekly_role_map: dict[str, Any], athlete_model: dict[str, Any]
) -> None:
    """Apply the shared mental-training frequency policy across the whole map.

    Covers the normal-camp weeks and the spliced late-fight tail together, so
    spacing (one Tactical Focus every two weeks, say) holds across the D-14
    boundary as well as inside each planner.
    """
    weeks = [week for week in weekly_role_map.get("weeks", []) or [] if isinstance(week, dict)]
    if not weeks:
        return
    owner: dict[int, tuple[int, dict[str, Any]]] = {}
    roles: list[dict[str, Any]] = []
    offsets: dict[int, int] = {}
    contact_offsets: set[int] = set()
    weekday_by_offset: dict[int, tuple[int, str]] = {}
    for ordinal, week in enumerate(weeks, start=1):
        for d_day, weekday in _week_day_slots(week):
            weekday_by_offset[d_day] = (ordinal, weekday)
        contact_offsets |= weekly_role_map_legality(weekly_role_map, week, ordinal).contact_offsets()
        for role in week.get("session_roles") or []:
            if not isinstance(role, dict):
                continue
            d_day = _role_d_day(week, role)
            if d_day is None:
                continue
            owner[id(role)] = (ordinal, week)
            offsets[id(role)] = d_day
            roles.append(role)

    # Phase/progression weeks can split a Monday-based calendar week. The
    # frequency cap belongs to the athlete's calendar, not those internal rows.
    from datetime import date
    try:
        fight_weekday = date.fromisoformat(str(athlete_model.get("fight_date"))[:10]).weekday()
    except ValueError:
        fight_day = (weekday_by_offset.get(0) or (None, ""))[1].lower()
        fight_weekday = {name: i for i, name in enumerate(
            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        )}.get(fight_day)

    def bucket_of(role: dict[str, Any], offset: int) -> Any:
        return calendar_week_bucket(offset, fight_weekday)

    def relocate(role: dict[str, Any], offset: int) -> None:
        day_title = str((weekday_by_offset.get(offset) or (None, ""))[1]).strip().title()
        if day_title:
            role["scheduled_day_hint"] = day_title
            role["real_weekday"] = day_title
            role["countdown_display_label"] = f"D-{offset} ({day_title})"
        role["countdown_offset"] = offset
        role["countdown_label"] = f"D-{offset}"
        role["scheduled_countdown_label"] = f"D-{offset}"
        target = weekday_by_offset.get(offset)
        original = owner.get(id(role))
        if target and original and target[0] != original[0]:
            original[1]["session_roles"] = [item for item in original[1]["session_roles"] if item is not role]
            destination = weeks[target[0] - 1]
            destination.setdefault("session_roles", []).append(role)
            owner[id(role)] = (target[0], destination)
        offsets[id(role)] = offset

    removed = govern_mental_roles(
        roles,
        athlete_model,
        offset_of=lambda role: offsets.get(id(role)),
        bucket_of=bucket_of,
        contact_offsets=contact_offsets,
        relocate=relocate,
    )
    if not removed:
        return
    for week in weeks:
        # In place: a caller's wrapped list (a late-fight sequence) must see it.
        week.setdefault("session_roles", [])[:] = [
            role
            for role in week.get("session_roles") or []
            if not (isinstance(role, dict) and id(role) in removed)
        ]
