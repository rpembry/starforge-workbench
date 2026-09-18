"""Read-only launcher collector. Never reads transcripts, starts or controls agents."""
import argparse
import hashlib
import json
import os
import secrets
import socket
import stat
import subprocess
import time
import uuid

import httpx
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .client import client


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def processes(proc_root=Path('/proc')):
    result = {}
    for path in proc_root.iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
            result[int(path.name)] = dict(parent=int(fields[1]), birth=int(fields[19]), name=(path/'comm').read_text().strip())
        except (OSError, ValueError, IndexError):
            continue
    return result


class ScanUnavailable(RuntimeError):
    pass


def configured_contexts(manifest, selected=()):
    contexts = yaml.safe_load(Path(manifest).read_text())['contexts']
    return {c['id']: c for c in contexts
            if c.get('enabled') is True and (not selected or c['id'] in selected)}


def collect(manifest, selected=(), proc_root=Path('/proc')):
    contexts = configured_contexts(manifest, selected)
    result = subprocess.run(['tmux', '-L', 'starforge-ai-workbench', 'list-panes', '-a', '-F',
                             '#{session_name}|#{pane_pid}|#{pane_dead}|#{window_activity}'], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ScanUnavailable('Workbench tmux server unavailable; no run heartbeats submitted')
    boot = (proc_root/'sys/kernel/random/boot_id').read_text().strip()
    btime = int(next(line.split()[1] for line in (proc_root/'stat').read_text().splitlines() if line.startswith('btime ')))
    ticks = os.sysconf('SC_CLK_TCK')
    procs = processes(proc_root)
    runs = []
    for line in result.stdout.splitlines():
        session, pane_pid, dead, activity = line.split('|')
        identity = session.removeprefix('sfwb-')
        if identity not in contexts or dead == '1':
            continue
        c = contexts[identity]
        family = {int(pane_pid)}
        for _ in range(32):
            new = family | {pid for pid, item in procs.items() if item['parent'] in family}
            if new == family:
                break
            family = new
        expected = {'codex': 'codex', 'claude': 'claude', 'antigravity': 'agy', 'ollama': 'ollama', 'opencode': 'opencode'}[c['provider']]
        matches = [(pid, procs[pid]) for pid in family if pid in procs and procs[pid]['name'] == expected]
        if not matches:
            continue  # A live launcher menu alone is not a running provider.
        pid, process = min(matches, key=lambda p: p[1]['birth'])
        source_id = hashlib.sha256(f'{boot}:{identity}:{pid}:{process["birth"]}'.encode()).hexdigest()
        activity_time = int(activity) if activity.isdigit() and int(activity) > 0 else None
        runs.append(dict(source=socket.gethostname()+':tmux', source_id=source_id,
            context=identity, provider=c['provider'], actor=c['provider'], status='running',
            started_at=timestamp(btime+process['birth']/ticks),
            last_activity_at=timestamp(activity_time) if activity_time else None,
            activity_basis='tmux terminal activity; provider process present, task progress unknown'))
    return runs


def submit(api, runs):
    submitted = []
    for run in runs:
        response = api.post('/api/runs', json=run)
        response.raise_for_status()
        submitted.append(response.json())
    return submitted


def _private_file(path):
    try:
        item = path.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISREG(item.st_mode) and item.st_uid == os.getuid()
            and not item.st_mode & 0o077)


def _private_directory(path):
    try:
        item = path.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISDIR(item.st_mode) and item.st_uid == os.getuid()
            and not item.st_mode & 0o077)


