"""Exact symptom ownership and conservative baselines within existing pathways."""
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support import cover

from api.contracts.injury_policy import reconcile_session_prescription, resolve_injury_policy
from api.contracts.rehab_completion import build_rehab_exposure_event, resolve_rehab_completion
from api.contracts.rehab_evidence import read_exact_events
from api.contracts.rehab_schedule import schedule_rehab
from api.contracts.rehab_stage import resolve_rehab_stage
from api.services.rehab_completion_service import session_rehab_items
from fightcamp.rehab_clinical import content_hash, load_clinical_policies
from fightcamp.rehab_protocols import get_rehab_bank
from tests.test_tendon_family_coverage import DAY, PLAN, injury
from tools.seed_nonspecific_msk_family import PROFILES, SYMPTOMS

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "shoulder_pain", "elbow_pain", "wrist_pain", "hand_pain", "fingers_pain", "knee_pain", "hip_pain", "lower_back_pain",
    "neck_stiffness", "elbow_stiffness", "wrist_stiffness", "lower_back_stiffness",
    "neck_tightness", "shoulder_tightness", "neck_soreness", "shoulder_soreness",
}
RESTORABLE = {"lower_back_pain", "neck_stiffness", "wrist_stiffness"}
# Later source-dosed bank publication; the seed's own RESTORE set is unchanged.
EXPANDED_RESTORE = {"shoulder_pain", "elbow_pain", "wrist_pain", "knee_pain", "hip_pain", "elbow_stiffness",
                    "neck_tightness", "shoulder_tightness", "neck_soreness", "shoulder_soreness"}
PAIRS = [(key, value[0], value[1]) for key, value in PROFILES.items()]


def read(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def resolve(row, **kwargs):
    return resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(), **kwargs)


def frozen_work(row):
    decision = resolve(row)
    frozen = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY, injuries=[row])
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=frozen["session"]["session_id"], prescription=frozen)
    completion = dict(status="done", prescription_snapshot=frozen)
    return frozen, items, completion


@pytest.mark.parametrize("key,region,kind", PAIRS)
def test_exact_reviewed_identity_calm_restore_completion_and_readiness(key, region, kind):
    for stage in ("calm", "restore"):
        row = injury(region, kind, stage)
        decision = resolve(row)
        assert decision["injury_type"] == kind and decision["policy_id"] == key
        if stage == "restore" and key not in RESTORABLE | EXPANDED_RESTORE:
            assert decision["prescription"] is None and decision["reason_codes"] == ["stage_not_activated"]
            continue
        assert decision["outcome"] == "prescribed_rehab" and decision["stage"] == stage
        prescription = decision["prescription"]
        assert prescription["bank_hash"] == content_hash(prescription["drill"])
        # The daily verdict never changes rehab.
        activation = prescription["drill"]["function"] == "activation"
        assert prescription["dose"] == (prescription["drill"]["dose"] or {}) and prescription["is_loading"] == activation
        ledger = {r["drill_id"]: r for r in read("data/rehab_metadata_review.json")}
        assert ledger[prescription["drill_id"]]["review_state"] == "reviewed"
        assert schedule_rehab(row, decision, training_day=DAY, readiness_decision="pull_back")["state"] == "due"
        frozen, items, completion = frozen_work(row)
        eligible = resolve_rehab_completion(items, [row], completion=completion).eligible
        assert len(eligible) == 1
        event = build_rehab_exposure_event(eligible[0], athlete_id=row["athlete_id"], plan_id=PLAN,
            session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="same", limit="no")
        assert event.is_attributable_to(row)


@pytest.mark.parametrize("key,region,kind,other_kind", cover(PAIRS, sorted(SYMPTOMS | {"strain", "sprain", "instability", "tendonitis", "impingement", "contusion", "hyperextension"})))
def test_exact_type_and_region_boundaries(key, region, kind, other_kind):
    policy = next(p for p in load_clinical_policies() if p.policy_id == key)
    if other_kind != kind:
        assert resolve_injury_policy(injury(region, other_kind), policies=[policy], bank=get_rehab_bank())["prescription"] is None
    other_region = "wrist" if region != "wrist" else "neck"
    assert resolve_injury_policy(injury(other_region, kind), policies=[policy], bank=get_rehab_bank())["prescription"] is None


