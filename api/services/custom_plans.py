"""Owner-scoped drafts and deterministic publication into the shared plan store."""

import re
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid5

from fastapi import HTTPException

from api.contracts.custom_plans import (
    CustomDose,
    CustomDoseSuggestion,
    CustomExerciseSnapshot,
    CustomPlanInput,
)
from api.services.exercise_media import load_media_index
from api.services.training_library import cache, preview


SESSION_NAMES = {
    "mixed": "Training", "strength_power": "Strength & Power", "conditioning": "Conditioning",
    "skill": "Light Technical", "sparring": "Hard Sparring", "recovery": "Recovery", "rest": "Rest",
}


def default_session_name(name: str, previous_type: str = "mixed") -> bool:
    return name.strip().casefold() in {
        "training", "workout", "session", SESSION_NAMES.get(previous_type, "Training").casefold(),
        *({"light technical / skill"} if previous_type == "skill" else set()),
    }


def schedule_issues(sessions: list[dict]) -> list[str]:
    """Publishability comes from explicit session types and current doses, never stored flags."""
    issues = []
    for session in sessions:
        kind = session.get("session_type", "mixed")
        label = f"{session['weekday']}'s {SESSION_NAMES[kind]}" if session.get("weekday") else session["name"]
        if not session.get("weekday"):
            issues.append(f"Choose a day for {session['name']}.")
        if kind == "rest":
            # Retain saved work if the athlete temporarily switches a day off.
            # It is neither prescribed nor validated as a workout on a rest day.
            continue
        if not session.get("exercises") and kind not in {"recovery", "skill", "sparring"}:
            issues.append(f"{label} session has no exercises.")
        for exercise in session.get("exercises", []):
            if dose_incomplete(exercise["dose"]):
                name = exercise.get("name") or exercise.get("snapshot", {}).get("name") or "an exercise"
                issues.append(f"Finish the dose for {name} in {label}.")
    if not any(s.get("session_type", "mixed") not in {"recovery", "rest"} for s in sessions):
        issues.append("Add at least one training day to your weekly plan.")
    return issues


