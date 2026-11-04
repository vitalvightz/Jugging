"""One bank eligibility contract; no exercise registry or clinical sign-off.

Classification and a usable prescription are content. Athlete eligibility still
belongs to the shared pathway engine, selector, permissions and scheduler.
Legacy prescriptions remain readable, including immutable historical snapshots.
"""
from __future__ import annotations

from collections import Counter

from pydantic import ValidationError

from .injury_location_registry import canonicalize_location_from_registry
from .rehab_clinical import ClinicalPolicy, ClinicalPrescription, content_hash, policy_review_hash
from .rehab_schema import (
    CONTACT_LEVEL_VALUES, CONTRACTION_TYPE_VALUES, IMPACT_VALUES,
    LATERALITY_APPLICABILITY_VALUES, LOAD_VALUES, REHAB_FUNCTIONS,
    SPORT_SPECIFICITY_VALUES, VELOCITY_VALUES, canonical_rehab_types, is_surface_injury_type,
)


def bank_prescription(group: dict, drill: dict) -> tuple[ClinicalPrescription | None, list[str]]:
    """Project existing bank fields; null dose is missing, {} explicitly self-paced.

    ``prescription`` contains only scheduling/loading/source fields from the
    existing ClinicalPrescription model. Identity, movement, dose, severity and
    stop rules have one owner in the bank, and cannot be overridden there.
    Missing data never means low demand or permission to load.
    """
    reasons = []
    if group.get("type") not in canonical_rehab_types() or is_surface_injury_type(group.get("type")):
        reasons.append("not_a_musculoskeletal_bank_group")
    vocabularies = dict(function=REHAB_FUNCTIONS, impact=IMPACT_VALUES,
        load=LOAD_VALUES, velocity=VELOCITY_VALUES,
        laterality_applicability=LATERALITY_APPLICABILITY_VALUES,
        contraction_type=CONTRACTION_TYPE_VALUES, sport_specificity=SPORT_SPECIFICITY_VALUES,
        contact_level=CONTACT_LEVEL_VALUES)
    for key, values in vocabularies.items():
        if drill.get(key) not in values or drill.get(key) == "unknown":
            reasons.append(f"missing_or_unknown_{key}")
    for key in ("id", "name", "notes", "target_regions", "target_tissues", "evidence_notes"):
        if not drill.get(key):
            reasons.append(f"missing_{key}")
    for key in ("target_regions", "target_tissues"):
        values = drill.get(key)
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() or v == "unknown" for v in values):
            reasons.append(f"invalid_{key}")
    equipment = drill.get("equipment")
    if not isinstance(equipment, list) or any(not isinstance(e, str) or not e.strip() for e in equipment):
        reasons.append("missing_equipment")
    if not isinstance(drill.get("dose"), dict):
        reasons.append("missing_dose_or_explicit_self_paced_guidance")
    compatible = drill.get("compatible_injury_types")
    if (not isinstance(compatible, list) or not compatible
            or any(t not in canonical_rehab_types() or t == "unspecified" for t in compatible)):
        reasons.append("missing_injury_compatibility")
    extra = drill.get("prescription")
    if not isinstance(extra, dict) or not extra.get("sources") or not extra.get("frequency"):
        reasons.append("missing_prescription_sources_or_cadence")
    if reasons:
        return None, reasons
    owned = {"drill_id", "bank_hash", "stage", "instructions", "dose", "allowed_severities", "stop_when"}
    if owned & extra.keys():
        return None, ["prescription_overrides_bank_classification"]
    try:
        prescription = ClinicalPrescription.model_validate(dict(**extra,
            drill_id=drill["id"], bank_hash=content_hash(drill), stage=drill.get("rehab_stage"),
            instructions=drill["notes"], dose=drill["dose"] or None,
            allowed_severities=drill.get("allowed_severities"), stop_when=drill.get("stop_when")))
    except (ValidationError, TypeError):
        return None, ["invalid_prescription"]
    if prescription.stage not in {"calm", "restore"} and not (
            prescription.required_rehabilitation_level == "loading" and prescription.dose
            and prescription.range_choice and prescription.resistance and prescription.mandatory_restrictions):
        return None, ["missing_bounded_loading_prescription"]
    if prescription.clinical_criterion:
        return None, ["bank_content_cannot_create_clinical_review_requirement"]
    return prescription, []


def expand_bank_prescriptions(policy: ClinicalPolicy, bank: list[dict]) -> ClinicalPolicy:
    """Complete, compatible content joins existing options, never opens a stage.

    Hashes are calculated for integrity/freeze checks, not entered as another
    administrative approval. Existing sourced routines keep their exact dose.
    Duplicate bank identities fail closed rather than picking the first match.
    """
    counts = Counter(d.get("id") for g in bank for d in g.get("drills", []))
    prescriptions = list(policy.prescriptions)
    existing = {p.drill_id for p in prescriptions}
    sources = list(policy.evidence_sources)
    region = canonicalize_location_from_registry(policy.region)
    for group in bank:
        if canonicalize_location_from_registry(group.get("location")) != region:
            continue
        for drill in group.get("drills", []):
            if (drill.get("id") in existing or counts[drill.get("id")] != 1
                    or policy.injury_type not in (drill.get("compatible_injury_types") or [])
                    or drill.get("rehab_stage") not in policy.live_stages):
                continue
            prescription, _ = bank_prescription(group, drill)
            if prescription is None:
                continue
            prescriptions.append(prescription)
            sources.extend(s for s in prescription.sources if s not in sources)
    if prescriptions == policy.prescriptions:
        return policy
    expanded = policy.model_copy(update=dict(prescriptions=prescriptions, evidence_sources=sources))
    return ClinicalPolicy.model_validate({**expanded.model_dump(), "content_hash": policy_review_hash(expanded)})