@pytest.mark.parametrize("key,region,kind", PAIRS)
def test_stale_hashes_fail_closed_and_worsening_returns_calm(key, region, kind):
    policy = next(p for p in load_clinical_policies() if p.policy_id == key)
    bank = deepcopy(get_rehab_bank())
    drill = next(d for g in bank for d in g["drills"] if d["id"] == policy.prescriptions[0].drill_id)
    drill["notes"] += " Unreviewed change."
    assert resolve_injury_policy(injury(region, kind), policies=[policy], bank=bank)["prescription"] is None
    worse = resolve(injury(region, kind, "restore", latest_reported_status="worse"))
    assert worse["stage"] == "calm" and worse["prescription"]["drill"]["rehab_stage"] == "calm"


DANGER = ["major trauma", "deformity", "unable to bear weight", "cannot use the limb", "rapidly increasing swelling",
    "large unexplained swelling", "hot joint with systemic illness", "fever", "neurological symptoms", "progressive weakness",
    "numbness", "altered sensation", "vascular compromise", "cold hand", "pale limb", "severe unexplained pain",
    "night pain with systemic symptoms", "locking mechanical block", "recurrent giving way", "suspected fracture",
    "suspected dislocation", "suspected complete tear", "open wound", "chest pain after trauma",
    "abdominal pain after trauma", "calf swelling with breathlessness", "after a fall", "joint is red"]


@pytest.mark.parametrize("key,region,kind,text", cover(PAIRS, DANGER))
def test_scoped_serious_symptoms_remain_medical(key, region, kind, text):
    decision = resolve(injury(region, kind, description=text))
    assert decision["outcome"] == "medical_review" and decision["prescription"] is None


@pytest.mark.parametrize("key,region,kind", PAIRS)
@pytest.mark.parametrize("text", ["hot swollen joint", "wrist is swollen and red"])
def test_hot_swollen_joint_does_not_require_fever_to_fail_closed(key, region, kind, text):
    assert resolve(injury(region, kind, description=text))["outcome"] == "medical_review"


@pytest.mark.parametrize("key,region,kind,changes", cover(PAIRS, [dict(severity="high"), dict(severity="severe"), dict(injury_type="post_surgery"),
    dict(injury_type="tendon_rupture"), dict(rehab_medical_gate=True)]))
def test_existing_structural_and_severe_routes(key, region, kind, changes):
    assert resolve(injury(region, kind, **changes))["outcome"] == "medical_review"


@pytest.mark.parametrize("key,region,kind", PAIRS)
@pytest.mark.parametrize("stage", ["load", "dynamic", "return"])
def test_advanced_injection_cannot_activate_even_reviewed_inventory(key, region, kind, stage):
    row = injury(region, kind, "restore", rehab_stage=stage)
    decision = resolve(row)
    assert decision["stage"] == "calm" and decision["prescription"]["drill"]["rehab_stage"] == "calm"
    policy = next(p for p in load_clinical_policies() if p.policy_id == key)
    assert not any(t.promotable for t in policy.transitions)
    bank = deepcopy(get_rehab_bank())
    allowed = {p.drill_id for p in policy.prescriptions}
    for group in bank:
        if group["type"] == kind:
            for d in group["drills"]:
                if d["id"] not in allowed:
                    d.update(rehab_stage=stage, function="control", load="low", impact="none", velocity="low",
                        equipment=[], target_regions=[region], target_tissues=[region], laterality_applicability="side_specific",
                        contraction_type="mixed", sport_specificity="general_rehab", contact_level="none")
    assert resolve_injury_policy(row, policies=[policy], bank=bank)["prescription"]["drill_id"] in allowed


@pytest.mark.parametrize("key,region,kind", PAIRS)
def test_clearance_camp_time_and_tolerated_history_do_not_create_progression(key, region, kind):
    stage = "restore" if key in RESTORABLE else "calm"
    row = injury(region, kind, stage, created_at="2020-01-01T00:00:00Z")
    row["clinician_clearance"] = dict(episode_id=row["episode_id"], scopes=["rehab", "training", "contact"])
    for phase in ("GPP", "SPP", "TAPER"):
        assert resolve(row, phase=phase, readiness_decision="train_as_planned")["stage"] == stage
    frozen, items, completion = frozen_work(row)
    candidate = resolve_rehab_completion(items, [row], completion=completion).eligible[0]
    event = build_rehab_exposure_event(candidate, athlete_id=row["athlete_id"], plan_id=PLAN,
        session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="better", limit="no")
    raw = event.model_dump(mode="json")
    raw["response"]["next_day_response"] = "same"
    rows = [dict(athlete_id=row["athlete_id"], event_json=raw)]
    for truncated in (False, True):
        assert resolve(row, exposures=rows, history_truncated=truncated)["stage"] == stage
    raw["response"]["next_day_response"] = "worse"
    assert resolve(row, exposures=rows)["stage"] == "calm"
    row["description"] = "rapidly increasing swelling"
    assert reconcile_session_prescription(None, decisions=[resolve(row)], plan_id=PLAN, training_day=DAY,
        frozen=frozen, injuries=[row])["safety_hold"]


