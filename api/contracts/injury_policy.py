"""One episode's current prescription decision; no storage or camp inference."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any, Mapping, Sequence

from fightcamp.injury_awareness import is_functional_cant_train
from fightcamp.injury_formatting import parse_injury_entry
from fightcamp.exercise_identity import normalize_exercise_key
from fightcamp.injury_negation import remove_negated_phrases
from fightcamp.injury_taxonomy import INJURY_TAXONOMY
from fightcamp.injury_location_registry import get_rehab_location_candidates
from fightcamp.rehab_clinical import ClinicalPolicy, content_hash, validate_clinical_bank
from fightcamp.rehab_schema import normalize_severity_bucket
from fightcamp.rehab_schema import canonical_rehab_locations
from fightcamp.rehab_selector import select_rehab_candidate, filter_rehab_candidates
from fightcamp.surface_wound_safety import sanitize_surface_guidance

from .rehab_stage import resolve_rehab_stage
from .rehab_progression import resolve_reviewed_progression, episode_setback_at, _instant
from .clinician_clearance import effective_clinician_clearance, clinician_clears_baseline, permits_rehabilitation_stage, STAGE_REHABILITATION_PERMISSION
from .rehab_assessment import AssessmentContext, input_definitions, read_assessment_input
from .achilles_restore_load import review_achilles_restore_load
from .clinical_review_validity import ClinicalReviewInput, evaluate_clinical_review


_NON_CONTACT_BLOCK_TYPES = frozenset({
    "preparation", "mobility_activation", "plyometric_power", "speed",
    "strength", "strength_speed", "accessory", "conditioning",
    "cooldown_recovery", "nutrition", "mindset", "rehab",
})


def reported_load_option(profile_id, drill_id=None):
    """Compatibility lookup of published content; no per-exercise code registry."""
    from fightcamp.rehab_clinical import load_clinical_policies
    return next((p for policy in load_clinical_policies() if policy.policy_id == profile_id
        and "load" in policy.live_stages for p in policy.prescriptions
        if p.stage == "load" and p.range_choice and (drill_id is None or p.drill_id == drill_id)), None)


def resolve_injury_policy(
    injury: Mapping[str, Any], *, policies: Sequence[ClinicalPolicy], bank: list[dict],
    phase: str = "", equipment: Sequence[str] | None = None,
    exposures: Sequence[Mapping[str, Any]] = (), current_checkin: Mapping[str, Any] | None = None,
    history_truncated: bool = False,
    readiness_decision: str | None = None,
    excluded_drill_ids: Sequence[str] = (),
    as_of: datetime | None = None,
    clinical_review_inputs: Mapping[str, ClinicalReviewInput] | None = None,
) -> dict[str, Any]:
    parsed = parse_injury_entry(" ".join(str(injury.get(k) or "") for k in ("body_area", "description"))) or {}
    region = injury.get("canonical_location") or parsed.get("canonical_location") or injury.get("body_region")
    kind = str(injury.get("injury_type") or injury.get("rehab_type") or parsed.get("injury_type") or "")
    stage_decision = resolve_rehab_stage(injury, current_checkin=current_checkin)
    stage = injury.get("rehab_stage") or stage_decision.stage
    # An explicit Same in owned episode history is a follow-up, unlike a fresh
    # flag's default ongoing value. An earlier baseline stamp may predate that
    # history projection; re-evaluate it before considering higher stages.
    if (stage == "calm" and injury.get("latest_reported_status") == "ongoing"
            and injury.get("latest_reported_at") and not stage_decision.medical_gate):
        stage = stage_decision.stage
    result: dict[str, Any] = {
        "injury_id": str(injury.get("id") or injury.get("injury_id") or ""),
        "injury_episode_id": str(injury.get("episode_id") or ""),
        "region": region, "injury_type": kind, "stage": stage,
        "outcome": "unsupported_prescription", "summary": "No rehab exercises available for this stage yet.",
        "reason_codes": ["unsupported_injury_policy"], "activation": "shadow",
        "prescription": None, "restrictions": {},
        # Pull back changes training, never rehab: rehab is identical on
        # go, modify and pull back days.
        "readiness_hold": readiness_decision == "pull_back",
    }
    as_of = as_of or datetime.now(timezone.utc)
    if region == "achilles" and kind == "tendonitis":
        context = AssessmentContext.from_injury(injury, as_of=as_of,
            setback_at=episode_setback_at(injury, exposures), history_truncated=history_truncated)
        result["achilles_load_review"] = review_achilles_restore_load(context)
    if injury.get("progression_assessments"):
        context = AssessmentContext.from_injury(injury, as_of=as_of,
            setback_at=episode_setback_at(injury, exposures), history_truncated=history_truncated)
        result["assessment_inputs"] = {key: read_assessment_input(key, context)
            for key, (_, protocol) in sorted(input_definitions().items()) if protocol.applies(injury)}
    if stage_decision.care_pathway == "wound_care":
        result.update(outcome="wound_care", summary="Follow the skin-care guidance for this injury.", reason_codes=["surface_wound_care"])
        return result
    # "Can't train normally" is stored on the severe band for compatibility. It
    # restricts normal training (readiness) but is not medical severity, so on
    # its own it must not replace clinically permitted rehab with a review hold.
    severity_review = (str(injury.get("severity")) in {"severe", "high"}
                       and not is_functional_cant_train(dict(injury)))
    if stage_decision.medical_gate or injury.get("rehab_medical_gate") or severity_review:
        result.update(outcome="medical_review", summary="Get this injury checked before training.", reason_codes=list(stage_decision.reasons))
        return result
    if injury.get("status") == "resolved":
        result.update(outcome="no_rehab_indicated", summary="This injury is marked resolved.", reason_codes=["resolved_episode"])
        return result
    report = injury.get("clinician_clearance") or {}
    if (str(report.get("episode_id")) == str(injury.get("episode_id")) and injury.get("episode_id")
            and (report.get("rehabilitation_permission") or {}).get("level") == "not_cleared"):
        result.update(outcome="no_rehab_indicated", reason_codes=["reported_rehabilitation_not_cleared"],
                      summary="Your reported clinician advice does not permit rehab. Update clearance only when that advice changes.")
        return result
    if not region or not kind or kind == "unspecified":
        result.update(outcome="missing_information", summary="Add the injury area and type so your rehab can be matched.", reason_codes=["missing_injury_identity"])
        return result
    if kind in {"pain", "soreness", "tightness", "stiffness", "swelling"}:
        # A conflicting episode identity must be corrected, not diagnosed or
        # downgraded into a less specific automatic prescription. Medical gates
        # above remain authoritative; ordinary specific-family routing is intact.
        specific = {key for key, rule in INJURY_TAXONOMY.items()
                    if rule["category"] not in {"symptom", "surface", "unknown"}}
        described = remove_negated_phrases(" ".join(str(injury.get(k) or "") for k in ("body_area", "description")))
        described = described.lower().replace("_", " ").replace("-", " ")
        conflict = (injury.get("rehab_type") in specific or parsed.get("injury_type") in specific
                    or any(re.search(rf"\b{re.escape(key.replace('_', ' '))}\b", described) for key in specific))
        if conflict:
            matched = next((p for p in policies if p.region == region and p.injury_type == kind
                            and p.activation == "live" and p.status == "active"), None)
            result.update(outcome="missing_information", reason_codes=["specific_injury_identity_conflict"],
                          summary="Confirm the exact injury to get symptom guidance.",
                          restrictions={"blocked_regions": [region], "blocked_tags": [], "contact_limit": "none"})
            if matched:
                result.update(activation="live", policy_id=matched.policy_id, policy_version=matched.version)
            return result
    policy = next((p for p in policies if p.region == region and p.injury_type == kind), None)
    if policy is None:
        return result
    result.update(policy_id=policy.policy_id, policy_version=policy.version, activation=policy.activation)
    if policy.status == "retired":
        result.update(activation="retired", outcome="unsupported_prescription", prescription=None,
                      summary="No rehab exercises available for this stage yet.", reason_codes=["rehab_policy_retired"])
        return result
    if policy.status != "active":
        result["reason_codes"] = ["rehab_policy_draft"]
        return result
    result["restrictions"] = {
        "blocked_regions": policy.blocked_regions, "blocked_tags": policy.blocked_tags,
        "contact_limit": policy.contact_limit,
    }
    if policy.policy_id == "shoulder_tendonitis":
        from .shoulder_restore_load import unsupported_shoulder_presentation
        if unsupported_shoulder_presentation(injury):
            result.update(stage=None, summary="No reviewed guided pathway covers this shoulder presentation. Follow clinician advice and your current restrictions.",
                          reason_codes=["shoulder_conflicting_or_unsupported_presentation"])
            return result
    if policy.policy_id == "ankle_sprain":
        from .ankle_restore_load import unsupported_ankle_presentation
        if unsupported_ankle_presentation(injury):
            result.update(stage=None, summary="No reviewed guided pathway covers this ankle presentation. Follow clinician advice and your current restrictions.",
                          reason_codes=["ankle_conflicting_or_unsupported_presentation"])
            return result
    if validate_clinical_bank((policy,), bank):
        result["reason_codes"] = ["rehab_policy_stale_or_incomplete"]
        return result
    target_content = None
    target_rejections = {}
    selection_exposures = exposures
    if any(p.stage not in {"calm", "restore"} for p in policy.prescriptions):
        target_content = {}
        bank_index = {d["id"]:d for group in bank for d in group.get("drills",[])}
        for target in policy.live_stages:
            options = [{**bank_index[p.drill_id], "injury_type":kind, "allowed_severities":p.allowed_severities}
                       for p in policy.prescriptions if p.stage == target]
            if options:
                eligible, rejected = filter_rehab_candidates(injury={**injury,"body_region":region,"injury_type":kind,
                    "body_region_aliases":get_rehab_location_candidates(region)}, rehab_stage=target,
                    candidates=options, available_equipment=equipment, exposures=exposures, activated_stages=policy.live_stages)
                target_content[target] = bool(eligible)
                target_rejections[target] = sorted({code for r in rejected for code in r.reason_codes})
    progression = resolve_reviewed_progression({**injury, "body_region": region}, base_stage=str(stage), policy=policy,
        exposures=exposures, history_truncated=history_truncated, as_of=as_of, clinical_review_inputs=clinical_review_inputs,
        target_content_available=target_content)
    blocked = progression.get("next_transition", {})
    resolution = next((r for r in blocked.get("requirements", []) if
        r.get("reason_code") == "setback_resolved_by_improvement_and_reviewed_response"), None)
    if "reviewed_target_content_unavailable" in blocked.get("reason_codes", []) and resolution:
        # The engine has passed every requirement, including the version-bound
        # flare re-entry rule. Its resolved history must not become a second,
        # permanent per-drill ban in the selector. Only the selection projection
        # drops resolved adverse rows; eligibility still reads the entire history.
        cutoff = _instant(resolution["recovery_reported_at"])
        selection_exposures = [row for row in exposures if
            not (observed := episode_setback_at({**injury, "body_region": region}, [row])) or observed >= cutoff]
        target = blocked["to_stage"]
        options = [{**bank_index[p.drill_id], "injury_type": kind, "allowed_severities": p.allowed_severities}
                   for p in policy.prescriptions if p.stage == target]
        eligible, rejected = filter_rehab_candidates(injury={**injury, "body_region": region, "injury_type": kind,
            "body_region_aliases": get_rehab_location_candidates(region)}, rehab_stage=target,
            candidates=options, available_equipment=equipment, exposures=selection_exposures, activated_stages=policy.live_stages)
        target_content[target] = bool(eligible)
        target_rejections[target] = sorted({code for r in rejected for code in r.reason_codes})
        progression = resolve_reviewed_progression({**injury, "body_region": region}, base_stage=str(stage), policy=policy,
            exposures=exposures, history_truncated=history_truncated, as_of=as_of, clinical_review_inputs=clinical_review_inputs,
            target_content_available=target_content)
    result["progression"] = progression
    next_transition = progression.get("next_transition", {})
    if "reviewed_target_content_unavailable" in next_transition.get("reason_codes", []):
        rejected = target_rejections.get(next_transition["to_stage"], [])
        next_transition["target_content_rejections"] = rejected
        if "REJECT_EQUIPMENT_UNAVAILABLE" in rejected:
            next_transition["reason_codes"].append("target_equipment_unavailable")
        if "REJECT_UNRESOLVED_NEGATIVE_EXPOSURE" in rejected:
            next_transition["reason_codes"].append("target_adverse_response_unresolved")
    if policy.activation == "live":
        stage = progression["stage"]
        result["stage"] = stage
    if stage not in policy.live_stages:
        result.update(outcome="unsupported_prescription", summary="No rehab exercises available for this stage yet.", reason_codes=["stage_not_activated"])
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
    setback, improvement = episode_setback_at(injury, exposures), _instant(injury.get("latest_reported_at"))
    if injury.get("latest_reported_status") == "improving" and setback and improvement and improvement > setback:
        selection_exposures = [e for e in selection_exposures if (_instant(e.get("response_recorded_at") or e.get("created_at")
                              or (e.get("event_json") or e).get("occurred_at")) or improvement) >= improvement]
    # Use the existing bank/location aliases just as the bank lookup does.
    # Canonical biceps must still match reviewed bank metadata spelled bicep.
    selection_injury = {**injury, "body_region": region, "injury_type": kind, "severity": severity,
                        "body_region_aliases": get_rehab_location_candidates(region)}
    eligible, rejections = filter_rehab_candidates(injury=selection_injury,
        rehab_stage=str(stage), candidates=candidates, available_equipment=equipment,
        exposures=selection_exposures, activated_stages=policy.live_stages)
    bundle_ids = policy.stage_bundles.get(str(stage))
    if bundle_ids:
        eligible_ids = {d["id"] for d in eligible}
        # A combination remains atomic. A complete standalone routine can be
        # selected instead when the entire bundle is unavailable; never present
        # a partial bundle or combine its doses with an alternative.
        if not set(bundle_ids) <= eligible_ids:
            missing = set(bundle_ids) - eligible_ids
            equipment_only = {r.drill_id for r in rejections
                              if set(r.reason_codes) == {"REJECT_EQUIPMENT_UNAVAILABLE"}}
            # Exclusions, adverse exposures and clinical ineligibility must not
            # be bypassed by substituting another routine.
            if missing - equipment_only or set(bundle_ids) & set(excluded_drill_ids):
                result["reason_codes"] = ["reviewed_bundle_member_ineligible"]
                return result
            eligible = [d for d in eligible if d["id"] not in bundle_ids and d.get("rehab_stage") == stage]
            if not eligible:
                result["reason_codes"] = ["reviewed_bundle_member_ineligible"]
                return result
            bundle_ids = None
        else:
            candidates = [d for d in eligible if d["id"] in bundle_ids]
    if not bundle_ids and eligible:
        priority = max(prescriptions[d["id"]].priority for d in eligible)
        candidates = [d for d in eligible if prescriptions[d["id"]].priority == priority]
    selected = select_rehab_candidate(
        injury=selection_injury, rehab_stage=str(stage),
        candidates=candidates, available_equipment=equipment, exposures=selection_exposures,
        activated_stages=policy.live_stages,
    )
    if not selected.selected_drill_id:
        result["reason_codes"] = list(dict.fromkeys(code for rejection in selected.rejected_candidates for code in rejection.reason_codes)) or ["no_supported_candidate"]
        return result
    selected_ids = bundle_ids or [selected.selected_drill_id]
    resolved_drills = []
    for identity in selected_ids:
        prescription = prescriptions[identity]
        drill = deepcopy(drill_by_id[identity])
        reviewed_work = None
        permission_work = (prescription.required_rehabilitation_level or
            (prescription.range_choice and not prescription.clinical_criterion
                and prescription.stage not in {"calm", "restore"}))
        if permission_work:
            if (not prescription.range_choice or not prescription.resistance or not prescription.mandatory_restrictions
                    or not permits_rehabilitation_stage(injury, prescription.stage)):
                result["reason_codes"] = ["reported_rehabilitation_permission_unavailable_or_held"]
                return result
        if prescription.clinical_criterion:
            supplied = (clinical_review_inputs or {}).get(prescription.clinical_criterion)
            if supplied is None:
                result["reason_codes"] = ["reviewed_selection_unavailable_or_held"]
                return result
            evaluation = evaluate_clinical_review(supplied.context, supplied.reviews, as_of=as_of,
                lifecycle=supplied.lifecycle, registry=supplied.registry, trust=supplied.trust)
            if not evaluation.pin or evaluation.criterion_status != "pass" or not evaluation.prescription_valid:
                result["reason_codes"] = ["reviewed_selection_invalid"]
                return result
            review = next(supplied.registry.parse(value) for value in supplied.reviews
                          if (value.get("review_id") if isinstance(value, Mapping) else value.review_id) == evaluation.review_id)
            definition = supplied.registry.current(prescription.clinical_criterion)
            option = next(o for o in definition.options if o.option_id == review.selected_prescription.option_id)
            selection = review.selected_prescription
            if (selection.drill_id != identity or selection.bank_hash != prescription.bank_hash
                    or option.instructions != prescription.instructions):
                result["reason_codes"] = ["reviewed_selection_policy_mismatch"]
                return result
            reviewed_work = (evaluation, option, selection)
        # Neither fight-camp periodisation nor the daily verdict changes an injury's approved dose.
        selected_dose = prescription.dose
        dose = (selection.dose.model_dump(exclude_none=True) if reviewed_work else
                selected_dose.model_dump(exclude_none=True) if selected_dose else {})
        resolved_drills.append({
            "drill_id": prescription.drill_id, "drill": drill,
            "instructions": prescription.instructions,
            "dose": dose,
            "stop_when": prescription.stop_when, "frequency": prescription.frequency,
            "sources": prescription.sources,
            "bank_hash": prescription.bank_hash, "policy_id": policy.policy_id, "policy_version": policy.version,
            "policy_review_hash": policy.content_hash,
            "minimum_gap_days": prescription.minimum_gap_days,
            "is_loading": prescription.stage in {"load", "dynamic", "return"} or
                prescription.stage != "calm" and drill.get("function") in {"tendon_loading", "isometric_analgesia", "activation", "control"},
        })
        if permission_work:
            resolved_drills[-1].update(required_rehabilitation_level=STAGE_REHABILITATION_PERMISSION[prescription.stage],
                range_choice=prescription.range_choice, resistance=deepcopy(prescription.resistance),
                mandatory_restrictions=list(prescription.mandatory_restrictions))
        if reviewed_work:
            resolved_drills[-1].update(frequency=selection.cadence.frequency,
                minimum_gap_days=selection.cadence.minimum_gap_days,
                range_choice=selection.range_choice, resistance=selection.resistance.model_dump(mode="json"),
                mandatory_restrictions=list(selection.restrictions))
            from api.services.clinical_review_freeze import pin_reviewed_work
            resolved_drills[-1] = pin_reviewed_work(resolved_drills[-1], evaluation, option=option, selection=selection)
    resolved = resolved_drills[0]
    if bundle_ids:
        resolved = {
            **resolved, "drills": resolved_drills,
            "minimum_gap_days": max(d["minimum_gap_days"] for d in resolved_drills),
            "is_loading": any(d["is_loading"] for d in resolved_drills),
        }
    result.update(
        outcome="prescribed_rehab" if policy.activation == "live" else "unsupported_prescription",
        summary="Your rehab is matched to this injury's current recovery stage." if policy.activation == "live" else "This rehab routine is not active yet. Keep to your current restrictions.",
        reason_codes=[selected.selection_reason],
        prescription=resolved,
        restrictions={
            "blocked_regions": policy.blocked_regions, "blocked_tags": policy.blocked_tags,
            "contact_limit": policy.contact_limit,
        },
    )
    return result


def rehab_allocation_count(blocks: Sequence[Mapping[str, Any]], *, include_held: bool = False) -> int:
    """Count reviewed bundles once; preserve legacy per-block accounting."""
    return len({
        ("bundle", block["rehab_allocation_id"])
        if block.get("rehab_allocation_id") else ("legacy", index)
        for index, block in enumerate(blocks)
        if block.get("block_type") == "rehab" and (include_held or not block.get("_policy_held"))
    })


def _current_clearance_scopes(injuries: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    effective = effective_clinician_clearance(injuries)
    return [effective["scopes"]] if effective else []


_HARD_CONTACT_WORDS = ("hard", "live", "full contact", "full-contact", "all-out", "all out", "competition")
_TECHNICAL_CONTACT_WORDS = ("technical", "light", "controlled", "positional", "flow")


def _hard_contact(mapping: Mapping[str, Any] | None) -> bool:
    """Contact that needs the contact scope: hard sparring, not technical sparring.

    Only explicit technical wording or ``contact_level: controlled`` makes contact
    technical. Today derives ``effective_load`` from the day type, so it is not
    trusted to release anything; unqualified contact stays hard.
    """
    from .readiness_message import (
        _iter_session_mappings, _mapping_executable_text, _session_has_contact, _session_text,
        _text_mentions_exposure,
    )

    if not isinstance(mapping, Mapping) or not _session_has_contact(mapping):
        return False
    nested = ("blocks", "exercises", "movements", "items")
    for item in _iter_session_mappings(mapping):
        own = {key: value for key, value in item.items() if key not in nested and key != "effective_load"}
        if not _session_has_contact(own):
            continue
        text = f"{_session_text(own)} {_mapping_executable_text(own)}".lower()
        if (_text_mentions_exposure(text, _HARD_CONTACT_WORDS) or own.get("contact_level") == "full"
                or item.get("effective_load") == "hard"):
            return True
        if own.get("contact_level") != "controlled" and not _text_mentions_exposure(text, _TECHNICAL_CONTACT_WORDS):
            return True
    return False


def _clinician_clearance_hold(
    session: Mapping[str, Any] | None, injuries: Sequence[Mapping[str, Any]],
) -> str | None:
    """Enforce the effective scope independently of baseline policy relaxation."""
    from .readiness_message import _session_has_contact, is_support_session

    if not session:
        return None
    # A coach-owned/contact headline remains real work even if its app-owned
    # children carry only non-contact support blocks.
    contact = _session_has_contact(session) or _session_has_contact({**session, "blocks": []})
    for scopes in _current_clearance_scopes(injuries):
        if "training" not in scopes:
            rehab_only = (session.get("session_type") == "rehab" and bool(session.get("blocks"))
                          and all(b.get("block_type") == "rehab" for b in session["blocks"]))
            if contact or not (rehab_only or is_support_session(session)):
                return "Your reported clinician clearance is for rehab only. Normal training is on hold."
        elif "contact" not in scopes and (_hard_contact(session) or _hard_contact({**session, "blocks": []})):
            return "Your reported clinician clearance excludes hard sparring. Hard sparring is on hold."
    return None


def reconcile_session_prescription(
    session: Mapping[str, Any] | None, *, decisions: Sequence[Mapping[str, Any]],
    plan_id: str | None, training_day: str, frozen: Mapping[str, Any] | None = None,
    injuries: Sequence[Mapping[str, Any]] = (),
    allocation_ceiling: int | None = None,
) -> dict[str, Any] | None:
    """Project reviewed live work onto a copy. Never modify the stored camp.

    No unknown block is silently classified as safe, and no replacement expands
    the reviewed workload. Frozen content remains fixed; a new gate can stop it.
    """
    live = [d for d in decisions if d.get("activation") in {"live", "retired"}]
    if frozen:
        snapshot = sanitize_surface_guidance(frozen)
        if snapshot != frozen:
            snapshot["safety_hold"] = True
            snapshot["safety_hold_reason"] = "Saved surface guidance was withdrawn. Refresh your wound-care guidance."
        snapshot["frozen"] = True
        clearance_hold = _clinician_clearance_hold(snapshot.get("session"), injuries)
        if clearance_hold:
            snapshot["safety_hold"] = True
            snapshot.setdefault("safety_hold_reason", clearance_hold)
        snapshot["safety_hold"] = bool(snapshot.get("safety_hold")) or any(d.get("outcome") == "medical_review" for d in decisions)
        if any(d.get("activation") == "live" and d.get("injury_id") not in snapshot.get("injury_ids", []) for d in decisions):
            snapshot["safety_hold"] = True
        accepted = [b for b in snapshot.get("session", {}).get("blocks", []) if b.get("injury_id")]
        known_injuries = {row.get("id") for row in snapshot.get("injury_context", [])} or set(snapshot.get("injury_ids", []))
        if any(d.get("injury_id") not in known_injuries and d.get("outcome") != "wound_care" for d in decisions):
            snapshot["safety_hold"] = True
        for block in accepted:
            decision = next((d for d in decisions if d.get("injury_id") == block.get("injury_id")), None)
            if decision is None:
                snapshot["safety_hold"] = True
                continue
            current = decision.get("prescription") or {}
            member_ids = {p["drill_id"] for p in current.get("drills", [current]) if p.get("drill_id")}
            reserved_baseline = (decision.get("schedule", {}).get("state") == "already_completed"
                and (block or {}).get("drill_snapshot", {}).get("rehab_stage") in {"calm", "restore"}
                and (not (block or {}).get("is_loading") or decision.get("stage") == "restore"))
            if block and (decision.get("outcome") != "prescribed_rehab"
                          or block.get("injury_episode_id") != decision.get("injury_episode_id")
                          or (not reserved_baseline and block.get("rehab_drill_id") not in member_ids)
                          or block.get("policy_review_hash") != (decision.get("prescription") or {}).get("policy_review_hash")):
                snapshot["safety_hold"] = True
        return snapshot
    safe_session = sanitize_surface_guidance(session)
    withdrawn_surface = safe_session != session
    session = safe_session
    clearance_hold = _clinician_clearance_hold(session, injuries)
    if withdrawn_surface:
        clearance_hold = clearance_hold or "Saved surface guidance was withdrawn. Refresh your wound-care guidance."
    if not live and not clearance_hold and allocation_ceiling is None:
        return None
    prescribed = [d for d in live if d.get("outcome") == "prescribed_rehab" and d.get("prescription")
                  and d.get("schedule", {}).get("state", "due") == "due"]
    if session is None and not prescribed:
        return None
    entry = deepcopy(dict(session or {"session_id": f"rehab-{training_day}", "title": "Today's rehab", "session_type": "rehab", "blocks": []}))
    athlete_owned = entry.get("prescription_source") == "athlete"
    # A partial support projection needs accepted ownership even when no rehab
    # policy is live. Its caller retains the original day's allocation ceiling.
    restrictions = []
    for decision in live:
        current = dict(decision.get("restrictions", {}))
        injury = next((row for row in injuries if str(row.get("id")) == str(decision.get("injury_id"))
                       and str(row.get("episode_id")) == str(decision.get("injury_episode_id"))), None)
        if (injury and decision.get("activation") == "live"
                and decision.get("outcome") != "medical_review" and clinician_clears_baseline(injury)):
            current["blocked_regions"], current["blocked_tags"] = [], []
            # Training clearance covers technical sparring; hard sparring needs contact.
            if clinician_clears_baseline(injury, contact=True):
                current["contact_limit"] = "full"
            elif current.get("contact_limit", "none") == "none":
                current["contact_limit"] = "controlled"
        restrictions.append(current)
    blocked_regions = set().union(*(set(r.get("blocked_regions", [])) for r in restrictions))
    blocked_tags = set().union(*(set(r.get("blocked_tags", [])) for r in restrictions))
    contact_rank = {"none": 0, "controlled": 1, "full": 2}
    allowed_contact = min((contact_rank[r.get("contact_limit", "none")] for r in restrictions), default=contact_rank["full"])
    from .readiness_message import _session_has_contact, _is_non_physical_mapping

    policy_contact_limit = allowed_contact
    clearance_scopes = _current_clearance_scopes(injuries)
    contact_ceiling = any("training" in scopes and "contact" not in scopes for scopes in clearance_scopes)
    # A contact heading/coach-owned portion cannot be removed by editing children.
    # Otherwise the existing block replacement path can retain safe non-contact work.
    block_contact_ceiling = (contact_ceiling and bool(entry.get("blocks"))
                             and not _hard_contact({**entry, "blocks": []})
                             and all("training" in scopes for scopes in clearance_scopes))
    if block_contact_ceiling:
        allowed_contact = min(allowed_contact, contact_rank["controlled"])
    hold = bool(clearance_hold and not block_contact_ceiling) or any(d.get("outcome") == "medical_review" for d in decisions)
    # A current pull-back holds camp independently of tissue demand. Eligible
    # non-loading rehab can then use the existing standalone replacement path.
    readiness_hold = entry.get("session_type") != "rehab" and any(d.get("readiness_hold") for d in decisions)
    hold = hold or readiness_hold
    # The planner also uses a sparring type to budget app-owned support work.
    # An explicit contact headline/coach portion remains contact even when its
    # child blocks are only support; the allocation type alone is not that owner.
    headline = {**entry, "blocks": [], "session_type": ""}
    restriction_hold = False
    if _session_has_contact(headline) and (allowed_contact < contact_rank["controlled"]
                                           or (allowed_contact < contact_rank["full"] and _hard_contact(headline))):
        hold = restriction_hold = True
    # A blockless day has nothing to check against restrictions. Clearance that
    # removes them all releases it: training covers technical sparring, contact
    # covers hard sparring.
    cleared_blockless = (not blocked_regions and not blocked_tags
                         and any("training" in scopes for scopes in clearance_scopes)
                         and (allowed_contact == contact_rank["full"]
                              or (allowed_contact >= contact_rank["controlled"] and not _hard_contact(entry))))
    if session is not None and not entry.get("blocks") and entry.get("session_type") != "rehab" and not cleared_blockless:
        hold = True
    blocks, changes = [], []
    if clearance_hold and not block_contact_ceiling:
        changes.append({"action": "held", "reason": "clinician_clearance_ceiling"})

    def string_set(value):
        return set(value) if isinstance(value, list) and all(isinstance(item, str) for item in value) else None

    def contact_level(block):
        # Fitness work does not imply opponent contact. Explicit declarations
        # and combat-owned descriptions still retain their existing gates.
        if (block.get("contact_level") is None and block.get("block_type") in _NON_CONTACT_BLOCK_TYPES
                and not _session_has_contact(block)):
            return "none"
        value = block.get("contact_level")
        if value is None and _session_has_contact(block):
            return "full" if _hard_contact(block) else "controlled"
        return value if isinstance(value, str) else None

    replaced = set()

    def prescribed_block(decision, prescription):
        identity = f"rehab:{decision['injury_id']}:{decision['injury_episode_id']}"
        if decision["prescription"].get("drills"):
            identity = f"{identity}:{prescription['drill_id']}"
        block = {
            **({"injury_side": next((i.get("side") for i in injuries
                if str(i.get("id")) == str(decision["injury_id"])
                and str(i.get("episode_id")) == str(decision["injury_episode_id"])), None)}
                if prescription.get("required_rehabilitation_level") else {}),
            **({"rehab_allocation_id": f"rehab:{decision['injury_id']}:{decision['injury_episode_id']}"}
               if decision["prescription"].get("drills") else {}),
            "block_id": identity, "block_type": "rehab", "title": prescription["drill"]["name"],
            "display_name": prescription["drill"]["name"], "coaching_cues": [prescription["instructions"]],
            **({"duration": {"value": prescription["dose"]["duration_seconds"], "unit": "seconds"}} if "duration_seconds" in prescription["dose"] else {}),
            "rehab_drill_id": prescription["drill_id"], "exercise_key": normalize_exercise_key(prescription["drill_id"]), "injury_id": decision["injury_id"],
            "injury_episode_id": decision["injury_episode_id"], "target_regions": [decision["region"]],
            "dose": prescription["dose"], "instructions": prescription["instructions"],
            **prescription["dose"],
            "stop_rules": prescription["stop_when"], "drill_snapshot": prescription["drill"],
            "policy_id": prescription["policy_id"], "policy_version": prescription["policy_version"],
            "bank_hash": prescription["bank_hash"],
            "policy_review_hash": prescription["policy_review_hash"],
            "minimum_gap_days": decision["prescription"]["minimum_gap_days"],
            "is_loading": prescription["is_loading"],
            "source_references": prescription["sources"],
        }
        if prescription.get("required_rehabilitation_level"):
            block.update({key: deepcopy(prescription[key]) for key in (
                "required_rehabilitation_level", "range_choice", "resistance", "mandatory_restrictions", "frequency")})
        if prescription.get("clinical_review_pin"):
            block.update({key: deepcopy(prescription[key]) for key in (
                "range_choice", "resistance", "mandatory_restrictions", "frequency",
                "clinical_review_pin", "reviewed_prescription")})
        return block

    def prescribed_blocks(decision):
        current = decision["prescription"]
        return [prescribed_block(decision, member) for member in current.get("drills", [current])]

    for block in entry.get("blocks", []):
        # Explicit additions and swaps retain the athlete's selected exercise.
        change = block.get("athlete_change") or {}
        if change.get("action") in {"add", "swap"}:
            blocks.append(deepcopy(block))
            continue
        if _is_non_physical_mapping(block) and not blocked_tags:
            # Mindset/video review is not ankle/chest loading. Its combat topic
            # and absent physical metadata cannot turn it into contact work.
            blocks.append(deepcopy(block))
            continue
        if block.get("block_type") == "rehab":
            replacement = next((d for d in prescribed if
                block.get("policy_id") == d.get("policy_id")
                and block.get("injury_id") == d.get("injury_id")
                and block.get("injury_episode_id") == d.get("injury_episode_id")), None)
            if replacement:
                if (replacement["injury_id"], replacement["injury_episode_id"]) not in replaced:
                    blocks.extend(prescribed_blocks(replacement))
                replaced.add((replacement["injury_id"], replacement["injury_episode_id"]))
                changes.append({"block_id": block.get("block_id"), "action": "replaced", "reason": "current_episode_prescription"})
                continue
            # Legacy/other-episode work must stay visible or be explicitly held.
            if any(block.get("policy_id") == d.get("policy_id") and block.get("injury_id") == d.get("injury_id") for d in live):
                blocks.append({**deepcopy(block), "_policy_held": True})
                changes.append({"block_id": block.get("block_id"), "action": "held", "reason": "episode_rehab_not_due"})
                hold = True
                continue
        demands = string_set(block.get("mechanical_load_regions"))
        tags = string_set(block.get("tags"))
        contact = contact_level(block)
        uncertain = bool(blocked_regions) and demands is None
        uncertain = uncertain or bool((demands or set()) - canonical_rehab_locations())
        uncertain = uncertain or (bool(blocked_tags) and tags is None)
        uncertain = uncertain or (policy_contact_limit < contact_rank["full"] and contact not in contact_rank)
        clearance_contact = block_contact_ceiling and _hard_contact(block)
        incompatible = uncertain or bool((demands or set()) & blocked_regions) or bool((tags or set()) & blocked_tags)
        incompatible = incompatible or (contact in contact_rank and contact_rank[contact] > allowed_contact) or clearance_contact
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
                         and contact_level(a) in contact_rank and contact_rank[contact_level(a)] <= allowed_contact
                         and (not block_contact_ceiling or not _hard_contact(a))), None)
            if safe and not athlete_owned:
                blocks.append({**deepcopy(safe), "block_id": block.get("block_id")})
                changes.append({"block_id": block.get("block_id"), "action": "substituted"})
            elif not athlete_owned and clearance_contact and not uncertain and not ((demands or set()) & blocked_regions) and not ((tags or set()) & blocked_tags):
                changes.append({"block_id": block.get("block_id"), "action": "removed", "reason": "clinician_clearance_ceiling"})
            else:
                hold = restriction_hold = True
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
                if policy_contact_limit < contact_rank["full"] and contact not in contact_rank:
                    reasons.append("missing_contact_details")
                if clearance_contact or (contact in contact_rank and contact_rank[contact] > allowed_contact):
                    reasons.append("contact_restriction")
                changes.append({"block_id": block.get("block_id"), "action": "held", "reason": "no_reviewed_safe_substitution",
                                "restriction_reasons": reasons})
        else:
            blocks.append(deepcopy(block))
    # Match the existing camp allocation ceiling. More affected episodes are
    # explicit deferred decisions rather than extra, unbudgeted work.
    day_budget = 1 if "sparring" in str(entry.get("session_type") or "").lower() else 2
    if allocation_ceiling in (1, 2):
        day_budget = min(day_budget, allocation_ceiling)
    budget = max(0, day_budget - rehab_allocation_count(blocks))
    # Added rehab goes before the closing cool-down, not after it.
    rehab_at = len(blocks)
    while rehab_at and blocks[rehab_at - 1].get("block_type") == "cooldown_recovery":
        rehab_at -= 1
    for index, decision in enumerate(sorted((d for d in prescribed if (d["injury_id"], d["injury_episode_id"]) not in replaced),
                                             key=lambda d: d["injury_id"])):
        if athlete_owned:
            continue
        if index >= budget:
            changes.append({"injury_id": decision["injury_id"], "action": "deferred", "reason": "rehab_slot_budget"})
            continue
        added = prescribed_blocks(decision)
        blocks[rehab_at:rehab_at] = added
        rehab_at += len(added)
    if block_contact_ceiling and not blocks:
        hold = True
    entry["blocks"] = blocks
    rehab_only = session is None and bool(blocks) and all(
        block.get("block_type") == "rehab" and block.get("policy_id") and not block.get("_policy_held") for block in blocks)
    if hold and not athlete_owned and not any(d.get("outcome") == "medical_review" for d in decisions):
        reviewed_blocks = [block for block in blocks if block.get("block_type") == "rehab" and block.get("policy_id")
                           and not block.get("_policy_held")]
        manual_choices = any((block.get("athlete_change") or {}).get("action") in {"add", "swap"}
                             for block in blocks)
        if reviewed_blocks and not manual_choices:
            # Hold the original training while offering separately reviewed
            # rehab. It has its own completion identity, so a rehab completion
            # cannot assert that the scheduled strength/combat work was done.
            entry = {key: value for key, value in entry.items() if key in {
                "calendar_date", "weekday", "weekday_with_label", "day_label", "phase",
            }}
            entry.update(session_id=f"rehab-{training_day}", session_type="rehab", title="Today's rehab",
                         objective="Follow the reviewed rehab for your current injury guidance.",
                         effective_load="reduced", contact_level="none", blocks=reviewed_blocks)
            hold, rehab_only = False, True
    if session is None or rehab_only:
        # Only the allocations actually offered own this completion occurrence.
        # Exclude drill/stage/content changes: accepted snapshots remain frozen.
        owners = sorted({(str(b["injury_id"]), str(b["injury_episode_id"]))
                         for b in entry["blocks"] if b.get("injury_id") and b.get("injury_episode_id")})
        entry["session_id"] = f"rehab-{training_day}-{content_hash({'plan_id': plan_id, 'owners': owners})[:24]}"
    snapshot = {"plan_id": plan_id, "training_day": training_day, "session": entry, "changes": changes,
                "safety_hold": hold, "frozen": False, "engine_version": "2",
                "rehab_only": rehab_only,
                "held_session_id": session.get("session_id") if rehab_only and session else None,
                "injury_ids": sorted(d["injury_id"] for d in live),
                "allocation_limit": day_budget}
    if clearance_hold and hold and not any(d.get("outcome") == "medical_review" for d in decisions):
        snapshot["safety_hold_reason"] = clearance_hold
    # Standalone rehab with no planned session (a rest day, or training already
    # logged) is additional work: nothing was held, so there is no hold reason.
    if hold or (rehab_only and session is not None):
        reasons = {reason for change in changes for reason in change.get("restriction_reasons", [])}
        detail = ("The injury-loading details for your planned training are unavailable."
                  if reasons & {"missing_load_details", "unknown_load_regions"}
                  # Pull back is a readiness verdict, not an injury restriction.
                  else "Today's pull-back guidance holds your planned training."
                  if readiness_hold and not restriction_hold
                  else "Your planned training conflicts with the current injury restrictions.")
        snapshot["training_hold_reason"] = detail
        # Which rule held training, so every surface names the same cause.
        snapshot["training_hold_source"] = ("readiness" if readiness_hold and not restriction_hold and not clearance_hold
                                            and not any(d.get("outcome") == "medical_review" for d in decisions)
                                            else "injury")
        if hold:
            snapshot.setdefault("safety_hold_reason", detail)
    snapshot["revision"] = content_hash(snapshot)
    return snapshot
