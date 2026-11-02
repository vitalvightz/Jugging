"""Bounded source corrections exercise real validated generator consumers."""
from copy import deepcopy
import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from api.services.training_library import LibraryCache
from fightcamp import bank_schema, conditioning
from fightcamp.injury_filtering import injury_match_details
from fightcamp.restriction_parsing import parse_restriction_entry
from fightcamp.tagging import normalize_tags
from fightcamp.training_catalogue import build_catalogue
from tests.test_generator_equipment_accuracy import flags, generate, record
from tools.audit_metadata_integrity import (
    ROOT, mechanical_baseline_sources, reviewed_mechanical_changes, reverse_mechanical_source,
)

CHANGES = reviewed_mechanical_changes()
EQUIPMENT = ['battle_rope', 'battle_ropes', 'trap_bar', 'pins', 'pullup_bar']
SCENARIOS = [(35, 'low', 3), (35, 'high', 1), (18, 'low', 3), (10, 'high', 1),
             (7, 'low', 3), (3, 'high', 1)]


def run(monkeypatch, row, athlete_flags):
    source = 'conditioning_bank.json' if 'system' in row else 'exercise_bank.json'
    validated = bank_schema.validate_training_item(deepcopy(row), source=source,
                                                    require_phases=True, mode='runtime')
    return generate(monkeypatch, validated, athlete_flags)


def candidate(output, row):
    reservoir = output[3][5] if 'system' in row else output[3]['candidate_reservoir']
    key = 'drill' if 'system' in row else 'exercise'
    return next((c for role, values in reservoir.items() if not role.startswith('__')
                 for c in values if c[key]['name'] == row['name']), None)


def test_exact_seven_source_deltas_preserve_every_identity_and_other_row():
    after = {e['catalogue_id']: e for e in build_catalogue()}
    with mechanical_baseline_sources() as before_root:
        before = {e['catalogue_id']: e for e in build_catalogue(before_root / 'data')}
    assert len(after) == len(before) == 2889 and after.keys() == before.keys()
    changes = {c['catalogue_id']: c for c in CHANGES}
    assert len(changes) == 7
    for identity, entry in after.items():
        old = before[identity]
        assert (entry['source_id'], entry['exercise_key'], entry['eligibility'], entry['raw_group']) == (
            old['source_id'], old['exercise_key'], old['eligibility'], old['raw_group'])
        if identity in changes:
            c = changes[identity]
            assert entry['row_sha256'] == c['after_sha256']
            assert old['row_sha256'] == c['before_sha256']
            assert {**entry['raw'], **c['before']} == old['raw']
            assert {k for k in entry['raw'] if entry['raw'][k] != old['raw'][k]} == set(c['after'])
        else:
            assert entry['raw'] == old['raw']
    index = LibraryCache().get()
    for identity, review in index.equipment_reviews.items():
        assert review['row_sha256'] == after[identity]['row_sha256']
        assert index.by_id[identity]['summary']['prescription_allowed'] is False


@pytest.mark.parametrize('change', CHANGES, ids=lambda c: c['name'])
@pytest.mark.parametrize('phase', ['GPP', 'SPP', 'TAPER'])
@pytest.mark.parametrize('days,fatigue,capacity', SCENARIOS)
def test_real_generator_phase_fatigue_capacity_and_fight_windows(monkeypatch, change, phase, days, fatigue, capacity):
    new = record(change['name'])
    old = {**new, **change['before']}
    f = flags(new['name'], EQUIPMENT)
    f.update(phase=phase, days_until_fight=days, fatigue=fatigue,
             training_frequency=capacity, days_available=capacity)
    before, after = run(monkeypatch, old, f), run(monkeypatch, new, f)
    a, b = candidate(before, old), candidate(after, new)
    # An unknown eccentric cost may only make late selection more conservative.
    if new['name'] == 'Slow-Lowered Pull-Up':
        assert not b or a
        assert after[1] <= before[1]
    else:
        assert bool(a) == bool(b)
        assert before[1] == after[1]
    if phase not in new['phases']:
        assert new['name'] not in after[1]
    if a and b:
        expected = -0.25 if 'mech_ballistic' in change['before'].get('tags', []) and days <= 21 else 0
        assert b['score'] - a['score'] == pytest.approx(expected)
        reasons = deepcopy(a['reasons'])
        if expected:
            assert 'late_strength_boost_crisp_ballistic' in reasons['reason_codes']
            reasons['reason_codes'].remove('late_strength_boost_crisp_ballistic')
            reasons['final_score'] -= 0.25
            reasons['late_window_adjustment'] -= 0.25
            # Both rows are authored lower_body: once the ballistic tag goes,
            # the region is the authored one, not the 'pull' in the name.
            reasons['base_categories'] = ['lower_body_loaded']
        _same_reasons(b['reasons'], reasons)
    if new['name'] in before[1] & after[1]:
        # Text contains the actual prescribed work, not a score-only helper.
        if 'system' in new:
            assert before[3][0] == after[3][0]
        else:
            assert before[3]['block'] == after[3]['block']