@pytest.mark.parametrize("key,region,kind", PAIRS)
def test_unknown_side_and_exact_episode_type_region_ownership(key, region, kind):
    row = injury(region, kind, side="unknown")
    frozen, items, completion = frozen_work(row)
    eligible = resolve_rehab_completion(items, [row], completion=completion).eligible
    assert len(eligible) == 1
    event = build_rehab_exposure_event(eligible[0], athlete_id=row["athlete_id"], plan_id=PLAN,
        session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="same", limit="no")
    assert event.side == "unknown" and event.is_attributable_to(row)
    raw = dict(athlete_id=row["athlete_id"], event_json=event.model_dump(mode="json"))
    assert not read_exact_events(athlete_id=row["athlete_id"], injury=row, exposure_rows=[raw])[0]
    for changes in [dict(episode_id=str(uuid4())), dict(body_region="hip" if region != "hip" else "knee"),
        dict(injury_type="swelling", rehab_type="swelling")]:
        changed = {**row, **changes}
        assert not resolve_rehab_completion(items, [changed], completion=completion).eligible
        assert not event.is_attributable_to(changed)
    # Known-side evidence is also rejected when only the symptom label changes.
    known = {**row, "side": "left"}
    frozen, items, completion = frozen_work(known)
    candidate = resolve_rehab_completion(items, [known], completion=completion).eligible[0]
    event = build_rehab_exposure_event(candidate, athlete_id=row["athlete_id"], plan_id=PLAN,
        session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="same", limit="no")
    changed = {**known, "injury_type": "swelling"}
    evidence, ignored = read_exact_events(athlete_id=row["athlete_id"], injury=changed,
        exposure_rows=[dict(athlete_id=row["athlete_id"], event_json=event.model_dump(mode="json"))])
    assert not evidence and ignored["ignored_type_mismatch"] == 1


def test_every_unprofiled_original_combination_is_closed_and_swelling_inventory_separate():
    audit = read("docs/nonspecific-msk-bank-audit.json")
    assert len(audit) == 358
    expected_counts = {"pain": 86, "soreness": 76, "tightness": 84, "stiffness": 68, "swelling": 44}
    for kind, count in expected_counts.items():
        assert sum(r["injury_type"] == kind for r in audit) == count
    active = {(r, t) for _, r, t in PAIRS}
    for region, kind in {(r["region"], r["injury_type"]) for r in audit} - active:
        assert resolve(injury(region, kind))["prescription"] is None
    assert not any(p.injury_type == "swelling" for p in load_clinical_policies())
    assert resolve(injury("calf", "swelling", description="unexplained calf swelling on one leg"))["outcome"] == "medical_review"
    assert resolve(injury("ankle", "swelling", description="swelling with fever and hot joint"))["outcome"] == "medical_review"


@pytest.mark.parametrize("region,kind", [("hamstring", "strain"), ("ankle", "sprain"), ("achilles", "tendonitis"),
    ("shoulder", "impingement"), ("elbow", "hyperextension"), ("quads", "contusion")])
def test_specific_injury_with_symptom_words_keeps_its_pathway(region, kind):
    decision = resolve(injury(region, kind, description=f"{region} {kind} with pain soreness tightness stiffness swelling"))
    assert decision["policy_id"] == f"{region}_{kind}" and decision["injury_type"] == kind
    assert decision["prescription"] is not None


@pytest.mark.parametrize("description", ["diagnosed shoulder impingement with pain", "shoulder strain and soreness",
    "shoulder contusion with stiffness", "shoulder sprain and pain"])
def test_conflicting_generic_identity_cannot_downgrade_a_specific_injury(description):
    row = injury("shoulder", "pain")
    frozen, _, _ = frozen_work(row)
    row["description"] = description
    decision = resolve(row)
    assert decision["outcome"] == "missing_information" and decision["prescription"] is None
    assert decision["reason_codes"] == ["specific_injury_identity_conflict"]
    assert reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY,
        frozen=frozen, injuries=[row])["safety_hold"]


def test_negated_specific_injury_is_not_inferred_and_symptom_cause_not_diagnosed():
    decision = resolve(injury("shoulder", "soreness", description="shoulder soreness; no strain"))
    assert decision["injury_type"] == "soreness" and decision["prescription"] is not None
    for key, region, kind in PAIRS:
        assert resolve(injury(region, kind))["injury_type"] == kind