def _binding_record(context, launcher_state):
    """Read one exact protected binding for local use only."""
    sessions = launcher_state/'sessions'
    path = sessions/(context['id']+'.json')
    if not _private_directory(launcher_state) or not _private_directory(sessions) or not _private_file(path):
        return None
    try:
        binding = json.loads(path.read_text())
        expected_cwd = str(Path(os.path.expandvars(context['cwd'])).expanduser().resolve())
        if (not isinstance(binding, dict) or not isinstance(binding.get('id'), str)
                or not binding['id'] or len(binding['id']) > 200
                or binding.get('provider') != context['provider']
                or binding.get('cwd') != expected_cwd):
            return None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    value = json.dumps({'id': binding['id'], 'provider': binding['provider'],
                        'cwd': binding['cwd']}, sort_keys=True, separators=(',', ':'))
    return {'id': binding['id'], 'generation': hashlib.sha256(value.encode()).hexdigest()}


def _binding_generation(context, launcher_state):
    """Hash an exact protected binding without publishing its provider identity."""
    record = _binding_record(context, launcher_state)
    return record['generation'] if record else None


def _registration_state(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.stat()
    if path.parent.is_symlink() or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError('Registration state directory must be owned by you with mode 0700')
    if path.is_symlink():
        raise ValueError('Registration state file must not be a symlink')
    if not path.exists():
        return {'version': 1, 'contexts': {}}
    if not _private_file(path):
        raise ValueError('Registration state file must be owned by you with mode 0600')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('contexts'), dict):
        raise ValueError('Invalid registration state')
    for context, item in value['contexts'].items():
        if (not isinstance(context, str) or not isinstance(item, dict)
                or set(item) != {'id', 'generation', 'sequence', 'host', 'display_name',
                                 'provider', 'run_id', 'action_id', 'last_activity_at'}
                or not isinstance(item['id'], str) or len(item['id']) < 16
                or not isinstance(item['generation'], str) or len(item['generation']) != 64
                or not isinstance(item['sequence'], int) or item['sequence'] < 0
                or not all(isinstance(item[key], str) and item[key]
                           for key in ('host', 'display_name', 'provider', 'run_id'))
                or item['action_id'] is not None and not isinstance(item['action_id'], str)
                or item['last_activity_at'] is not None and not isinstance(item['last_activity_at'], str)):
            raise ValueError('Invalid registration state')
    return value


