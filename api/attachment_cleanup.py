"""Run private attachment retention independently of upload traffic.

The compose service starts on deploy/reboot and retries every minute. A heartbeat
is written only after an entirely successful sweep; Docker health exposes errors
without logging credentials or response bodies.
"""
import argparse
import logging
import os
from pathlib import Path
import time

import httpx

logger = logging.getLogger(__name__)
HEARTBEAT = Path('/tmp/unlxck-attachment-cleanup-success')
INTERVAL_SECONDS = 60


def cleanup_once(client: httpx.Client) -> int:
    response = client.post('/rest/v1/rpc/attachment_cleanup_candidates', json={})
    response.raise_for_status()
    candidates = response.json()
    failures = 0
    for asset in candidates:
        try:
            paths = [('attachment-intake', asset['intake_path'])]
            if asset['status'] == 'rejected' and asset.get('ready_path'):
                paths.append(('attachment-ready', asset['ready_path']))
            for bucket, path in paths:
                removed = client.request('DELETE', f'/storage/v1/object/{bucket}', json={'prefixes': [path]})
                removed.raise_for_status()
            acknowledged = client.post('/rest/v1/rpc/ack_attachment_cleanup', json={
                'p_asset_id': asset['id'], 'p_all_removed': asset['status'] == 'rejected',
            })
            acknowledged.raise_for_status()
        except (httpx.HTTPError, ValueError, KeyError):
            failures += 1
            logger.error('Attachment cleanup will retry asset=%s', asset.get('id'))
    if failures:
        raise RuntimeError(f'{failures} attachment removals remain pending')
    return len(candidates)


def healthy() -> bool:
    try:
        return time.time() - HEARTBEAT.stat().st_mtime < INTERVAL_SECONDS * 5
    except FileNotFoundError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--health-check', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if args.health_check:
        return 0 if healthy() else 1
    url = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not url.startswith('https://') or not key:
        raise RuntimeError('Cleanup requires the server Supabase URL and service key')
    headers = {'apikey': key}
    if key.count('.') == 2:
        headers['Authorization'] = f'Bearer {key}'
    logging.basicConfig(level=logging.INFO)
    # Do not let HTTP client's INFO logging emit URLs; no response bodies or
    # request headers are included in this service's logs.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    with httpx.Client(base_url=url, headers=headers, timeout=15) as client:
        while True:
            try:
                count = cleanup_once(client)
                HEARTBEAT.touch()
                logger.info('Attachment cleanup sweep succeeded count=%s', count)
            except (httpx.HTTPError, RuntimeError, ValueError, KeyError):
                logger.error('Attachment cleanup sweep failed; retrying in %s seconds', INTERVAL_SECONDS)
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(INTERVAL_SECONDS)


if __name__ == '__main__':
    raise SystemExit(main())