def _same_reasons(actual, expected):
    for key in expected:
        if isinstance(expected[key], float):
            assert actual[key] == pytest.approx(expected[key])
        else:
            assert actual[key] == expected[key]
    assert actual.keys() == expected.keys()
    return True


@pytest.mark.parametrize('change', CHANGES, ids=lambda c: c['name'])
def test_declared_clinical_restriction_cannot_be_bypassed(monkeypatch, change):
    row = record(change['name'])
    f = flags(row['name'], EQUIPMENT)
    f.update(days_until_fight=18 if 'Isometric' in row['name'] or 'Mid-Thigh' in row['name'] else 35,
             restrictions=[parse_restriction_entry('avoid ' + row['name'])])
    result = run(monkeypatch, row, f)
    assert row['name'] not in result[1]
    assert all(e['name'] != row['name'] for e in result[2])


@pytest.mark.parametrize('change', CHANGES, ids=lambda c: c['name'])
@pytest.mark.parametrize('cut_bucket', ['high', 'critical'])
def test_weight_cut_and_reduced_capacity_do_not_gain_permissions(monkeypatch, change, cut_bucket):
    row = record(change['name'])
    f = flags(row['name'], EQUIPMENT)
    f.update(days_until_fight=7, fatigue='high', training_frequency=1, days_available=1,
             cut_health_bucket=cut_bucket, cut_severity_bucket=cut_bucket)
    before = run(monkeypatch, {**row, **change['before']}, f)
    after = run(monkeypatch, row, f)
    assert after[1] <= before[1]
    if row.get('movement') == 'isometric':
        assert row['name'] not in after[1]
        assert candidate(after, row) is None


@pytest.mark.parametrize('name', ['Battle Ropes Tempo Burner', 'Battle Rope Gauntlet', 'Battle Rope Waves'])
def test_rope_corrections_remove_false_ankle_hits_but_keep_upper_body_risk(monkeypatch, name):
    row = record(name)
    c = next(c for c in CHANGES if c['name'] == name)
    assert injury_match_details({**row, **c['before']}, ['ankle sprain'])
    assert not injury_match_details(deepcopy(row), ['ankle sprain'])
    assert injury_match_details(deepcopy(row), ['shoulder dislocation'])
    assert injury_match_details(deepcopy(row), ['hand fracture'])
    assert not conditioning._conditioning_mechanical_cost_is_high(row)
    assert row['lactate_load'] == row['movement_cost'] == 'high'
    f = flags(name, EQUIPMENT)
    f['injuries'] = ['shoulder dislocation']
    assert name not in run(monkeypatch, row, f)[1]


def test_cognitive_mechanical_grip_and_goal_namespaces_remain_distinct():
    assert set(normalize_tags(['reaction', 'mech_reactive', 'grip', 'mech_grip_support'])) == {
        'reaction', 'mech_reactive', 'grip', 'mech_grip_support'}
    cognitive = {'name': 'Cue-response tracking', 'tags': ['reaction', 'coordination'], 'system': 'cognitive'}
    assert not injury_match_details(cognitive, ['ankle sprain'])
    rebound = record('Plyo Step-Up Intervals')
    assert 'mech_landing_impact' in rebound['tags']
    assert 'reaction' not in rebound['tags']
    assert conditioning._conditioning_mechanical_cost_is_high(rebound)
    assert injury_match_details(rebound, ['ankle sprain'])
    for c in CHANGES:
        new = record(c['name'])
        assert all(t in new['mechanical_risk_tags'] for t in new['tags'] if t.startswith('mech_'))
        assert 'reaction' not in new.get('goals', []) and 'mech_reactive' not in new.get('goals', [])
    index = LibraryCache().get()
    from api.services.training_library_vocabulary import CONCEPTS, supports
    support = {'raw': {'tags': ['mech_grip_support']}}
    # The library's existing reviewed contract, not a mechanical tag, owns goals.
    assert not supports(support, next(c for c in CONCEPTS if c.label == 'Grip work'))
    assert index.by_id[CHANGES[4]['catalogue_id']]['entry']['raw']['goals'] == ['power', 'maximal_strength_maintenance']


