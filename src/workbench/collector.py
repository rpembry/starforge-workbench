"""Read-only launcher collector. Never reads transcripts, starts or controls agents."""
import argparse
import hashlib
import json
import os
import socket
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


def collect(manifest, selected=(), proc_root=Path('/proc')):
    contexts = yaml.safe_load(Path(manifest).read_text())['contexts']
    contexts = {c['id']: c for c in contexts if not selected or c['id'] in selected}
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
    for run in runs:
        response = api.post('/api/runs', json=run)
        response.raise_for_status()
    return len(runs)


def cycle(api, manifest, selected, instance_id, source=None):
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
            health['observed_runs'] = submit(api, runs)
        except httpx.HTTPError:
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
    p.add_argument('--interval', type=int, default=0, help='0 submits once; otherwise seconds between heartbeats (minimum 10)')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if args.interval and args.interval < 10:
        p.error('Minimum interval is 10 seconds')
    instance_id = str(uuid.uuid4())
    while True:
        if args.dry_run:
            print(json.dumps(collect(args.manifest, args.context), indent=2))
            return
        try:
            with client(args.url, args.credentials_file, 'collector') as api:
                result = cycle(api, args.manifest, args.context, instance_id)
                print(json.dumps({'status': result['status'], 'reason': result['reason'], 'observed_runs': result['observed_runs']}), flush=True)
        except (httpx.HTTPError, OSError, ValueError):
            print(json.dumps({'status': 'unreachable', 'message': 'Heartbeat failed; retrying next interval'}), flush=True)
            if not args.interval:
                raise SystemExit(1) from None
        if not args.interval:
            return
        time.sleep(args.interval)
