"""Deterministic episode/routine cadence using existing accepted occurrences."""
from datetime import date, timedelta


def schedule_rehab(injury, decision, *, training_day, completions=(), exposures=(),
                   readiness_decision=None, training_session=None):
    def result(state, reason, next_day=None):
        return {"state": state, "reason": reason, "next_due_day": next_day}
    prescription = decision.get("prescription")
    if not prescription:
        return result("held" if decision.get("outcome") == "medical_review" else "unsupported", decision["summary"])
    today = date.fromisoformat(training_day)
    injury_id, episode_id = str(injury["id"]), str(injury["episode_id"])
    performed = []
    for completion in completions:
        if str(completion.get("athlete_id")) != str(injury.get("athlete_id")) or completion.get("status") not in {"started", "done", "modified"}:
            continue
        snapshot = completion.get("prescription_snapshot") or {}
        for block in snapshot.get("session", {}).get("blocks", []):
            if str(block.get("injury_id")) != injury_id or str(block.get("injury_episode_id")) != episode_id:
                continue
            day = date.fromisoformat(str(completion["training_day"]))
            if day == today:
                return result("already_completed", "Today's rehab allocation is reserved by your started session." if completion.get("status") == "started" else "You have already logged rehab for this injury today.", (today + timedelta(days=max(1, block.get("minimum_gap_days", 1)))).isoformat())
            if completion.get("status") in {"done", "modified"} and block.get("rehab_drill_id") == prescription["drill_id"]:
                performed.append((day, max(prescription["minimum_gap_days"], block.get("minimum_gap_days", 1))))
    # Legacy recorded rehab also spaces work; ordinary training has no such event.
    for row in exposures:
        raw = row.get("event_json") or row
        if str(row.get("athlete_id")) != str(injury.get("athlete_id")) or str(raw.get("injury_id")) != injury_id or str(raw.get("injury_episode_id")) != episode_id:
            continue
        day_text = (raw.get("provenance") or {}).get("training_day") or str(raw.get("occurred_at") or "")[:10]
        try:
            day = date.fromisoformat(day_text)
        except (TypeError, ValueError):
            continue
        if day == today:
            return result("already_completed", "You have already logged rehab for this injury today.", (today + timedelta(days=prescription["minimum_gap_days"])).isoformat())
        if (raw.get("drill_id") or raw.get("rehab_drill_id")) == prescription["drill_id"]:
            performed.append((day, prescription["minimum_gap_days"]))
    next_due = max((day + timedelta(days=gap) for day, gap in performed), default=today)
    if next_due > today:
        return result("recovery_day", "Allow the configured recovery gap before repeating this routine.", next_due.isoformat())
    phase_rehab = decision.get("stage") in {"calm", "restore", "load"}
    if not phase_rehab and readiness_decision in {"stop", "not_checked_in"}:
        return result("held", "Complete today's check-in and follow its training guidance.")
    if prescription.get("is_loading"):
        if not phase_rehab and readiness_decision == "pull_back":
            return result("held", "Loading rehab is held by today's reduced-training guidance.")
        # No unknown or missing next-day response is treated as tolerance.
        recent = [r for r in exposures if str((r.get("event_json") or r).get("injury_episode_id")) == episode_id]
        if recent:
            latest = max(recent, key=lambda r: str((r.get("event_json") or r).get("occurred_at") or ""))
            response = (latest.get("event_json") or latest).get("response") or {}
            from .rehab_progression import _instant
            improved = _instant(injury.get("updated_at"))
            observed = _instant(latest.get("response_recorded_at") or latest.get("created_at") or (latest.get("event_json") or latest).get("occurred_at"))
            explicit_later_improvement = injury.get("latest_reported_status") == "improving" and improved and observed and improved > observed
            if not explicit_later_improvement and (response.get("during_response") not in {"better", "same"} or response.get("next_day_response") not in {"better", "same"}):
                return result("held", "Loading rehab needs an injury-specific response to the last rehab session.")
        for block in (training_session or {}).get("blocks", []):
            if block.get("block_type") == "rehab":
                continue
            regions = block.get("mechanical_load_regions") or []
            # Structured load is a prescription object, not a demand label.
            # Never hash that object or let it hide the day's declared demand.
            load = block.get("load")
            demand = block.get("effective_load") or (load if isinstance(load, str) else None) or (training_session or {}).get("effective_load")
            demand = demand.strip().lower() if isinstance(demand, str) else ""
            if decision.get("region") in regions and (demand in {"high", "hard", "heavy"} or block.get("contact_level") == "full"):
                return result("deferred", "Demanding training already loads this region today.", (today + timedelta(days=1)).isoformat())
    return result("due", "Your baseline rehab is due today; missed work adds no extra volume.", training_day)