def test_multiple_symptom_words_in_one_episode_do_not_duplicate_today_work():
    row = injury("shoulder", "pain", description="shoulder pain with soreness and tightness")
    decision = resolve(row)
    assert decision["injury_type"] == "pain" and decision["policy_id"] == "shoulder_pain"
    frozen, items, completion = frozen_work(row)
    assert len(frozen["session"]["blocks"]) == 1 and len(items) == 1
    assert len(resolve_rehab_completion(items, [row], completion=completion).eligible) == 1


def test_stored_rehab_type_conflict_and_untyped_red_flags_fail_closed():
    assert resolve(injury("shoulder", "pain", rehab_type="impingement"))["reason_codes"] == ["specific_injury_identity_conflict"]
    row = injury("wrist", "pain", description="wrist pain with fever and hot joint")
    row.pop("injury_type")
    assert resolve(row)["outcome"] == "medical_review"
    assert resolve_rehab_stage(row).medical_gate
    assert resolve(injury("ankle", "swelling", description="ankle swelling", side="left"))["outcome"] == "medical_review"


def test_symptom_exposure_cannot_cross_known_side():
    row = injury("wrist", "stiffness", "restore")
    frozen, items, completion = frozen_work(row)
    candidate = resolve_rehab_completion(items, [row], completion=completion).eligible[0]
    event = build_rehab_exposure_event(candidate, athlete_id=row["athlete_id"], plan_id=PLAN,
        session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="same", limit="no")
    changed = {**row, "side": "right"}
    assert not event.is_attributable_to(changed)
    assert not read_exact_events(athlete_id=row["athlete_id"], injury=changed,
        exposure_rows=[dict(athlete_id=row["athlete_id"], event_json=event.model_dump(mode="json"))])[0]


@pytest.mark.parametrize("kind", ["strain", "sprain", "instability", "tendonitis", "impingement", "hyperextension", "contusion"])
def test_additional_symptom_screen_does_not_change_other_family_stage_rules(kind):
    assert not resolve_rehab_stage(injury("shoulder", kind, description="fever and progressive weakness")).medical_gate


@pytest.mark.parametrize("first,second", [(('quads','strain'),('quads','soreness')),
    (('ankle','sprain'),('ankle','swelling')), (('wrist','tendonitis'),('wrist','pain')),
    (('wrist','contusion'),('wrist','stiffness')), (('neck','tightness'),('neck','soreness')),
    (('neck','stiffness'),('wrist','pain'))])
def test_multi_injury_restrictions_and_episode_ownership_do_not_weaken(first, second):
    rows = [injury(*first), injury(*second)]
    decisions = [resolve(row) for row in rows]
    session = dict(session_id="contact", session_type="sparring", title="Sparring", blocks=[dict(block_id="contact",
        block_type="conditioning", mechanical_load_regions=[first[0]], tags=["contact"], contact_level="full")])
    combined = reconcile_session_prescription(session, decisions=decisions, plan_id=PLAN, training_day=DAY, injuries=rows)
    assert combined is not None
    assert all(b.get("contact_level") != "full" or b.get("_policy_held") for b in combined["session"]["blocks"])
    blocks = [b for b in combined["session"]["blocks"] if b.get("injury_id")]
    ownership = [(b["injury_id"], b["injury_episode_id"], b.get("policy_id")) for b in blocks]
    assert len(ownership) == len(set(ownership))
    assert all(b["injury_id"] in {r["id"] for r in rows} for b in blocks)


def test_seeds_and_metadata_generation_are_byte_idempotent(tmp_path, monkeypatch):
    from tools import seed_nonspecific_msk_family
    from tools.generate_rehab_metadata_review import build_ledger
    folder = tmp_path / "data"
    folder.mkdir()
    names = ["rehab_bank.json", "rehab_metadata_review.json", "rehab_pathways.json"]
    from tests.rehab_inventory_history import pre_restore_expansion_bytes
    # Dated seed: later RESTORE publication is reconstructed from its exact history fixture.
    expected = {name: pre_restore_expansion_bytes(name) for name in names}
    for name, data in expected.items():
        (folder / name).write_bytes(data)
    monkeypatch.setattr(seed_nonspecific_msk_family, "ROOT", tmp_path)
    seed_nonspecific_msk_family.main()
    seed_nonspecific_msk_family.main()
    assert {name:(folder / name).read_bytes() for name in names} == expected
    assert build_ledger(json.loads(expected["rehab_bank.json"]), prior=json.loads(expected["rehab_metadata_review.json"])) == json.loads(expected["rehab_metadata_review.json"])
