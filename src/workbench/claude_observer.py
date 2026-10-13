"""Observe local Claude record metadata, never raw content or inferred completion."""
import argparse
import fcntl
import hashlib
import json
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .client import client
from .codex_observer import atomic_state, observe


def event_from_record(record,path,cutoff,known):
    if 'subagents' in path.parts or not isinstance(record,dict) or record.get('type')!='assistant':
        return None
    if not all(isinstance(record.get(k),str) for k in ['timestamp','sessionId','uuid']):
        raise ValueError('Claude identity/timestamp missing')
    stamp=datetime.fromisoformat(record['timestamp'].replace('Z','+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Claude timestamp requires timezone')
    if stamp<=cutoff:
        return None
    session=str(uuid.UUID(record['sessionId']))
    record_id=str(uuid.UUID(record['uuid']))
    message=record.get('message') or {}
    if not isinstance(message,dict):
        raise ValueError('Invalid Claude message shape')
    content=message.get('content') or []
    if not isinstance(content,list):
        raise ValueError('Invalid Claude content shape')
    kinds=sorted({b.get('type') if b.get('type') in {'text','tool_use','thinking'} else 'other' for b in content if isinstance(b,dict)})
    raw_hash=hashlib.sha256(json.dumps(record,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    identity=hashlib.sha256(f'claude:{session}:{record_id}:{raw_hash}'.encode()).hexdigest()
    return dict(kind='observation',summary='Claude recorded '+('/'.join(kinds) or 'assistant')+' activity',
                details=json.dumps(dict(provider='claude',session_id=session,record_id=record_id,record_sha256=raw_hash,
                    activity_types=kinds,basis='Local assistant record; not proof of current process presence, task progress, or completion. Content remains local.'),sort_keys=True),
                source=socket.gethostname()+':claude-observer',source_id=identity,occurred_at=stamp.isoformat())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--sessions',type=Path,default=Path.home()/'.claude/projects')
    p.add_argument('--credentials-file',type=Path,required=True)
    args=p.parse_args()
    args.state.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if args.state.is_symlink() or args.state.parent.stat().st_mode & 0o077:
        raise ValueError('Claude observer state must be private')
    lock=args.state.with_suffix('.lock').open('w');os.chmod(lock.name,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state=json.loads(args.state.read_text()) if args.state.exists() else {}
    # Persist once, so restart never moves the initial observation boundary.
    cutoff=state.setdefault('_cutoff',(datetime.now(timezone.utc)-timedelta(days=1)).isoformat())
    atomic_state(args.state,state)
    instance=str(uuid.uuid4())
    while True:
        try:
            with client(credentials_file=args.credentials_file,role='collector') as api:
                health=dict(source=socket.gethostname()+':claude-observer',instance_id=instance,scope='Claude top-level local assistant records; last 24h at bootstrap',status='ok',reason='scan_complete',observed_runs=0)
                try:
                    counts=observe(api,args.sessions,state,cutoff,set(),parser=event_from_record)
                    if counts['malformed'] or counts['failed_files']:
                        health.update(status='degraded',reason='scan_failed')
                    print(json.dumps(counts),flush=True)
                except (OSError,ValueError,TypeError):
                    health.update(status='degraded',reason='scan_failed')
                finally:
                    atomic_state(args.state,state)
                r=api.post('/api/collectors/heartbeat',json=health);r.raise_for_status()
        except (OSError,ValueError,httpx.HTTPError):
            print('{"status":"unreachable","reason":"retry_pending"}',flush=True)
        time.sleep(30)


if __name__=='__main__':
    main()
