"""The prompt projection must retain every athlete field and precedence."""
import json
from copy import deepcopy

import pytest

from fightcamp.stage2_payload import build_stage2_handoff_text


def _section(handoff, heading):
    body = handoff.split(heading + '\n', 1)[1].split('\n\n---\n\n', 1)[0]
    return json.loads(body.removeprefix('```json\n').removesuffix('\n```'))


@pytest.mark.parametrize('brief', [None, {}, {'athlete_snapshot': {'sport': 'boxing', 'age': 30, 'custom_goal': 'balance', 'injuries_raw_text': 'knee pain'}}])
def test_profile_reconstructs_without_losing_personalisation(brief):
    payload = {'athlete_model': {'sport': 'mma', 'custom_goal': 'speed'}}
    original = deepcopy((payload, brief))
    handoff = build_stage2_handoff_text(stage2_payload=payload, planning_brief=brief, plan_text='draft')
    packet = _section(handoff, 'FINALIZER PACKET')
    additions = _section(handoff, 'ATHLETE PROFILE')
    profile = (brief or {}).get('athlete_snapshot') or payload['athlete_model']
    assert {**packet['athlete_model'], **additions} == profile
    assert not any(key in packet['athlete_model'] and value == packet['athlete_model'][key] for key, value in additions.items())
    assert (payload, brief) == original
