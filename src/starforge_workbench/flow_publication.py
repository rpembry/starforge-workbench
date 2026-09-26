"""Explicit, private FLOW publication outbox. Local Git remains authoritative."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .flow import _atomic_json, _private, _writer_lock, load_profile
from .flow_history import _item, _run, snapshot
from .flow_markdown import parse
from .flow import _find, _registry, resolve


def _path(profile: Path) -> Path:
    return profile.with_suffix('.publications.json')


def _load(profile: Path) -> dict:
    path = _path(profile)
    if not path.exists():
        return {'version': 1, 'operations': {}}
    _private(path, directory=False)
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('version') != 1 or not isinstance(state.get('operations'), dict):
        raise ValueError('Invalid FLOW publication outbox')
    return state


def queue(reference: str, *, profile=None, operation_id: str | None = None,
          expected_version: int | None = None) -> dict:
    """Capture one exact document revision; never contact the remote API."""
    path, config = load_profile(profile)
    snap = snapshot(reference, profile=path)
    document = parse(snap['document'])
    item = _find(_registry(path), resolve(reference, config)['canonical'])
    if not item or item['id'] != snap['work_item_id']:
        raise ValueError('FLOW identity changed during capture')
    if snap['revision'] == 'UNBORN':
        raise ValueError('Checkpoint the FLOW document before publication')
    _, _, relative = _item(Path(config['root']), path, config, reference)
    committed = _run(Path(config['root']), 'show', f'HEAD:{relative}', check=False)
    if committed.returncode or committed.stdout != snap['document']:
        raise ValueError('FLOW document has unpublished local edits; checkpoint before publication')
    if expected_version is not None and expected_version < 1:
        raise ValueError('Expected version must be positive')
    task_ids = [task.task_id for task in document.tasks if task.task_id]
    if len(task_ids) != len(set(task_ids)) or len(task_ids) > 100:
        raise ValueError('FLOW task IDs must be unique and limited to 100')
    operation_id = operation_id or 'publish-' + uuid4().hex
    payload = {'operation_id': operation_id, 'work_item_id': item['id'],
               'source_ref': item['source'], 'document_revision': snap['revision'],
               'document_hash': snap['document_hash'], 'task_ids': task_ids,
               'expected_version': expected_version}
    signature = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with _writer_lock(path):
        state = _load(path)
        old = state['operations'].get(operation_id)
        if old:
            if old['signature'] != signature:
                raise ValueError('Publication operation ID was used for different content')
            return {'operation_id': operation_id, 'status': old['status'], 'payload': old['payload']}
        state['operations'][operation_id] = {'signature': signature, 'payload': payload,
                                              'status': 'publication_pending'}
        _atomic_json(_path(path), state)
    return {'operation_id': operation_id, 'status': 'publication_pending', 'payload': payload}


def replay(operation_id: str, transport, *, profile=None) -> dict:
    """Transport exposes get(work_item_id) and publish(payload); retries retain the same ID.

    A 404 from get means there is no projection. Network failures leave the outbox
    pending; version conflicts are explicit reconciliation work, never blind retries.
    """
    path, _ = load_profile(profile)
    with _writer_lock(path):
        state = _load(path)
        entry = state['operations'].get(operation_id)
        if not entry:
            raise ValueError('Unknown FLOW publication operation')
        if entry['status'] == 'published':
            return {'operation_id': operation_id, 'status': 'published', 'response': entry['response']}
        if entry['status'] == 'reconciliation_needed':
            return {'operation_id': operation_id, 'status': 'reconciliation_needed',
                    'reason': entry['reason']}
        payload = entry['payload']
        try:
            current = transport.get(payload['work_item_id'])
            actual = current['version'] if current is not None else None
            # A lost response may follow a successful transaction. Retry the exact
            # operation ID against the server's idempotency ledger in that case.
            same_document = (current is not None and
                             current['document_revision'] == payload['document_revision'] and
                             current['document_hash'] == payload['document_hash'] and
                             current['source_ref'] == payload['source_ref'])
            if actual != payload['expected_version'] and not same_document:
                entry.update(status='reconciliation_needed', reason='version_conflict')
            elif current and current['source_ref'] != payload['source_ref']:
                entry.update(status='reconciliation_needed', reason='identity_conflict')
            else:
                response = transport.publish(payload)
                if response['projection']['document_revision'] != payload['document_revision'] or response['projection']['document_hash'] != payload['document_hash']:
                    entry.update(status='reconciliation_needed', reason='unexpected_response')
                else:
                    entry.update(status='published', response=response)
        except Exception as exc:
            # The request might have committed before the response was lost. Recheck
            # on replay; never generate a new operation ID for an ambiguous outcome.
            if getattr(exc, 'status_code', None) == 409:
                entry.update(status='reconciliation_needed', reason='server_conflict')
            else:
                return {'operation_id': operation_id, 'status': 'publication_pending',
                        'reason': 'api_unavailable_or_outcome_unknown'}
        _atomic_json(_path(path), state)
        return {'operation_id': operation_id, 'status': entry['status'],
                'response': entry.get('response'), 'reason': entry.get('reason')}


class APITransport:
    """Caller-supplied endpoint and bearer token; neither is stored in the outbox."""
    def __init__(self, base_url: str, token: str):
        from urllib.parse import urlsplit
        parts = urlsplit(base_url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            raise ValueError('FLOW publication needs a plain HTTPS API origin')
        if not token:
            raise ValueError('FLOW publication needs an operator token')
        self.base_url = base_url.rstrip('/')
        self.token = token

    def _request(self, method: str, path: str, payload=None):
        import httpx
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.request(method, self.base_url + path,
                                      headers={'Authorization': 'Bearer ' + self.token}, json=payload)
        if response.status_code == 404 and method == 'GET':
            return None
        response.raise_for_status()
        return response.json()

    def get(self, work_item_id: str):
        return self._request('GET', '/api/flow/work-items/' + work_item_id)

    def publish(self, payload: dict):
        return self._request('POST', '/api/flow/work-items', payload)


def main(argv=None) -> None:
    import argparse
    import os
    parser = argparse.ArgumentParser(description='Explicit FLOW projection publication')
    parser.add_argument('--profile')
    sub = parser.add_subparsers(dest='command', required=True)
    capture = sub.add_parser('queue')
    capture.add_argument('reference')
    capture.add_argument('--operation-id')
    capture.add_argument('--expected-version', type=int)
    send = sub.add_parser('replay')
    send.add_argument('operation_id')
    send.add_argument('--api-url', required=True)
    args = parser.parse_args(argv)
    if args.command == 'queue':
        result = queue(args.reference, profile=args.profile,
                       operation_id=args.operation_id, expected_version=args.expected_version)
    else:
        result = replay(args.operation_id, APITransport(args.api_url, os.environ.get('WB_API_TOKEN', '')),
                        profile=args.profile)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
