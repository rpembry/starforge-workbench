"""Opt-in local instruction worker; never starts or resumes provider sessions."""
import argparse
import json
import os
from pathlib import Path
import stat
import time

import httpx

from workbench.client import client
from workbench.collector import _registration_state

from .opencode_delivery import OpenCodeDelivery
from .opencode_registration import resolve_opencode_registration


class WorkerConfigError(ValueError):
    pass


def load_config(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise WorkerConfigError('Worker configuration must be caller-owned and private')
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise WorkerConfigError('Invalid worker configuration') from exc
    expected = {'enabled', 'manifest', 'registration_state', 'launcher_state',
                'delivery_state', 'opencode_origin', 'provider_id', 'model_id', 'contexts'}
    if not isinstance(data, dict) or set(data) != expected or type(data['enabled']) is not bool:
        raise WorkerConfigError('Invalid worker configuration shape')
    if (not isinstance(data['contexts'], list) or not data['contexts'] or
        any(not isinstance(value, str) or not value for value in data['contexts'])):
        raise WorkerConfigError('Invalid worker context selection')
    for key in expected - {'enabled', 'contexts'}:
        if not isinstance(data[key], str) or not data[key]:
            raise WorkerConfigError('Invalid worker configuration value')
    return data


def _post(api, path, payload):
    try:
        return api.post(path, json=payload)
    except (httpx.HTTPError, OSError):
        return None


def _result_outcome(result):
    return {
        'received': ('received', 'provider_accepted'),
        'vanished': ('failed', 'session_missing'),
        'unavailable': ('retryable', 'provider_unavailable'),
        'uncertain': ('uncertain', 'delivery_ambiguous'),
    }[result.state]


def cycle(api, config, adapter, resolver=resolve_opencode_registration):
    """Sweep locally owned OpenCode registrations once, returning aggregate counts."""
    counts = {'eligible': 0, 'claimed': 0, 'reported': 0, 'responses_reported': 0,
              'ambiguous': 0}
    if not config['enabled']:
        return counts
    for evidence in adapter.pending_responses():
        report = _post(api, f'/api/instructions/{evidence.instruction_id}/results', {
            'lease_token': evidence.lease_token, 'outcome': evidence.outcome,
            'reason_code': evidence.reason})
        if report is not None and report.status_code == 200:
            adapter.mark_response(evidence.instruction_id, 'reported')
            counts['responses_reported'] += 1
        elif report is not None and report.status_code in (409, 422):
            # The server rejected the durable lease/state pairing. Repeating
            # cannot make that transition valid and must not loop forever.
            adapter.mark_response(evidence.instruction_id, 'unreportable')
            counts['ambiguous'] += 1
        else:
            counts['ambiguous'] += 1
    state_file = Path(config['registration_state'])
    if not state_file.is_file():
        return counts
    state = _registration_state(state_file)
    selected = tuple(config['contexts'])
    for item in state['contexts'].values():
        if item['provider'] != 'opencode':
            continue
        registered_id = item['id']
        try:
            exact = resolver(registered_id, config['manifest'], state_file,
                             config['launcher_state'], selected)
        except (OSError, ValueError, RuntimeError):
            continue
        if not exact:
            continue
        counts['eligible'] += 1
        response = _post(api, '/api/instructions/claim', {'registered_session_id': registered_id})
        if response is None or response.status_code in (404, 503):
            continue
        if response.status_code != 200:
            counts['ambiguous'] += 1
            continue
        try:
            claim = response.json()
            instruction_id = claim['id']
            token = claim['lease_token']
            text = claim['text']
            if (claim['state'] != 'claimed' or claim['registered_session_id'] != registered_id or
                not isinstance(instruction_id, str) or not isinstance(token, str) or
                not isinstance(text, str)):
                raise ValueError('Invalid claim envelope')
        except (ValueError, KeyError, TypeError):
            counts['ambiguous'] += 1
            continue
        counts['claimed'] += 1
        try:
            confirmed = resolver(registered_id, config['manifest'], state_file,
                                 config['launcher_state'], selected)
            if confirmed != exact:
                outcome, reason = 'failed', 'session_missing'
            else:
                renewed = _post(api, f'/api/instructions/{instruction_id}/renew',
                                {'lease_token': token})
                if renewed is None or renewed.status_code != 200:
                    # No transmission without a confirmed live lease.
                    counts['ambiguous'] += 1
                    continue
                result = adapter.deliver(instruction_id, exact, text, token)
                outcome, reason = _result_outcome(result)
        except (OSError, ValueError, RuntimeError, KeyError):
            # After a claim, unknown local failure must not become another POST.
            outcome, reason = 'uncertain', 'worker_interrupted'
        report = _post(api, f'/api/instructions/{instruction_id}/results', {
            'lease_token': token, 'outcome': outcome, 'reason_code': reason})
        if report is not None and report.status_code == 200:
            counts['reported'] += 1
        else:
            counts['ambiguous'] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--credentials-file', type=Path)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    while True:
        try:
            config = load_config(args.config)
            if config['enabled']:
                adapter = OpenCodeDelivery(config['opencode_origin'], config['delivery_state'],
                                           config['provider_id'], config['model_id'])
                with client(role='collector', credentials_file=args.credentials_file) as api:
                    counts = cycle(api, config, adapter)
            else:
                counts = {'eligible': 0, 'claimed': 0, 'reported': 0,
                          'responses_reported': 0, 'ambiguous': 0}
            if args.once or counts['claimed'] or counts['ambiguous']:
                print(json.dumps(counts, sort_keys=True), flush=True)
        except (OSError, ValueError, WorkerConfigError, httpx.HTTPError):
            # No paths, credentials, instruction text or provider output in logs.
            print('{"worker":"unavailable"}', flush=True)
        if args.once:
            break
        time.sleep(5)


if __name__ == '__main__':
    main()