@pytest.mark.parametrize('change', CHANGES, ids=lambda c: c['name'])
@pytest.mark.parametrize('injury', ['ankle sprain', 'shoulder dislocation', 'hand fracture', 'lumbar strain'])
def test_real_injury_decisions_change_only_for_false_rope_ankle_risk(monkeypatch, change, injury):
    row = record(change['name'])
    old = {**row, **change['before']}
    before, after = (injury_match_details(deepcopy(r), [injury]) for r in (old, row))
    if injury == 'ankle sprain' and row['name'] in [c['name'] for c in CHANGES[:3]]:
        assert before and not after
    else:
        if row['name'] in [c['name'] for c in CHANGES[4:6]]:
            before = [{**r, 'tags': [t for t in r['tags'] if t != 'mech_ballistic']} for r in before]
        assert before == after
    if any(r['risk_level'] == 'exclude' for r in after):
        f = flags(row['name'], EQUIPMENT)
        f.update(injuries=[injury])
        if row.get('movement') == 'isometric':
            f['days_until_fight'] = 18
        assert row['name'] not in run(monkeypatch, row, f)[1]


def test_unrelated_edit_cannot_hide_in_mechanical_history():
    path = ROOT / 'data/conditioning_bank.json'
    rows = json.loads(path.read_bytes())
    next(r for r in rows if r['name'] == CHANGES[0]['name'])['duration'] = 'invented dose'
    with pytest.raises(ValueError, match='Mechanical source history drift'):
        reverse_mechanical_source('data/conditioning_bank.json', json.dumps(rows).encode())


def query_results(index):
    from api.services import training_library as library
    cases = json.loads((ROOT / 'tools/training_library_search_cases.json').read_bytes())['cases']
    return [{'query': case['query'], 'total': result['total'],
             'ordered_ids': [entry['catalogue_id'] for entry in result['entries']],
             'discovery_matches': [entry['discovery_match'] for entry in result['entries']]}
            for case in cases for result in [library.search(index, q=case['query'], limit=3000)]]


def test_actual_search_queries_and_all_facets_have_before_after_parity():
    from api.services import training_library as library
    current = LibraryCache().get()
    with mechanical_baseline_sources() as before_root:
        reviews = json.loads((before_root / 'shared/equipment-requirements.json').read_bytes())['reviews']
        projection = deepcopy(current.projection)
        before_entries = {e['catalogue_id']: e for e in build_catalogue(before_root / 'data')}
        mechanical_ids = {change['catalogue_id'] for change in CHANGES}
        for goal in projection['goal_reviews']:
            before_entry = before_entries[goal['catalogue_id']]
            after_entry = current.by_id[goal['catalogue_id']]['entry']
            if before_entry['row_sha256'] != goal['row_sha256']:
                assert goal['catalogue_id'] in mechanical_ids
                # Rebind the current discovery receipt only for this historical
                # comparison, proving adaptation/dose evidence did not change.
                for field in goal['evidence_fields']:
                    old, new = before_entry['raw'][field], after_entry['raw'][field]
                    if field == 'tags':
                        old = [v for v in old if not v.startswith('mech_')]
                        new = [v for v in new if not v.startswith('mech_')]
                    assert old == new
                goal['row_sha256'] = before_entry['row_sha256']
        with patch.object(library, 'load_projection', return_value=projection), patch.object(
            library, 'load_equipment_reviews', return_value={r['catalogue_id']: r for r in reviews}
        ):
            before = LibraryCache(before_root / 'data').get()
    assert query_results(before) == query_results(current)
    assert {r['entry']['catalogue_id']: r['facets'] for r in before.records} == {
        r['entry']['catalogue_id']: r['facets'] for r in current.records}


def test_complete_fixed_clock_main_plan_comparison(tmp_path):
    outputs = []
    with mechanical_baseline_sources() as before_root:
        for label, root in [('before', before_root), ('after', ROOT)]:
            path = tmp_path / (label + '.json')
            subprocess.run([sys.executable, str(ROOT / 'tools/canonical_metadata_plan_probe.py'),
                            str(ROOT / 'tools/canonical_metadata_baseline.json'), str(path)], cwd=root, check=True, capture_output=True,
                           env={**os.environ, 'PYTHONHASHSEED': '0', 'PYTHONUTF8': '1'})
            outputs.append(json.loads(path.read_bytes()))
    assert outputs[0] == outputs[1]
