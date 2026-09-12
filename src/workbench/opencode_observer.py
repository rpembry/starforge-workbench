"""Read completed OpenCode metadata and optional minimized lifecycle hooks."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import httpx
from .client import client
from .codex_observer import atomic_state


ATTENTION_FIELDS = {
    'generation': {'kind', 'provider', 'session_id', 'generation_id', 'source', 'source_instance',
                   'started_at', 'provenance'},
    'observation': {'kind', 'provider', 'session_id', 'generation_id', 'source', 'source_instance',
                     'incident_id', 'sequence', 'observed_at', 'reason', 'state', 'provenance'},
}
IDENTITY = re.compile(r'[A-Za-z0-9_.:-]{1,200}')


def scan(api, database, state):
    counts = dict(submitted=0, malformed=0, failed=0)
    seen = state.setdefault('acknowledged', [])
    known = set(seen)
    # mode=ro does not create a missing database; no immutable flag, so WAL is visible.
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=2) as db:
        rows = db.execute('''select m.id,m.session_id,m.data from message m join session s
            on m.session_id=s.id where s.parent_id is null and m.time_created>=?
            order by m.time_created,m.id''', (state['cutoff_ms'],))
        for message_id, session_id, raw in rows:
            source_id = hashlib.sha256(f'opencode:{session_id}:{message_id}'.encode()).hexdigest()
            if source_id in known:
                continue
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError('message shape')
                if message.get('role') != 'assistant':
                    continue
                stamp = message.get('time')
                if not isinstance(stamp,dict):
                    raise ValueError('time shape')
                completed = stamp.get('completed')
                if completed is None:
                    continue  # Streaming records are revisited until completed.
                if isinstance(completed,bool) or not isinstance(completed,(int,float)):
                    raise ValueError('timestamp')
                occurred = datetime.fromtimestamp(completed/1000,timezone.utc).isoformat()
                for identity in (message_id,session_id):
                    if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',identity):
                        raise ValueError('identity')
                details = dict(provider='opencode',session_id=session_id,message_id=message_id,
                    basis='Completed local assistant record; not proof of task completion or current process presence. Content stays local.')
                for field in ('providerID','modelID'):
                    value=message.get(field)
                    if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_./:-]{1,100}',value):
                        details[field]=value
            except (ValueError,TypeError,OverflowError,OSError):
                counts['malformed']+=1
                # Hash-only diagnostics remain local; bad records are retried, not discarded.
                state.setdefault('diagnostics',{})[source_id]='invalid_metadata'
                continue
            try:
                response=api.post('/api/events',json=dict(kind='observation',
                    summary='OpenCode recorded assistant activity',details=json.dumps(details,sort_keys=True),
                    source=socket.gethostname()+':opencode-observer',source_id=source_id,occurred_at=occurred))
                response.raise_for_status()
            except httpx.HTTPError:
                counts['failed']+=1
                continue
            seen.append(source_id);known.add(source_id)
            state.setdefault('diagnostics',{}).pop(source_id,None)
            counts['submitted']+=1
    return counts


def scan_attention(api, queue, state):
    """Forward allowlisted plugin records in order; never read provider content."""
    counts = dict(submitted=0, malformed=0, rejected=0, failed=0)
    offset = state.get('attention_offset', 0)
    with queue.open('rb') as source:
        file_stat = os.fstat(source.fileno())
        identity = {'device': file_stat.st_dev, 'inode': file_stat.st_ino}
        source.seek(0, os.SEEK_END)
        if state.get('attention_file') != identity or offset > source.tell():
            offset = 0  # Replacement or truncation replays through server generation checks.
        state['attention_file'] = identity
        state['attention_offset'] = offset
        source.seek(offset)
        while True:
            start = source.tell()
            raw = source.readline()
            if not raw or not raw.endswith(b'\n'):
                break
            end = source.tell()
            try:
                record = json.loads(raw)
                kind = record.get('kind') if isinstance(record, dict) else None
                if kind not in ATTENTION_FIELDS or set(record) != ATTENTION_FIELDS[kind]:
                    raise ValueError('shape')
                if record['provider'] != 'opencode':
                    raise ValueError('provider')
                for field in ('session_id', 'generation_id', 'source_instance'):
                    if not isinstance(record[field], str) or not IDENTITY.fullmatch(record[field]):
                        raise ValueError('identity')
                if kind == 'observation' and (not isinstance(record['incident_id'], str) or
                                              not re.fullmatch(r'[a-f0-9]{64}', record['incident_id'])):
                    raise ValueError('incident')
                if record['source'] != 'opencode-plugin':
                    raise ValueError('source')
                endpoint = ('/api/provider-attention/generations' if kind == 'generation'
                            else '/api/provider-attention/observations')
                body = {key: value for key, value in record.items() if key != 'kind'}
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                counts['malformed'] += 1
                state.setdefault('attention_diagnostics', {})[hashlib.sha256(raw).hexdigest()] = 'invalid_metadata'
                state['attention_offset'] = end
                continue
            try:
                response = api.post(endpoint, json=body)
                if response.status_code == 409 and response.json().get('error', {}).get('code') in {
                    'duplicate_observation', 'out_of_order_observation', 'stale_generation',
                    'generation_conflict'}:
                    counts['rejected'] += 1
                else:
                    response.raise_for_status()
                    counts['submitted'] += 1
            except (ValueError, httpx.HTTPError):
                counts['failed'] += 1
                source.seek(start)
                break
            state['attention_offset'] = end
    return counts


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,default=Path.home()/'.local/share/opencode/opencode.db')
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--credentials-file',type=Path,required=True)
    p.add_argument('--attention-events',type=Path,default=os.environ.get('WB_OPENCODE_ATTENTION_EVENTS'))
    args=p.parse_args()
    args.state.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if args.state.is_symlink() or args.state.parent.stat().st_mode & 0o077:
        raise ValueError('Observer state must be private')
    lock=args.state.with_suffix('.lock')
    fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state=json.loads(args.state.read_text()) if args.state.exists() else {}
    state.setdefault('cutoff_ms',int(time.time()*1000))
    atomic_state(args.state,state)
    instance=str(uuid.uuid4())
    while True:
        try:
            with client(credentials_file=args.credentials_file,role='collector') as api:
                health=dict(source=socket.gethostname()+':opencode-observer',instance_id=instance,
                    scope='OpenCode top-level assistant records created since observer installation',
                    status='ok',reason='scan_complete',observed_runs=0)
                try:
                    counts=scan(api,args.database,state)
                    if args.attention_events:
                        queue_stat = args.attention_events.stat()
                        parent_stat = args.attention_events.parent.stat()
                        if (args.attention_events.is_symlink() or queue_stat.st_uid != os.getuid() or
                            queue_stat.st_mode & 0o077 or parent_stat.st_uid != os.getuid() or
                            parent_stat.st_mode & 0o077):
                            raise ValueError('Attention event queue must be private')
                        counts['attention'] = scan_attention(api,args.attention_events,state)
                    attention = counts.get('attention', {})
                    if counts['malformed'] or counts['failed'] or attention.get('malformed') or attention.get('failed'):
                        health.update(status='degraded',reason='scan_failed')
                    print(json.dumps(counts),flush=True)
                except (OSError,ValueError,TypeError,sqlite3.Error):
                    health.update(status='degraded',reason='database_unavailable')
                atomic_state(args.state,state)
                api.post('/api/collectors/heartbeat',json=health).raise_for_status()
        except (OSError,ValueError,httpx.HTTPError):
            print('{"status":"unreachable","reason":"retry_pending"}',flush=True)
        time.sleep(30)


if __name__=='__main__':
    main()
