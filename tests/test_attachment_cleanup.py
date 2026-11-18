"""The retention timer must delete through Storage and retry without forgetting rows."""
import json

import httpx
import pytest

from api.attachment_cleanup import cleanup_once


@pytest.mark.parametrize('failed_path', ['attachment-intake', 'attachment-ready', 'ack_attachment_cleanup'])
def test_failed_cleanup_retries_before_acknowledging(failed_path):
    calls = []
    failing = True

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith('attachment_cleanup_candidates'):
            return httpx.Response(200, json=[{'id': 'asset', 'status': 'rejected',
                                             'intake_path': 'source.jpg', 'ready_path': 'clean.jpg'}])
        if failing and request.url.path.endswith(failed_path):
            return httpx.Response(503)
        return httpx.Response(200, json=None)

    with httpx.Client(base_url='https://storage.example', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match='remain pending'):
            cleanup_once(client)
        if failed_path != 'ack_attachment_cleanup':
            assert not any(path.endswith('ack_attachment_cleanup') for _, path in calls)
        calls.clear()
        failing = False
        assert cleanup_once(client) == 1
        assert calls == [('POST', '/rest/v1/rpc/attachment_cleanup_candidates'),
                         ('DELETE', '/storage/v1/object/attachment-intake'),
                         ('DELETE', '/storage/v1/object/attachment-ready'),
                         ('POST', '/rest/v1/rpc/ack_attachment_cleanup')]


def test_ready_asset_cleanup_never_deletes_the_approved_file():
    def handler(request):
        if request.url.path.endswith('attachment_cleanup_candidates'):
            return httpx.Response(200, json=[{'id': 'asset', 'status': 'ready',
                                             'intake_path': 'source.jpg', 'ready_path': 'clean.jpg'}])
        assert 'attachment-ready' not in request.url.path
        if request.url.path.endswith('ack_attachment_cleanup'):
            assert json.loads(request.content)['p_all_removed'] is False
        return httpx.Response(200, json=None)
    with httpx.Client(base_url='https://storage.example', transport=httpx.MockTransport(handler)) as client:
        assert cleanup_once(client) == 1


def test_health_expires_without_a_successful_sweep(tmp_path, monkeypatch):
    from api import attachment_cleanup
    heartbeat = tmp_path / 'heartbeat'
    monkeypatch.setattr(attachment_cleanup, 'HEARTBEAT', heartbeat)
    assert not attachment_cleanup.healthy()
    heartbeat.touch()
    assert attachment_cleanup.healthy()
    monkeypatch.setattr(attachment_cleanup.time, 'time', lambda: heartbeat.stat().st_mtime + 301)
    assert not attachment_cleanup.healthy()
