"""Playback must consult the live training record before signing media."""
from unittest.mock import MagicMock

import pytest

from api.private_media_store import PrivateMediaStoreMixin


@pytest.mark.parametrize('available', [True, False, None])
def test_playback_revalidates_the_owned_record(available):
    store = PrivateMediaStoreMixin()
    store.client = MagicMock()
    row = {'id': 'attachment', 'athlete_id': 'owner', 'source': 'external',
           'target_id': 'session', 'exercise_ref': 'exercise'}
    query = store.client.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value
    query.execute.return_value.data = [row]
    store.client.rpc.return_value.execute.return_value.data = available
    assert store.get_training_attachment('owner', 'attachment') == (row if available is True else None)
    store.client.rpc.assert_called_once_with('training_record_available', {
        'p_athlete_id': 'owner', 'p_source': 'external', 'p_target_id': 'session', 'p_ref': 'exercise',
    })
