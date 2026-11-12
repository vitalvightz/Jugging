"""Versioned rehab policies. The legacy module name is kept for compatibility.

A live policy is composed from a pathway family and a regional profile in
``data/rehab_pathways.json`` (see :mod:`fightcamp.rehab_pathways`). Activation
is a software/data validation step, not clinician approval.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer

from .config import DATA_DIR
from .injury_location_registry import canonicalize_location_from_registry
from .rehab_pathways import PathwayCatalog, PathwayTransition, TransitionOverride, compose_transitions
from .rehab_schema import REHAB_STAGES, canonical_rehab_locations, canonical_rehab_types


def content_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def policy_review_hash(policy) -> str:
    # Historical snapshots use policy_review_hash; its meaning is now content
    # integrity only. No clinician identity or sign-off is asserted.
    raw = policy.model_dump(exclude={"content_hash", "status", "activation", "pathway_family"})
    # Preserve hashes of existing single-drill policies exactly.
    if not raw.get("stage_bundles"):
        raw.pop("stage_bundles", None)
    # Only a transition that can change a stage is reviewed content. Blocked
    # transitions (no clinical criterion, or closed) are diagnostics, so adding
    # the shared safety baseline leaves existing hashes and frozen work intact.
    raw["transitions"] = [t.model_dump() for t in policy.transitions if t.promotable]
    return content_hash(raw)


class ClinicalDose(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sets: int | None = Field(default=None, gt=0)
    reps: int | None = Field(default=None, gt=0)
    duration_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def measurable(self):
        if self.reps is None and self.duration_seconds is None:
            raise ValueError("a prescription needs reps or duration")
        return self


class ClinicalPrescription(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    drill_id: str = Field(min_length=1)
    bank_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    stage: Literal["calm", "restore", "load", "dynamic", "return"]
    instructions: str = Field(min_length=1)
    dose: ClinicalDose | None = None
    camp_doses: dict[str, ClinicalDose] = Field(default_factory=dict)
    readiness_doses: dict[Literal["modify", "pull_back"], ClinicalDose] = Field(default_factory=dict)
    allowed_severities: list[Literal["low", "moderate", "high"]] = Field(min_length=1)
    stop_when: list[str] = Field(min_length=1)
    frequency: Literal["scheduled_sessions", "daily"] = "scheduled_sessions"
    minimum_gap_days: int = Field(default=1, ge=1, le=14)
    priority: int = 0
    sources: list[str] = Field(default_factory=list)
    clinical_criterion: str | None = None
    required_rehabilitation_level: Literal["loading"] | None = None
    range_choice: str | None = None
    resistance: dict | None = None
    mandatory_restrictions: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def preserve_baseline_shape(self, handler):
        raw = handler(self)
        if self.clinical_criterion is None:
            raw.pop("clinical_criterion", None)
        if self.required_rehabilitation_level is None:
            raw.pop("required_rehabilitation_level", None)
        for key in ("range_choice", "resistance", "mandatory_restrictions"):
            if not raw.get(key):
                raw.pop(key, None)
        return raw

    @model_validator(mode="after")
    def bounded_camp_dose(self):
        if not self.instructions.strip() or any(not rule.strip() for rule in self.stop_when):
            raise ValueError("instructions and stop rules must not be blank")
        if self.resistance is not None:
            mode, kg = self.resistance.get("mode"), self.resistance.get("kg")
            if (set(self.resistance) != {"mode", "kg"} or
                    not (mode == "bodyweight" and kg is None or
                         mode == "external_kg" and isinstance(kg, (int, float)) and not isinstance(kg, bool)
                         and math.isfinite(kg) and kg >= 0)):
                raise ValueError("prescribed resistance must be bodyweight or an explicit nonnegative weight")
        if any((self.range_choice, self.resistance, self.mandatory_restrictions)) and not (
                self.range_choice and self.resistance and self.mandatory_restrictions and self.dose):
            raise ValueError("loading limits need range, resistance, restrictions and dose together")
        if self.range_choice is not None and (not self.range_choice.strip() or any(not r.strip() for r in self.mandatory_restrictions)):
            raise ValueError("prescription range and restrictions must not be blank")
        if any(phase not in {"GPP", "SPP", "TAPER"} for phase in self.camp_doses):
            raise ValueError("unknown camp phase")
        for dose in [*self.camp_doses.values(), *self.readiness_doses.values()]:
            for name in ClinicalDose.model_fields:
                value, ceiling = getattr(dose, name), getattr(self.dose, name) if self.dose else None
                if value is not None and (ceiling is None or value > ceiling):
                    raise ValueError("context dose may only reduce the reviewed prescription")
        return self


class ClinicalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    #: None only for a legacy standalone policy read from schema v2 data.
    pathway_family: str | None = None
    region: str = Field(min_length=1)
    injury_type: str = Field(min_length=1)
    status: Literal["draft", "active", "retired"] = "draft"
    activation: Literal["shadow", "live"] = "shadow"
    evidence_sources: list[str] = Field(default_factory=list)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    prescriptions: list[ClinicalPrescription] = Field(default_factory=list)
    blocked_regions: list[str] = Field(default_factory=list)
    blocked_tags: list[str] = Field(default_factory=list)
    contact_limit: Literal["none", "controlled", "full"] = "none"
    live_stages: list[Literal["calm", "restore", "load", "dynamic", "return"]] = Field(default_factory=lambda: ["calm", "restore"])
    #: Composed family + profile transitions. Evaluated by
    #: ``api.contracts.rehab_progression``; only promotable ones can move a stage.
    transitions: list[PathwayTransition] = Field(default_factory=list)
    # Explicit compatibility review, not a count applied to ranked alternatives.
    stage_bundles: dict[Literal["calm", "restore", "load", "dynamic", "return"], list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_active_policy(self):
        if self.region not in canonical_rehab_locations() or self.injury_type not in canonical_rehab_types():
            raise ValueError("unknown policy region or injury type")
        if self.status == "active" and not (self.evidence_sources and self.prescriptions):
            raise ValueError("active policy needs sources and routines")
        if self.activation == "live" and self.status != "active":
            raise ValueError("inactive policy cannot be live")
        if self.status == "active":
            if any(not source.strip() for source in self.evidence_sources):
                raise ValueError("policy sources must identify evidence")
            if any(not p.sources or any(not s.strip() or s not in self.evidence_sources for s in p.sources) for p in self.prescriptions):
                raise ValueError("active routines need instruction and cadence sources")
            if any(s not in self.evidence_sources for t in self.transitions if t.promotable
                   for r in t.requirements for s in r.sources):
                raise ValueError("active transition criteria need policy evidence sources")
        if len({p.drill_id for p in self.prescriptions}) != len(self.prescriptions):
            raise ValueError("duplicate prescription identity")
        by_id = {p.drill_id: p for p in self.prescriptions}
        for stage, ids in self.stage_bundles.items():
            if stage not in self.live_stages or not ids or len(set(ids)) != len(ids):
                raise ValueError("bundle needs unique drills in an activated stage")
            if any(identity not in by_id or by_id[identity].stage != stage for identity in ids):
                raise ValueError("bundle drills must have reviewed prescriptions in the same stage")
            if len({by_id[identity].frequency for identity in ids}) != 1:
                raise ValueError("bundle drills must share a scheduling frequency")
        if len({t.to_stage for t in self.transitions}) != len(self.transitions):
            raise ValueError("duplicate transition criteria")
        higher = [stage for stage in self.live_stages if stage not in {"calm", "restore"}]
        if higher and list(self.live_stages) != list(REHAB_STAGES[:len(self.live_stages)]):
            raise ValueError("a live higher stage needs every lower stage live, in ladder order")
        by_target = {t.to_stage: t for t in self.transitions}
        for stage in higher:
            # A higher stage is reachable only through reviewed, sourced criteria
            # and only when reviewed content exists for it.
            if stage not in by_target or not by_target[stage].promotable:
                raise ValueError(f"{stage} cannot be live without an open transition with clinical criteria")
            if not any(p.stage == stage for p in self.prescriptions):
                raise ValueError(f"{stage} cannot be live without a reviewed prescription")
        for prescription in self.prescriptions:
            if prescription.clinical_criterion and not any(
                    t.to_stage == prescription.stage and t.promotable and
                    any(r.checkpoint == prescription.clinical_criterion and r.basis == "clinical" for r in t.requirements)
                    for t in self.transitions):
                raise ValueError("clinician-selected prescription needs its exact open clinical transition")
        if set(self.blocked_regions) - canonical_rehab_locations():
            raise ValueError("unknown restricted region")
        if self.status == "active" and self.content_hash != policy_review_hash(self):
            raise ValueError("policy content hash is missing or stale")
        return self


PATHWAYS_PATH = DATA_DIR / "rehab_pathways.json"


def compose_policy(catalog: PathwayCatalog, profile: dict, *, bank: list[dict] | None = None) -> ClinicalPolicy:
    """Compose one live policy from its family and regional profile."""
    profile = dict(profile)
    family = catalog.family(str(profile.get("pathway_family") or ""))
    if profile.get("injury_type") not in family.injury_types:
        raise ValueError(f"{profile.get('policy_id')}: injury type is outside pathway family {family.family_id}")
    if "transitions" in profile:
        raise ValueError("profiles declare transition_overrides; families own transitions")
    overrides = {key: TransitionOverride.model_validate(value)
                 for key, value in (profile.pop("transition_overrides", None) or {}).items()}
    policy = ClinicalPolicy.model_validate({**profile, "transitions": compose_transitions(catalog, family, overrides)})
    if policy.status == "active" and policy.activation == "live":
        from .rehab_publication import expand_bank_prescriptions
        if bank is None:
            bank = json.loads((DATA_DIR / "rehab_bank.json").read_text(encoding="utf-8"))
        policy = expand_bank_prescriptions(policy, bank)
    return policy


def load_pathway_catalog(path: Path | None = None) -> PathwayCatalog:
    raw = json.loads((path or PATHWAYS_PATH).read_text(encoding="utf-8"))
    return PathwayCatalog.model_validate(raw)


_DEFAULT_POLICIES: tuple[tuple, tuple[ClinicalPolicy, ...]] | None = None


def _file_identity(path: Path) -> tuple:
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size)


def load_clinical_policies(path: Path | None = None, *, bank: list[dict] | None = None) -> tuple[ClinicalPolicy, ...]:
    """The composed clinical policies.

    The default files are composed once and reused while neither file changes
    on disk: a Today build used to re-read and re-validate both JSON files
    several times per request. Policies are frozen models and nothing mutates
    them, so sharing the tuple is the same as composing it again. An explicit
    ``path`` or ``bank`` always composes fresh.
    """
    global _DEFAULT_POLICIES
    if path is not None or bank is not None:
        return _compose_clinical_policies(path, bank=bank)
    identity = (_file_identity(PATHWAYS_PATH), _file_identity(DATA_DIR / "rehab_bank.json"))
    cached = _DEFAULT_POLICIES
    if cached is not None and cached[0] == identity:
        return cached[1]
    policies = _compose_clinical_policies(None)
    _DEFAULT_POLICIES = (identity, policies)
    return policies


def _compose_clinical_policies(path: Path | None = None, *, bank: list[dict] | None = None) -> tuple[ClinicalPolicy, ...]:
    raw = json.loads((path or PATHWAYS_PATH).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("schema_version") == 2 and "policies" in raw:
        # Read-only compatibility for the pre-pathway policy file format. It is
        # not an activation route: only family + profile composition may carry
        # transitions or a live stage above RESTORE.
        policies = tuple(ClinicalPolicy.model_validate(item) for item in raw["policies"])
        if any(p.transitions or set(p.live_stages) - {"calm", "restore"} for p in policies):
            raise ValueError("legacy policy files cannot declare transitions or higher live stages")
    elif isinstance(raw, dict) and raw.get("schema_version") == 1 and "families" in raw:
        catalog = PathwayCatalog.model_validate(raw)
        if bank is None:
            bank = json.loads((DATA_DIR / "rehab_bank.json").read_text(encoding="utf-8"))
        # Derive candidate targets from content once. This replaces the rollout
        # list without rescanning the full bank for unrelated profiles.
        targets = {
            (canonicalize_location_from_registry(group.get("location")), kind)
            for group in bank for drill in group.get("drills", [])
            if isinstance(drill.get("prescription"), dict)
            and isinstance(drill.get("compatible_injury_types"), list)
            for kind in drill["compatible_injury_types"] if isinstance(kind, str)
        }
        policies = tuple(compose_policy(catalog, profile, bank=bank if (
            canonicalize_location_from_registry(profile.get("region")), profile.get("injury_type")) in targets
            else []) for profile in catalog.profiles)
    else:
        raise ValueError("unsupported clinical policy schema")
    keys = [(p.region, p.injury_type) for p in policies]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate clinical injury policy")
    return policies


def validate_pathway_catalog(catalog: PathwayCatalog) -> list[str]:
    """Every musculoskeletal rehab type belongs to exactly one pathway family."""
    from .rehab_schema import is_surface_injury_type
    errors = []
    msk = {t for t in canonical_rehab_types() if not is_surface_injury_type(t) and t != "unspecified"}
    owned = {t for f in catalog.families for t in f.injury_types}
    if owned - msk:
        errors.append(f"pathway families claim non-MSK or unknown injury types: {sorted(owned - msk)}")
    if msk - owned:
        errors.append(f"MSK injury types without a pathway family: {sorted(msk - owned)}")
    return errors


def validate_clinical_bank(policies: tuple[ClinicalPolicy, ...], bank: list[dict]) -> list[str]:
    from .injury_location_registry import canonicalize_location_from_registry
    indexed: dict[str, list[tuple[dict, dict]]] = {}
    for group in bank:
        for drill in group.get("drills", []):
            indexed.setdefault(str(drill.get("id") or ""), []).append((group, drill))
    errors: list[str] = []
    for policy in policies:
        if policy.status == "retired":
            continue
        for prescription in policy.prescriptions:
            matches = indexed.get(prescription.drill_id, [])
            if len(matches) != 1:
                errors.append(f"{policy.policy_id}: drill identity is missing or ambiguous: {prescription.drill_id}")
                continue
            group, drill = matches[0]
            if content_hash(drill) != prescription.bank_hash:
                errors.append(f"{policy.policy_id}: stale bank review: {prescription.drill_id}")
            if (canonicalize_location_from_registry(group.get("location")) != canonicalize_location_from_registry(policy.region)
                    or (group.get("type") not in {policy.injury_type, "unspecified"}
                        and policy.injury_type not in drill.get("compatible_injury_types", []))):
                errors.append(f"{policy.policy_id}: incompatible drill region or injury type")
            if policy.status == "active":
                for field in ("load", "impact", "velocity", "equipment", "function", "target_tissues"):
                    value = drill.get(field)
                    if value is None or value == "unknown":
                        errors.append(f"{policy.policy_id}: incomplete {field}: {prescription.drill_id}")
                if drill.get("rehab_stage") != prescription.stage:
                    errors.append(f"{policy.policy_id}: prescription stage differs from bank review")
    return errors