def _save_registration_state(path, value):
    path = Path(path)
    temporary = path.with_name('.'+uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(value, output, sort_keys=True, separators=(',', ':'))
            output.write('\n')
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def publish_registered_sessions(api, manifest, selected, submitted_runs, collector_source,
                                registration_state, launcher_state):
    """Publish bounded metadata for live configured contexts; never control them."""
    contexts = configured_contexts(manifest, selected)
    state = _registration_state(registration_state)
    published = 0
    for run in submitted_runs:
        context = contexts.get(run['context'])
        if context is None:
            continue
        if run.get('provider') != context['provider']:
            raise ValueError('Submitted run provider does not match configured context')
        binding = _binding_generation(context, Path(launcher_state))
        host = socket.gethostname()
        generation = hashlib.sha256(
            json.dumps({'run': run['source_id'], 'binding': binding or 'exact-binding-missing',
                        'host': host, 'display_name': context['title'],
                        'provider': context['provider']}, sort_keys=True).encode()
        ).hexdigest()
        item = state['contexts'].get(context['id'])
        if item is not None and item['generation'] != generation:
            replaced = {
                'id': item['id'],
                'collector_source': collector_source,
                'host': item['host'],
                'display_name': item['display_name'],
                'provider': item['provider'],
                'run_id': item['run_id'],
                'action_id': item['action_id'],
                'evidence_state': 'stopped',
                'reason': 'registration_replaced',
                'summary': '',
                'observation_sequence': item['sequence']+1,
                'observed_at': datetime.now(timezone.utc).isoformat(),
                'last_activity_at': item['last_activity_at'],
            }
            response = api.post('/api/registered-sessions', json=replaced)
            response.raise_for_status()
            item['sequence'] = replaced['observation_sequence']
            _save_registration_state(registration_state, state)
            item = None
        if item is None:
            item = {'id': 'registered_'+secrets.token_urlsafe(24),
                    'generation': generation, 'sequence': 0, 'host': host,
                    'display_name': context['title'], 'provider': context['provider'],
                    'run_id': run['id'], 'action_id': run.get('action_id'),
                    'last_activity_at': run.get('last_activity_at')}
            state['contexts'][context['id']] = item
        payload = {
            'id': item['id'],
            'collector_source': collector_source,
            'host': host,
            'display_name': context['title'],
            'provider': context['provider'],
            'run_id': run['id'],
            'action_id': run.get('action_id'),
            'evidence_state': 'present' if binding else 'unknown',
            'reason': 'process_observed' if binding else 'exact_binding_missing',
            'summary': '',
            'observation_sequence': item['sequence']+1,
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'last_activity_at': run.get('last_activity_at'),
        }
        response = api.post('/api/registered-sessions', json=payload)
        response.raise_for_status()
        item['sequence'] = payload['observation_sequence']
        item.update(run_id=run['id'], action_id=run.get('action_id'),
                    last_activity_at=run.get('last_activity_at'))
        _save_registration_state(registration_state, state)
        published += 1
    return published


def cycle(api, manifest, selected, instance_id, source=None, registration_state=None,
          launcher_state=None):
    health = dict(source=source or socket.gethostname()+':launcher', instance_id=instance_id,
                  scope=','.join(selected) or 'all configured contexts', status='ok', reason='scan_complete', observed_runs=0)
    try:
        runs = collect(manifest, selected)
    except ScanUnavailable:
        health.update(status='degraded', reason='tmux_unavailable')
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, subprocess.SubprocessError):
        health.update(status='degraded', reason='scan_failed')
    else:
        try:
            submitted_runs = submit(api, runs)
            health['observed_runs'] = len(submitted_runs)
        except httpx.HTTPError:
            health.update(status='degraded', reason='submission_failed')
    response = api.post('/api/collectors/heartbeat', json=health)
    response.raise_for_status()
    if health['status'] == 'ok' and registration_state is not None:
        try:
            publish_registered_sessions(api, manifest, selected, submitted_runs, health['source'],
                                        registration_state, launcher_state)
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            health.update(status='degraded', reason='submission_failed')
            response = api.post('/api/collectors/heartbeat', json=health)
            response.raise_for_status()
    return health


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--context', action='append', default=[])
    p.add_argument('--url', default=os.environ.get('WB_API_URL'))
    p.add_argument('--credentials-file', default=os.environ.get('WB_CREDENTIALS_FILE'))
    p.add_argument('--source', help='stable collector source; use a distinct value for each credential owner')
    p.add_argument('--registration-state', type=Path, default=os.environ.get('WB_REGISTERED_SESSION_STATE'),
                   help='protected local opaque-ID state; omitted disables registered-session publishing')
    p.add_argument('--launcher-state', type=Path,
                   default=Path(os.environ.get('WB_LAUNCHER_STATE', '~/.local/state/starforge-ai-workbench')).expanduser(),
                   help='protected launcher binding root (read only)')
    p.add_argument('--interval', type=int, default=0, help='0 submits once; otherwise seconds between heartbeats (minimum 10)')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if args.interval and args.interval < 10:
        p.error('Minimum interval is 10 seconds')
    if args.source is not None and not args.source.strip():
        p.error('Collector source must not be empty')
    instance_id = str(uuid.uuid4())
    while True:
        if args.dry_run:
            print(json.dumps(collect(args.manifest, args.context), indent=2))
            return
        try:
            with client(args.url, args.credentials_file, 'collector') as api:
                result = cycle(api, args.manifest, args.context, instance_id,
                               source=args.source,
                               registration_state=args.registration_state,
                               launcher_state=args.launcher_state)
                print(json.dumps({'status': result['status'], 'reason': result['reason'], 'observed_runs': result['observed_runs']}), flush=True)
        except (httpx.HTTPError, OSError, ValueError):
            print(json.dumps({'status': 'unreachable', 'message': 'Heartbeat failed; retrying next interval'}), flush=True)
            if not args.interval:
                raise SystemExit(1) from None
        if not args.interval:
            return
        time.sleep(args.interval)
