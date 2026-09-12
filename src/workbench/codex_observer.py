"""Cursor-based local final-answer observer; never uploads raw transcripts."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

from .client import client
from .worklog_import import sensitive

ACCOMPLISHMENT = re.compile(r'^(done|created|updated|fixed|installed|verified|removed|added|cleaned|merged|pushed|configured|implemented|addressed)\b', re.I)
MAX_LINE = 16 * 1024 * 1024


def atomic_state(path, data):
    temp=path.with_suffix('.next')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(data,f);f.flush();os.fsync(f.fileno())
    temp.replace(path)


def bootstrap(snapshot):
    with sqlite3.connect(Path(snapshot).resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as db:
        row=db.execute("SELECT value FROM import_state WHERE key='codex_last_final_answer_ts'").fetchone()
        if not row:
            raise ValueError('Legacy snapshot needs a verified collector watermark')
        hashes={hashlib.sha256(source.encode()).hexdigest() for (source,) in db.execute("SELECT source FROM events WHERE source LIKE 'codex:%'")}
    return row[0],hashes


def record_problem(state, path, cursor, reason, line=b''):
    # Keep a replayable local reference, never the response text or exception value.
    identity=f'{path}:{cursor["identity"]}:{cursor["offset"]}:{reason}'
    key=hashlib.sha256(identity.encode()).hexdigest()
    state.setdefault('_problems',{})[key]=dict(path=str(path),identity=cursor['identity'],
        offset=cursor['offset'],reason=reason,sha256=hashlib.sha256(line).hexdigest())


def observe(api, sessions, state, cutoff, known, parser=None):
    cutoff_time=datetime.fromisoformat(cutoff.replace('Z','+00:00'))
    counts={'submitted':0,'withheld':0,'malformed':0,'failed_files':0}
    if not sessions.is_dir():
        raise OSError('Session directory unavailable')
    for path in sorted(sessions.rglob('*.jsonl')):
        key=hashlib.sha256(str(path).encode()).hexdigest()
        try:
            observe_file(api,path,state,key,cutoff_time,known,counts,parser or event_from_record)
        except (OSError,ValueError,TypeError,httpx.HTTPError) as exc:
            # Do not advance a failed delivery or unreadable file. Other files still run.
            reason='delivery_failed' if isinstance(exc,httpx.HTTPError) else 'file_scan_failed'
            state.setdefault('_file_errors',{})[key]={'path':str(path),'reason':reason}
            counts['failed_files']+=1
        else:
            state.get('_file_errors',{}).pop(key,None)
    counts['malformed']=len(state.get('_problems',{}))
    return counts


def observe_file(api,path,state,key,cutoff_time,known,counts,parser):
    if path.is_symlink() or path.stat().st_uid!=os.getuid():
        return
    stat=path.stat()
    identity=f'{stat.st_dev}:{stat.st_ino}'
    cursor=state.setdefault(key,{'identity':identity,'offset':0})
    if cursor['identity']!=identity or stat.st_size<cursor['offset']:
        cursor.update(identity=identity,offset=0)
    if stat.st_mtime<cutoff_time.timestamp() and cursor['offset']==0:
        cursor['offset']=stat.st_size
        return
    with path.open('rb') as stream:
        stream.seek(cursor['offset'])
        while True:
            line=stream.readline(MAX_LINE+1)
            if not line:
                break
            if len(line)>MAX_LINE:
                record_problem(state,path,cursor,'oversize_record',line)
                break  # Retain this file's cursor; unrelated files still proceed.
            if not line.endswith(b'\n'):
                break
            try:
                record=json.loads(line)
                event=parser(record,path,cutoff_time,known)
            except (ValueError,TypeError,KeyError,IndexError,OverflowError):
                record_problem(state,path,cursor,'malformed_record',line)
                cursor['offset']=stream.tell()
                continue
            if event:
                if event.pop('_withheld',False):
                    counts['withheld']+=1
                response=api.post('/api/events',json=event)
                response.raise_for_status()
                counts['submitted']+=1
                state.setdefault('_references',{})[event['source_id']]={'path':str(path),'timestamp':record['timestamp'],'offset':cursor['offset']}
            cursor['offset']=stream.tell()  # Only after successful delivery or recorded quarantine.


def event_from_record(record,path,cutoff,known):
    if not isinstance(record,dict) or record.get('type')!='response_item':
        return None
    payload=record.get('payload') or {}
    if not isinstance(payload,dict) or payload.get('phase')!='final_answer':
        return None
    stamp=record.get('timestamp')
    if not isinstance(stamp,str):
        raise ValueError('Final-answer timestamp missing')
    occurred=datetime.fromisoformat(stamp.replace('Z','+00:00'))
    if occurred.tzinfo is None:
        raise ValueError('Final-answer timestamp must include timezone')
    if occurred<=cutoff:
        return None
    source_hash=hashlib.sha256(('codex:'+str(path)+'#'+stamp).encode()).hexdigest()
    if source_hash in known:
        return None
    content=payload.get('content') or []
    if not isinstance(content,list):
        raise ValueError('Final-answer content must be a list')
    text=content[0].get('text','') if content and isinstance(content[0],dict) else ''
    if not isinstance(text,str):
        return None
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not ACCOMPLISHMENT.search(lines[0]):
        return None
    title=lines[1] if lines[0].lower() in {'done','done.'} and len(lines)>1 else lines[0]
    withheld=bool(sensitive(title))
    if withheld:
        title='Accomplishment reported; summary retained locally for sensitive-data review'
    return dict(kind='accomplishment',summary=('Codex reported: '+title)[:500],
                details='Automated final-answer observation; not independent verification or task completion. Raw source remains local.',
                project=next((token for token in ('starforge',) if re.search(r'\b'+token+r'\b',text,re.I)),None),
                source='starforge:codex-observer',source_id=source_hash,occurred_at=occurred.isoformat(),_withheld=withheld)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bootstrap-file',type=Path,required=True)
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--sessions',type=Path,default=Path.home()/'.codex/sessions')
    p.add_argument('--credentials-file',type=Path,required=True)
    p.add_argument('--interval',type=int,default=30)
    args=p.parse_args()
    if args.interval<10:
        p.error('Minimum interval is 10 seconds')
    args.state.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if args.state.is_symlink() or args.state.parent.stat().st_mode & 0o077:
        raise ValueError('Use a private local state directory')
    lock=args.state.with_suffix('.lock').open('w')
    os.chmod(lock.name,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state=json.loads(args.state.read_text()) if args.state.exists() else {}
    config=json.loads(args.bootstrap_file.read_text())
    cutoff,known=bootstrap(config['snapshot'])
    instance=str(uuid.uuid4())
    while True:
        try:
            with client(credentials_file=args.credentials_file,role='collector') as api:
                health=dict(source=socket.gethostname()+':codex-observer',instance_id=instance,scope='Codex final-answer observations',status='ok',reason='scan_complete',observed_runs=0)
                try:
                    counts=observe(api,args.sessions,state,cutoff,known)
                    if counts['malformed'] or counts['failed_files']:
                        health.update(status='degraded',reason='scan_failed')
                    print(json.dumps(counts),flush=True)
                except (OSError,ValueError,TypeError,httpx.HTTPError):
                    health.update(status='degraded',reason='scan_failed')
                    print('{"status":"degraded","reason":"scan_failed"}',flush=True)
                finally:
                    atomic_state(args.state,state)
                r=api.post('/api/collectors/heartbeat',json=health);r.raise_for_status()
        except (OSError,ValueError,httpx.HTTPError):
            print('{"status":"unreachable","reason":"retry_pending"}',flush=True)
        time.sleep(args.interval)


if __name__=='__main__':
    main()
