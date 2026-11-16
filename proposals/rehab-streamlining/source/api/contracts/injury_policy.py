"""One episode's current prescription decision; no storage or camp inference."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from fightcamp.injury_formatting import parse_injury_entry
from fightcamp.rehab_clinical import ClinicalPolicy, content_hash, validate_clinical_bank
from fightcamp.rehab_schema import normalize_severity_bucket
from fightcamp.rehab_schema import canonical_rehab_locations
from fightcamp.rehab_selector import select_rehab_candidate, filter_rehab_candidates

from .rehab_stage import resolve_rehab_stage
from .rehab_progression import resolve_reviewed_progression, episode_setback_at, _instant


# Fitness/support blocks do not imply opponent contact. Skill and sparring
# can involve partners, so they need an explicit contact declaration.
_NON_CONTACT_BLOCK_TYPES = frozenset({
    "preparation", "mobility_activation", "plyometric_power", "speed",
    "strength", "strength_speed", "accessory", "conditioning",
    "cooldown_recovery", "nutrition", "mindset", "rehab",
})


def resolve_injury_policy(
    injury: Mapping[str, Any], *, policies: Sequence[ClinicalPolicy], bank: list[dict],
    phase: str = "", equipment: Sequence[str] | None = None,
    exposures: Sequence[Mapping[str, Any]] = (), current_checkin: Mapping[str, Any] | None = None,
    history_truncated: bool = False,
    readiness_decision: str | None = None,
    excluded_drill_ids: Sequence[str] = (),
) -> dict[str, Any]:
    parsed = parse_injury_entry(" ".join(str(injury.get(k) or "") for k in ("body_area", "description"))) or {}
    region = injury.get("canonical_location") or parsed.get("canonical_location") or injury.get("body_region")
    kind = str(injury.get("injury_type") or injury.get("rehab_type") or parsed.get("injury_type") or "")
    stage_decision = resolve_rehab_stage(injury, current_checkin=current_checkin)
    stage = injury.get("rehab_stage") or stage_decision.stage
    result: dict[str, Any] = {
        "injury_id": str(injury.get("id") or injury.get("injury_id") or ""),
        "injury_episode_id": str(injury.get("episode_id") or ""),
        "region": region, "injury_type": kind, "stage": stage,
        "outcome": "unsupported_prescription", "summary": "No suitable rehab is available for this injury yet. Keep to your current restrictions.",
        "reason_codes": ["unsupported_injury_policy"], "activation": "shadow",
        "prescription": None, "restrictions": {},
        "loading_hold": readiness_decision == "pull_back" and stage not in {"calm", "restore", "load"},
    }
    if stage_decision.care_pathway == "wound_care":
        result.update(outcome="wound_care", summary="Follow the skin-care guidance for this injury.", reason_codes=["surface_wound_care"])
        return result
    if stage_decision.medical_gate or injury.get("rehab_medical_gate") or str(injury.get("severity")) in {"severe", "high"}:
        result.update(outcome="medical_review", summary="Get this injury checked before training.", reason_codes=list(stage_decision.reasons))
        return result
    if injury.get("status") == "resolved":
        result.update(outcome="no_rehab_indicated", summary="This injury is marked resolved.", reason_codes=["resolved_episode"])
        return result
    if not region or not kind or kind == "unspecified":
        result.update(outcome="missing_information", summary="Add the injury area and type so your rehab can be matched.", reason_codes=["missing_injury_identity"])
        return result
    policy = next((p for p in policies if p.region == region and p.injury_type == kind), None)
    if policy is None:
        return result
    result.update(policy_id=policy.policy_id, policy_version=policy.version, activation=policy.activation)
    if policy.status == "retired":
        result.update(activation="retired", outcome="medical_review", prescription=None,
                      summary="This rehab routine is unavailable. Follow the current injury guidance.", reason_codes=["rehab_policy_retired"])
        return result
    if policy.status != "active":
        result["reason_codes"] = ["rehab_policy_draft"]
        return result
    result["restrictions"] = {
        "blocked_regions": policy.blocked_regions, "blocked_tags": policy.blocked_tags,
        "contact_limit": policy.contact_limit,
    }
    if validate_clinical_bank((policy,), bank):
        result["reason_codes"] = ["rehab_policy_stale_or_incomplete"]
        return result
    progression = resolve_reviewed_progression({**injury, "body_region": region}, base_stage=str(stage), policy=policy,
                                              exposures=exposures, history_truncated=history_truncated)
    result["progression"] = progression
    if policy.activation == "live":
        stage = progression["stage"]
        result["stage"] = stage
    result["loading_hold"] = readiness_decision == "pull_back" and stage not in {"calm", "restore", "load"}
    if stage not in policy.live_stages:
        result.update(outcome="missing_information", summary="More injury-specific evidence is needed before changing your rehab.", reason_codes=["stage_not_activated"])
        return result
    drill_by_id = {d.get("id"): d for group in bank for d in group.get("drills", [])}
    candidates, prescriptions = [], {}
    severity = normalize_severity_bucket(injury.get("severity") or "moderate")
    for prescription in policy.prescriptions:
        if severity not in prescription.allowed_severities or prescription.drill_id in excluded_drill_ids:
            continue
        drill = {**drill_by_id[prescription.drill_id], "injury_type": kind, "allowed_severities": prescription.allowed_severities}
        candidates.append(drill)
        prescriptions[prescription.drill_id] = prescription
    # An explicit later injury-specific improvement resolves a prior setback
    # for baseline selection only. Preserve the original observations in storage.
    selection_exposures = exposures
    setback, improvement = episode_setback_at(injury, exposures), _instant(injury.get("updated_at"))
    if injury.get("latest_reported_status") == "improving" and setback and improvement and improvement > setback:
        selection_exposures = [e for e in exposures if (_instant(e.get("response_recorded_at") or e.get("created_at")
                              or (e.get("event_json") or e).get("occurred_at")) or improvement) >= improvement]
    eligible, _ = filter_rehab_candidates(injury={**injury, "body_region": region, "injury_type": kind, "severity": severity},
        rehab_stage=str(stage), candidates=candidates, available_equipment=equipment,
        exposures=selection_exposures, activated_stages=policy.live_stages)
    if eligible:
        priority = max(prescriptions[d["id"]].priority for d in eligible)
        candidates = [d for d in eligible if prescriptions[d["id"]].priority == priority]
    selected = select_rehab_candidate(
        injury={**injury, "body_region": region, "injury_type": kind, "severity": severity}, rehab_stage=str(stage),
        candidates=candidates, available_equipment=equipment, exposures=selection_exposures,
        activated_stages=policy.live_stages,
    )
    if not selected.selected_drill_id:
        result["reason_codes"] = list(dict.fromkeys(code for rejection in selected.rejected_candidates for code in rejection.reason_codes)) or ["no_supported_candidate"]
        return result
    prescription = prescriptions[selected.selected_drill_id]
    drill = deepcopy(drill_by_id[selected.selected_drill_id])
    selected_dose = prescription.camp_doses.get(phase.upper(), prescription.dose)
    dose = selected_dose.model_dump(exclude_none=True) if selected_dose else {}
    readiness_dose = prescription.readiness_doses.get(readiness_decision)
    if readiness_decision == "pull_back" and stage not in {"calm", "restore", "load"} and readiness_dose is None and prescription.dose is not None:
        result.update(outcome="missing_information", summary="Follow today's reduced-training guidance. No reduced rehab dose is configured for this readiness state.",
                      reason_codes=["reviewed_readiness_dose_missing"])
        return result
    if readiness_dose is not None:
        for name, value in readiness_dose.model_dump(exclude_none=True).items():
            if name in dose:
                dose[name] = min(dose[name], value)
    result.update(
        outcome="prescribed_rehab" if policy.activation == "live" else "unsupported_prescription",
        summary="Your rehab is matched to this injury's current recovery stage." if policy.activation == "live" else "This rehab routine is not active yet. Keep to your current restrictions.",
        reason_codes=[selected.selection_reason],
        prescription={
            "drill_id": prescription.drill_id, "drill": drill,
            "instructions": prescription.instructions,
            "dose": dose,
            "stop_when": prescription.stop_when, "frequency": prescription.frequency,
            "sources": prescription.sources,
            "bank_hash": prescription.bank_hash, "policy_id": policy.policy_id, "policy_version": policy.version,
            "policy_review_hash": policy.content_hash,
            "minimum_gap_days": prescription.minimum_gap_days,
            "is_loading": prescription.stage != "calm" and drill.get("function") in {"tendon_loading", "isometric_analgesia", "activation", "control"},
        },
        restrictions={
            "blocked_regions": policy.blocked_regions, "blocked_tags": policy.blocked_tags,
            "contact_limit": policy.contact_limit,
        },
    )
    return result


def reconcile_session_prescription(
    session: Mapping[str, Any] | None, *, decisions: Sequence[Mapping[str, Any]],
    plan_id: str, training_day: str, frozen: Mapping[str, Any] | None = None,
    training_held: bool = False,
) -> dict[str, Any] | None:
    """Project reviewed live work onto a copy. Never modify the stored camp.

    No unknown block is silently classified as safe, and no replacement expands
    the reviewed workload. Frozen content remains fixed; a new gate can stop it.
    """
    live = [d for d in decisions if d.get("activation") in {"live", "retired"}]
    if frozen:
        snapshot = deepcopy(dict(frozen))
        snapshot["frozen"] = True
        snapshot["safety_hold"] = bool(snapshot.get("safety_hold")) or any(d.get("outcome") == "medical_review" for d in decisions)
        if any(d.get("activation") == "live" and d.get("injury_id") not in snapshot.get("injury_ids", []) for d in decisions):
            snapshot["safety_hold"] = True
        accepted = {b.get("injury_id"): b for b in snapshot.get("session", {}).get("blocks", []) if b.get("injury_id")}
        known_injuries = {row.get("id") for row in snapshot.get("injury_context", [])} or set(snapshot.get("injury_ids", []))
        if any(d.get("injury_id") not in known_injuries and d.get("outcome") != "wound_care" for d in decisions):
            snapshot["safety_hold"] = True
        for decision in decisions:
            block = accepted.get(decision.get("injury_id"))
            reserved_baseline = (decision.get("schedule", {}).get("state") == "already_completed"
                and (block or {}).get("drill_snapshot", {}).get("rehab_stage") in {"calm", "restore"}
                and (not (block or {}).get("is_loading") or decision.get("stage") == "restore"))
            if block and (decision.get("outcome") != "prescribed_rehab"
                          or block.get("injury_episode_id") != decision.get("injury_episode_id")
                          or (not reserved_baseline and block.get("rehab_drill_id") != (decision.get("prescription") or {}).get("drill_id"))
                          or (block.get("is_loading") and decision.get("loading_hold"))
                          or block.get("policy_review_hash") != (decision.get("prescription") or {}).get("policy_review_hash")):
                snapshot["safety_hold"] = True
        return snapshot
    if not live:
        return None
    prescribed = [d for d in live if d.get("outcome") == "prescribed_rehab" and d.get("prescription")
                  and d.get("schedule", {}).get("state", "due") == "due"]
    if session is None and not prescribed:
        return None
    entry = deepcopy(dict(session or {"session_id": f"rehab-{training_day}", "title": "Today's rehab", "session_type": "rehab", "blocks": []}))
    blocked_regions = set().union(*(set(d.get("restrictions", {}).get("blocked_regions", [])) for d in live))
    blocked_tags = set().union(*(set(d.get("restrictions", {}).get("blocked_tags", [])) for d in live))
    contact_rank = {"none": 0, "controlled": 1, "full": 2}
    allowed_contact = min((contact_rank[d.get("restrictions", {}).get("contact_limit", "none")] for d in live), default=0)
    hold = training_held or any(d.get("outcome") == "medical_review" for d in decisions)
    if session is not None and not entry.get("blocks") and entry.get("session_type") != "rehab":
        hold = True
    blocks, changes = [], []
    if allowed_contact == 0 and entry.get("coach_led_contact"):
        entry.pop("coach_led_contact")
        changes.append({"action": "contact_held", "reason": "contact_restriction", "scope": "coach_led_contact"})

    def string_set(value):
        return set(value) if isinstance(value, list) and all(isinstance(item, str) for item in value) else None

    def contact_level(block):
        # Missing contact metadata on fitness/support work must not turn a
        # no-contact restriction into a ban on all training.
        # Explicit unknown/malformed/contact declarations still fail closed.
        if block.get("contact_level") is None and block.get("block_type") in _NON_CONTACT_BLOCK_TYPES:
            return "none"
        value = block.get("contact_level")
        return value if isinstance(value, str) else None

    for block in entry.get("blocks", []):
        if block.get("block_type") == "rehab":
            # Keep non-pilot rehab unchanged; replace only the exact live-policy
            # region. An unattributed legacy rehab block cannot claim pilot safety.
            targets = set(block.get("target_regions") or [])
            if not targets or targets & {d.get("region") for d in live}:
                continue
        demands = string_set(block.get("mechanical_load_regions"))
        tags = string_set(block.get("tags"))
        if block.get("block_type") in {"nutrition", "mindset"}:
            # These blocks prescribe no physical movement. Do not require
            # exercise anatomy just to show nutrition or mental cues.
            if block.get("mechanical_load_regions") is None:
                demands = set()
            if block.get("tags") is None:
                tags = set()
        contact = contact_level(block)
        uncertain = bool(blocked_regions) and demands is None
        uncertain = uncertain or bool((demands or set()) - canonical_rehab_locations())
        uncertain = uncertain or (bool(blocked_tags) and tags is None)
        uncertain = uncertain or (allowed_contact < contact_rank["full"] and contact not in contact_rank)
        incompatible = uncertain or bool((demands or set()) & blocked_regions) or bool((tags or set()) & blocked_tags)
        incompatible = incompatible or (contact in contact_rank and contact_rank[contact] > allowed_contact)
        if incompatible:
            alternates = block.get("alternates") or []
            safe = next((a for a in alternates if isinstance(a, dict) and string_set(a.get("mechanical_load_regions")) is not None
                         and not (set(a["mechanical_load_regions"]) - canonical_rehab_locations())
                         and not (set(a["mechanical_load_regions"]) & blocked_regions)
                         and (not blocked_tags or string_set(a.get("tags")) is not None)
                         and not ((string_set(a.get("tags")) or set()) & blocked_tags)
                         and bool(block.get("role")) and isinstance(block.get("dose"), Mapping)
                         and a.get("role") == block.get("role")
                         and a.get("dose") == block.get("dose")
                         and contact_level(a) in contact_rank and contact_rank[contact_level(a)] <= allowed_contact), None)
            if safe:
                blocks.append({**deepcopy(safe), "block_id": block.get("block_id")})
                changes.append({"block_id": block.get("block_id"), "action": "substituted"})
            else:
                hold = True
                blocks.append({**deepcopy(block), "_policy_held": True})
                reasons = []
                if blocked_regions and demands is None:
                    reasons.append("missing_load_details")
                if (demands or set()) - canonical_rehab_locations():
                    reasons.append("unknown_load_regions")
                if (demands or set()) & blocked_regions:
                    reasons.append("injury_loading_restriction")
                if blocked_tags and tags is None:
                    reasons.append("missing_movement_details")
                if (tags or set()) & blocked_tags:
                    reasons.append("injury_movement_restriction")
                if allowed_contact < contact_rank["full"] and contact not in contact_rank:
                    reasons.append("missing_contact_details")
                if contact in contact_rank and contact_rank[contact] > allowed_contact:
                    reasons.append("contact_restriction")
                changes.append({"block_id": block.get("block_id"), "action": "held", "reason": "no_reviewed_safe_substitution",
                                "restriction_reasons": reasons})
        else:
            blocks.append(deepcopy(block))
    # Match the existing camp allocation ceiling. More affected episodes are
    # explicit deferred decisions rather than extra, unbudgeted work.
    budget = 1 if "sparring" in str(entry.get("session_type") or "").lower() else 2
    budget = max(0, budget - sum(block.get("block_type") == "rehab" and not block.get("_policy_held") for block in blocks))
    for index, decision in enumerate(sorted(prescribed, key=lambda d: d["injury_id"])):
        if index >= budget:
            changes.append({"injury_id": decision["injury_id"], "action": "deferred", "reason": "rehab_slot_budget"})
            continue
        prescription = decision["prescription"]
        identity = f"rehab:{decision['injury_id']}:{decision['injury_episode_id']}"
        blocks.append({
            "block_id": identity, "block_type": "rehab", "title": prescription["drill"]["name"],
            "display_name": prescription["drill"]["name"], "coaching_cues": [prescription["instructions"]],
            **({"duration": {"value": prescription["dose"]["duration_seconds"], "unit": "seconds"}} if "duration_seconds" in prescription["dose"] else {}),
            "rehab_drill_id": prescription["drill_id"], "injury_id": decision["injury_id"],
            "injury_episode_id": decision["injury_episode_id"], "target_regions": [decision["region"]],
            "dose": prescription["dose"], "instructions": prescription["instructions"],
            **prescription["dose"],
            "stop_rules": prescription["stop_when"], "drill_snapshot": prescription["drill"],
            "policy_id": prescription["policy_id"], "policy_version": prescription["policy_version"],
            "bank_hash": prescription["bank_hash"],
            "policy_review_hash": prescription["policy_review_hash"],
            "minimum_gap_days": prescription["minimum_gap_days"],
            "is_loading": prescription["is_loading"],
            "source_references": prescription["sources"],
        })
    entry["blocks"] = blocks
    rehab_only = False
    if hold and not any(d.get("outcome") == "medical_review" for d in decisions):
        reviewed_blocks = [block for block in blocks if block.get("block_type") == "rehab" and block.get("policy_id")]
        if reviewed_blocks:
            # Hold the original training while offering separately reviewed
            # rehab. It has its own completion identity, so a rehab completion
            # cannot assert that the scheduled strength/combat work was done.
            # This is a different prescription, not the held training with a
            # new title. Its duration, combat context and coaching must agree.
            entry = {key: value for key, value in entry.items() if key in {
                "calendar_date", "weekday", "weekday_with_label", "day_label", "phase",
            }}
            entry.update(session_id=f"rehab-{training_day}", session_type="rehab", title="Today's rehab",
                         objective="Follow the reviewed rehab for your current injury guidance.",
                         effective_load="reduced", blocks=reviewed_blocks)
            hold, rehab_only = False, True
    snapshot = {"plan_id": plan_id, "training_day": training_day, "session": entry, "changes": changes,
                "safety_hold": hold, "frozen": False, "engine_version": "2",
                "rehab_only": rehab_only,
                "held_session_id": session.get("session_id") if rehab_only and session else None,
                "injury_ids": sorted(d["injury_id"] for d in live),
                "allocation_limit": 1 if "sparring" in str((session or {}).get("session_type") or "").lower() else 2}
    if hold or rehab_only:
        reasons = {reason for change in changes for reason in change.get("restriction_reasons", [])}
        detail = ("The injury-loading details for your planned training are unavailable."
                  if reasons & {"missing_load_details", "unknown_load_regions"}
                  else "Your planned training conflicts with the current injury restrictions.")
        snapshot["training_hold_reason"] = detail
        if hold:
            snapshot["safety_hold_reason"] = detail
    snapshot["revision"] = content_hash(snapshot)
    return snapshot