def custom_open_plan_result(draft: dict, profile, *, training_day: str) -> dict:
    """Compile exactly the saved prescription, with no exercise selection."""
    from api.structured_plan_models import SCHEMA_VERSION, StructuredTrainingPlan

    sessions = draft.get("sessions") or []
    issues = schedule_issues(sessions)
    if issues:
        raise HTTPException(422, " ".join(issues))
    revision = hashlib.sha256(json.dumps(
        {k: draft[k] for k in ("name", "sessions")}, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    plan_id = str(uuid5(UUID(draft["id"]), revision))
    anchor = {"intent": "", "focus_cue": "", "reset_cue": ""}
    days = []
    hard, technical, training, recovery = [], [], [], []
    from api.contracts.custom_plans import WEEKDAYS
    for weekday in WEEKDAYS:
        daily = [s for s in sessions if s["weekday"] == weekday]
        short = weekday[:3]
        workouts = [s for s in daily if s.get("session_type", "mixed") not in {"recovery", "rest"}]
        if workouts:
            training.append(short)
        elif any(s.get("session_type") == "recovery" for s in daily):
            recovery.append(short)
        if any(s.get("session_type") == "sparring" for s in daily):
            hard.append(short)
        if any(s.get("session_type") == "skill" for s in daily):
            technical.append(short)
        rendered = []
        for position, session in enumerate(daily, 1):
            if session.get("session_type") == "rest" or (session.get("session_type") == "recovery" and not session["exercises"]):
                continue
            session_id = str(uuid5(UUID(plan_id), session["id"]))
            blocks = []
            for order, exercise in enumerate(session["exercises"], 1):
                dose = exercise["dose"]
                snapshot = exercise.get("snapshot") or {}
                content_type = snapshot.get("content_type")
                block_type = {"conditioning": "conditioning", "skill": "skill",
                              "strength": "strength", "power": "plyometric_power",
                              "speed": "speed"}.get(content_type) or {
                                  "strength_power": "strength", "conditioning": "conditioning",
                                  "skill": "skill", "sparring": "sparring", "recovery": "cooldown_recovery",
                              }.get(session.get("session_type"), "accessory")
                # A manual exercise has unknown mechanical demand. Missing
                # metadata must stay unknown for the shared injury guards.
                block = {
                    "block_id": str(uuid5(UUID(plan_id), exercise["id"])), "block_type": block_type,
                    "display_name": exercise.get("name") or snapshot["name"],
                    "order_index": order, "exercise_key": snapshot.get("exercise_key"),
                    "catalogue_id": exercise.get("catalogue_id"),
                    "private_media_id": exercise.get("media_id"), "notes": exercise.get("notes"),
                    "community_revision_id": exercise.get("community_revision_id"),
                    "source_session_id": session_id,
                    "prescription_source": "athlete", "athlete_dose": deepcopy(dose),
                    "prescription_text": _dose_text(dose),
                }
                if exercise.get("origin") == "catalogue":
                    record = cache.get().by_id.get(exercise["catalogue_id"])
                    raw = record["entry"]["raw"] if record else {}
                    for field in ("mechanical_load_regions", "tags", "contact_level", "impact_level"):
                        if raw.get(field) is not None:
                            block[field] = deepcopy(raw[field])
                    guidance = preview(record).get("guidance", {}) if record else {}
                    for target, source in (("coaching_cues", "Cues"), ("stop_rules", "Stop"),
                                           ("regression_options", "Regress")):
                        if guidance.get(source):
                            block[target] = guidance[source]
                    for target, source in (("purpose", "Why"), ("progression_rule", "Progress")):
                        if guidance.get(source):
                            block[target] = "\n".join(guidance[source])
                block.update(dose_block_fields(dose))
                for field in ("coaching_cues", "stop_rules"):
                    if exercise.get(field) is not None:
                        block[field] = list(exercise[field])
                blocks.append(block)
            rendered.append({
                "session_id": session_id, "session_type": session.get("session_type", "mixed"),
                "prescription_source": "athlete",
                "optional": True if session.get("session_type") == "recovery" else None,
                "title": session["name"], "objective": "", "mindset_anchor": anchor,
                "execution_order": position, "blocks": blocks,
            })
        days.append({
            "date": "", "weekday": short, "day_type": "moderate" if workouts else "recovery" if short in recovery else "rest", "countdown_label": "",
            "phase_label": "GPP", "sessions": rendered,
            "today_card": {"headline": " / ".join(s["name"] for s in workouts) if workouts else "Recovery day" if short in recovery else "Rest day",
                           "readiness_status": "train_as_planned", "mindset_anchor": anchor,
                           "coach_led_contact": "Hard sparring" if short in hard else None},
        })
    structured = StructuredTrainingPlan.model_validate({
        "plan_metadata": {"title": draft["name"], "sport": ", ".join(profile.technical_style),
                          "prescription_source": "athlete",
                          "plan_type": "open_ongoing_system", "timezone": profile.athlete_timezone or "UTC",
                          "status": "draft", "created_by": profile.athlete_id},
        "athlete_context": {"athlete_id": profile.athlete_id, "sport_profile": ", ".join(profile.technical_style)},
        "event_context": {"event_type": "none"},
        "weeks": [{"week_id": "custom-template", "week_index": 1, "phase_label": "GPP",
                   "week_goal": "", "start_date": "", "end_date": "",
                   "load_focus": {k: "maintain" for k in ("volume", "intensity", "specificity", "fatigue_target")},
                   "progression": {"week_type": "stabilise", "planned_change_from_previous": ""},
                   "days": days}],
        "nutrition": {k: "" for k in ("summary", "daily_focus", "training_day_guidance", "fight_week_guidance")},
    }).model_dump(mode="json")
    # The same saved prescription has one published row, including concurrent
    # retries. Changed drafts produce a new row; old plans/logs remain immutable.
    structured["plan_metadata"]["plan_id"] = plan_id
    return {
        "plan_id": plan_id, "status": "ready", "plan_name": draft["name"],
        "plan_text": "", "structured_plan": structured, "schema_version": SCHEMA_VERSION,
        "stage2_status": "not_required",
        "stage2_validator_report": {
            "custom_plan": {"id": draft["id"]},
            "structured_plan": {"status": "valid", "schema_version": SCHEMA_VERSION},
        },
        "planning_brief": {
            "custom_plan": {
                "id": draft["id"], "revision": revision,
                "exercise_media": {
                    f'exercise:{e["snapshot"]["exercise_key"]}': deepcopy(e["snapshot"]["media"])
                    for s in sessions for e in s["exercises"]
                    if e.get("snapshot", {}).get("exercise_key") and e["snapshot"].get("media")
                },
            },
            "athlete_snapshot": {"plan_creation_weekday": datetime.fromisoformat(training_day).strftime("%a"),
                                 "plan_creation_weekday_basis": "athlete_local_weekday"},
            "open_plan_spec": {
                "plan_type": "open_ongoing_system",
                "weekly_template": {
                    "training_days": training, "hard_sparring_days": hard,
                    "session_types_by_day": {d: "recovery" for d in recovery},
                    "coach_owned_days": {"hard_sparring_days": hard, "technical_skill_days": technical},
                },
                "development_block": {
                    f"week_{week}": "Maintain the saved athlete prescription"
                    for week in range(1, 5)
                },
            },
        },
    }


def dose_block_fields(dose: dict) -> dict:
    """The shared numeric block fields for one athlete dose (CustomDose shape)."""
    fields: dict = {}
    for field in ("sets", "reps", "rounds"):
        if dose.get(field) is not None:
            fields[field] = dose[field]
    for field, source in (("duration", "duration_seconds"), ("work", "work_seconds"), ("rest", "rest_seconds")):
        if dose.get(source) is not None:
            fields[field] = {"value": dose[source], "unit": "seconds"}
    if dose.get("distance") is not None:
        fields["distance"] = {"value": dose["distance"], "unit": dose["distance_unit"]}
    if dose.get("rpe") is not None:
        fields["effort"] = {"method": "RPE", "value": dose["rpe"]}
    if dose.get("load_or_intensity"):
        # Free text stays text. Do not manufacture numeric load.
        fields["intensity"] = dose["load_or_intensity"]
        weight = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(kg|lb|lbs)", dose["load_or_intensity"].strip(), re.I)
        if weight:
            fields["load"] = {"method": "absolute", "value": float(weight[1]),
                              "unit": "kg" if weight[2].lower() == "kg" else "lb",
                              "display": dose["load_or_intensity"]}
    return fields


def dose_text(dose: dict) -> str:
    return _dose_text(dose)


def _dose_text(dose: dict) -> str:
    if dose["mode"] == "reps":
        parts = [f'{dose["sets"]} × {dose["reps"]}']
    elif dose["mode"] == "duration":
        seconds = dose["duration_seconds"]
        parts = [f'{seconds // 60} min' if seconds % 60 == 0 else f'{seconds}s']
        if dose.get("sets"):
            parts[0] = f'{dose["sets"]} × {parts[0]}'
    elif dose["mode"] == "intervals":
        parts = [f'{dose["rounds"]} × {dose["work_seconds"]}s']
    elif dose["mode"] == "distance":
        parts = [f'{dose["distance"]:g} {dose["distance_unit"]}']
        if dose.get("sets"):
            parts[0] = f'{dose["sets"]} × {parts[0]}'
    else:
        parts = [dose["prescription"]]
    if dose.get("distance") and dose["mode"] != "distance":
        parts.append(f'{dose["distance"]} {dose["distance_unit"]}')
    if dose.get("load_or_intensity"):
        parts.append(dose["load_or_intensity"])
    if dose.get("rest_seconds") is not None:
        parts.append(f'rest {dose["rest_seconds"]}s')
    if dose.get("rpe"):
        parts.append(f'RPE {float(dose["rpe"]):g}')
    if dose.get("tempo"):
        parts.append(f'tempo {dose["tempo"]}')
    if dose.get("per_side"):
        parts.append("per side")
    return " · ".join(parts)


def publish_custom_plan(store, draft: dict, profile) -> dict:
    from api.compliance_guards import require_onboarding_compliance
    from api.contracts.training_day import resolve_training_day_str
    from api.models import AthleteProfileInput, PlanRequest
    from api.services.open_plan_timeline import project_open_structured_plan

    require_onboarding_compliance(profile)
    latest = store.get_latest_intake(profile.athlete_id)
    training_day = resolve_training_day_str(datetime.now(timezone.utc), athlete_timezone=profile.athlete_timezone)
    result = custom_open_plan_result(draft, profile, training_day=training_day)
    existing = store.get_plan_for_athlete(result["plan_id"], profile.athlete_id)
    if existing:
        return existing
    # Check the same projector Today/Plan Detail use before saving an executable row.
    _, context = project_open_structured_plan(
        {"created_at": training_day, "planning_brief": result["planning_brief"]},
        result["structured_plan"], current_training_day=training_day,
    )
    if context["projection_status"] != "projected":
        raise HTTPException(422, "This plan's weekly schedule could not be validated.")
    request = PlanRequest(athlete=AthleteProfileInput(
        full_name=profile.full_name, technical_style=profile.technical_style,
        athlete_timezone=profile.athlete_timezone,
    ), no_scheduled_fight=True)
    try:
        return store.create_plan(athlete_id=profile.athlete_id, intake_id=latest["id"] if latest else None,
                                 request=request, result=result, plan_id=result["plan_id"])
    except HTTPException:
        # A concurrent publication may have won the primary-key insert.
        existing = store.get_plan_for_athlete(result["plan_id"], profile.athlete_id)
        if existing:
            return existing
        raise


def plan_fields(store, body: CustomPlanInput, previous: dict | None = None, *, athlete_id: str | None = None) -> dict:
    fields = body.model_dump(mode="json")
    old = {
        exercise["catalogue_id"]: exercise["snapshot"]
        for session in (previous or {}).get("sessions", [])
        for exercise in session["exercises"]
        if "catalogue_id" in exercise
    }
    exercises = [
        exercise for session in fields["sessions"] for exercise in session["exercises"]
    ]
    try:
        index = (
            cache.get() if any(e["origin"] == "catalogue" for e in exercises) else None
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(
            503, "Training library is temporarily unavailable."
        ) from None
    media = (
        load_media_index(store)
        if any(e["origin"] == "catalogue" for e in exercises)
        else {}
    )
    previous_ids = {
        e["id"]: e for s in (previous or {}).get("sessions", []) for e in s["exercises"]
    }
    supplied = {str(e.id): e.model_fields_set for s in body.sessions for e in s.exercises}
    for exercise in exercises:
        prior = previous_ids.get(exercise["id"])
        if prior and (
            prior.get("origin", "catalogue") != exercise["origin"]
            or prior.get("catalogue_id") != exercise.get("catalogue_id")
        ):
            raise HTTPException(
                422,
                "An exercise occurrence cannot change origin or catalogue identity.",
            )
        exercise["incomplete"] = dose_incomplete(exercise["dose"])
        if exercise["origin"] == "manual":
            # Older clients know nothing about media_id; omission preserves the
            # saved attachment. Explicit null is the owner's remove action.
            if prior and "media_id" not in supplied[exercise["id"]]:
                exercise["media_id"] = prior.get("media_id")
            for field in ("community_id", "community_revision_id"):
                if prior and field not in supplied[exercise["id"]]:
                    exercise[field] = prior.get(field)
            if exercise.get("community_id"):
                store.community_rpc("community_owned_revision", {"p_actor_id": athlete_id, "p_exercise_id": exercise["community_id"]})
            if exercise.get("community_revision_id"):
                from api.services.community_exercises import detail_row
                revision = detail_row(store, athlete_id, exercise["community_revision_id"])
                existing = prior and prior.get("community_revision_id") == exercise["community_revision_id"]
                if not existing and not revision.get("published"):
                    raise HTTPException(409, 'This community exercise is no longer available.')
                if exercise.get("media_id"):
                    raise HTTPException(422, 'Community media uses its approved revision.')
            if exercise.get("media_id"):
                from api.services.private_media import owned_media

                if not athlete_id:
                    raise HTTPException(403, "Video ownership could not be verified.")
                owned_media(store, athlete_id, exercise["media_id"])
            continue
        identity = exercise["catalogue_id"]
        record = index.by_id.get(identity)
        if record is not None:
            if record["summary"]["category"] != "training":
                raise HTTPException(
                    422, "Only Training exercises can be added to custom plans."
                )
            snapshot = preview(record)
            snapshot["media"] = media.get(record["entry"]["exercise_key"])
            exercise["snapshot"] = CustomExerciseSnapshot.model_validate(
                snapshot
            ).model_dump(mode="json")
        elif identity in old:
            # A retired catalogue identity stays reviewable in its saved draft.
            exercise["snapshot"] = old[identity]
        else:
            raise HTTPException(
                422,
                "Exercise is no longer available. Choose another Training exercise.",
            )
    old_sessions = {s["id"]: s for s in (previous or {}).get("sessions", [])}
    for session in fields["sessions"]:
        prior = old_sessions.get(session["id"], {})
        previous_type = prior.get("session_type", "mixed")
        if default_session_name(session["name"], previous_type):
            session["name"] = SESSION_NAMES[session["session_type"]]
    fields["incomplete"] = bool(schedule_issues(fields["sessions"]))
    return fields


def dose_incomplete(dose):
    mode = dose["mode"]
    if mode == "distance":
        return not (dose.get("distance") and dose.get("distance_unit"))
    if mode == "reps":
        return not (dose.get("sets") and dose.get("reps"))
    if mode == "duration":
        return not dose.get("duration_seconds") or bool(dose.get("distance")) != bool(
            dose.get("distance_unit")
        )
    if mode == "intervals":
        return (
            not (dose.get("rounds") and dose.get("work_seconds"))
            or dose.get("rest_seconds") is None
        )
    return not (dose.get("prescription") or "").strip()


def source_suggestion(catalogue_id: str):
    """Read current bank evidence only; ambiguous formats remain editable text."""
    try:
        record = cache.get().by_id.get(catalogue_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(
            503, "Training library is temporarily unavailable."
        ) from None
    if not record or record["summary"]["category"] != "training":
        raise HTTPException(404, "Training exercise not found.")
    snapshot = preview(record)
    raw = record["entry"]["raw"]
    original = str(raw.get("duration") or raw.get("prescription") or "")
    evidence = {
        key: raw[key]
        for key in (
            "rounds",
            "work_sec",
            "rest_sec",
            "total_minutes",
            "rpe",
            "sets",
            "reps",
        )
        if raw.get(key) is not None
    }
    if evidence:
        # Keep all source evidence visible, including ambiguous prescriptions,
        # but never expose bank field names in athlete-facing or saved text.
        labels = {
            "rounds": ("Rounds", ""),
            "work_sec": ("Work", " sec"),
            "rest_sec": ("Rest", " sec"),
            "total_minutes": ("Total time", " min"),
            "rpe": ("RPE", ""),
            "sets": ("Sets", ""),
            "reps": ("Reps", ""),
        }
        details = []
        for key, value in evidence.items():
            label, unit = labels[key]
            display = f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)
            details.append(f"{label}: {display}{unit}")
        original = " · ".join(
            filter(None, [original, " · ".join(details)])
        )
    dose = None
    try:

        def number(key):
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return value

        rounds, work, rest, total = (
            number(k) for k in ("rounds", "work_sec", "rest_sec", "total_minutes")
        )
        duration = str(raw.get("duration") or raw.get("prescription") or "").strip()
        intervals = re.fullmatch(
            r"(\d+)\s*[x×]\s*(\d+)\s*min\s*(?:rounds?)?,?\s*(\d+)\s*s\s*rest", duration, re.I
        )
        continuous = re.fullmatch(
            r"(\d+(?:\.\d+)?)\s*(min|minutes?|s|sec|seconds?)(?:\s+continuous)?",
            duration,
            re.I,
        )
        if rounds is not None and work is not None and rest is not None:
            agrees = not duration or (
                intervals
                and (int(intervals[1]), int(intervals[2]) * 60, int(intervals[3]))
                == (rounds, work, rest)
            )
            totals = (
                (rounds * work + (rounds - 1) * rest) / 60,
                (rounds * work + rounds * rest) / 60,
            )
            if agrees and (total is None or total in totals):
                dose = CustomDose(
                    mode="intervals",
                    rounds=rounds,
                    work_seconds=work,
                    rest_seconds=rest,
                )
        elif (
            rounds is None
            and rest is None
            and total is not None
            and (not duration or continuous)
        ):
            seconds = float(total) * 60
            parsed = (
                float(continuous[1])
                * (60 if continuous[2].lower().startswith("min") else 1)
                if continuous
                else seconds
            )
            if parsed == seconds and (work is None or work == seconds):
                dose = (
                    CustomDose(mode="duration", duration_seconds=int(seconds))
                    if seconds.is_integer()
                    else None
                )
        elif set(evidence) <= {"rpe"} and continuous:
            seconds = float(continuous[1]) * (
                60 if continuous[2].lower().startswith("min") else 1
            )
            if seconds.is_integer():
                dose = CustomDose(mode="duration", duration_seconds=int(seconds))
        elif (
            not duration
            and set(evidence) <= {"sets", "reps", "rest_sec", "rpe"}
            and number("sets") is not None
            and number("reps") is not None
        ):
            dose = CustomDose(
                mode="reps", sets=number("sets"), reps=number("reps"), rest_seconds=rest
            )
        allowed = {
            "intervals": {"rounds", "work_sec", "rest_sec", "total_minutes", "rpe"},
            "duration": {"work_sec", "total_minutes", "rpe"},
            "reps": {"sets", "reps", "rest_sec", "rpe"},
        }
        if dose and (
            not set(evidence) <= allowed[dose.mode]
            or any(number(key) is None for key in evidence)
        ):
            dose = None
        if dose and number("rpe") is not None:
            dose = CustomDose.model_validate(
                {**dose.model_dump(), "rpe": number("rpe")}
            )
    except (ValueError, TypeError):
        dose = None
    if dose is not None:
        original = _dose_text(dose.model_dump())
        if number("total_minutes") is not None:
            original += f' · Total time: {number("total_minutes"):g} min'
    if dose is None and original:
        dose = CustomDose(mode="text", prescription=original[:500])
    return CustomDoseSuggestion(
        dose=dose,
        original=original,
        source=snapshot["source"],
        source_revision=snapshot["source_revision"],
        row_revision=snapshot["row_revision"],
    )
