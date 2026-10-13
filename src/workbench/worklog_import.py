"""Plan a conservative import from a protected, consistent Worklog snapshot."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .client import client
from .legacy import canonical, digest
from .models import ImportBatch

PATTERNS = {
    'private_key': r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
    'credential': r'(?i)(?:password|passwd|secret|api[_ -]?key|access[_ -]?token|client[_ -]?secret)\s*[=:]\s*[^\s,;]{6,}',
    'token': r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b',
    'bearer': r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]{15,}',
    'jwt': r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+',
    'credential_url': r'https?://[^\s/:]+:[^\s/@]+@',
    'ssn': r'\b\d{3}-\d{2}-\d{4}\b',
    'phi_marker': r'(?i)\b(?:patient name|date of birth|medical record number|patient id)\s*[:=]',
}


def sensitive(text):
    return [key for key, pattern in PATTERNS.items() if re.search(pattern, text)]


def legacy_time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not parsed.tzinfo:
        parsed = parsed.replace(tzinfo=ZoneInfo('America/New_York'))
    return parsed.isoformat()


def plan(snapshot, phase):
    path = Path(snapshot)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Use an owned, mode-0600 SQLite backup, not the live database')
    snapshot_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    records = []
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1', uri=True) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Snapshot integrity check failed')
        for raw in db.execute('SELECT * FROM '+('events' if phase == 'events' else 'tasks')+' ORDER BY id'):
            old = dict(raw)
            source = old.get('source') or ''
            reasons = sensitive(canonical(old))
            title = old['title']
            if sensitive(title):
                title = f'Legacy {phase} record {old["id"]} — text held for sensitive-data review'
            details = old.get('details') or ''
            if reasons:
                details = 'Original details retained only in the protected local legacy archive pending sensitive-data review.'
            metadata = {k: old.get(k) for k in ['kind', 'status', 'priority', 'due_at', 'created_at', 'updated_at', 'completed_at', 'occurred_at'] if k in old}
            metadata.update(details_withheld=bool(reasons), review_reasons=reasons, original_source_retained_in_local_snapshot=True)
            item = dict(legacy_id=old['id'], row_sha256=digest(old), source_sha256=hashlib.sha256(source.encode()).hexdigest(), metadata=canonical(metadata))
            if phase == 'events':
                occurred = old.get('occurred_at') or old['created_at']
                if not old.get('occurred_at'):
                    metadata['time_basis'] = 'legacy_created_at; occurrence unknown'
                item.update(disposition='redacted' if reasons else 'imported', event=dict(
                    summary=title, details=details, project=old.get('project'),
                    kind='accomplishment' if old['kind']=='accomplishment' else 'observation',
                    occurred_at=legacy_time(occurred), source='legacy:starforge-worklog:events', source_id=str(old['id'])), metadata=canonical(metadata))
            else:
                parsed=urlsplit(source)
                explicit = source.startswith('user:') or (parsed.scheme=='https' and parsed.hostname in {'github.com'} and not parsed.username)
                quarantine = source.startswith('codex:') or not explicit or bool(reasons)
                status = {'next':'accepted', 'now':'in_progress', 'done':'done', 'dropped':'canceled'}.get(old['status'])
                if not status:
                    quarantine=True
                item.update(disposition='quarantined' if quarantine else 'imported', action=dict(
                    title=title, details=details, project=old.get('project'), status='proposed' if quarantine else status,
                    execution_mode='human', actor='Operator', priority=max(0,min(3,int(old['priority'])//25)),
                    due_date=old['due_at'][:10] if old.get('due_at') else None,
                    source='legacy:starforge-worklog:tasks',source_id=str(old['id'])))
            records.append(item)
    batch=ImportBatch(dataset='starforge-worklog',snapshot_sha256=snapshot_hash,phase=phase,records=records)
    return batch.model_dump(mode='json')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    prepare=sub.add_parser('plan')
    prepare.add_argument('--snapshot',type=Path,required=True)
    prepare.add_argument('--phase',choices=['events','tasks'],required=True)
    prepare.add_argument('--output',type=Path,required=True)
    apply=sub.add_parser('apply')
    apply.add_argument('--manifest',type=Path,required=True)
    args=p.parse_args()
    if args.command=='plan':
        data=plan(args.snapshot,args.phase)
        if args.output.parent.stat().st_mode & 0o077:
            raise ValueError('Manifest parent must be private')
        fd=os.open(args.output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as f:json.dump(data,f,indent=2)
        print(json.dumps({'phase':args.phase,'records':len(data['records']),'dispositions':dict(Counter(r['disposition'] for r in data['records'])),'manifest_sha256':digest(data)}))
    else:
        if args.manifest.is_symlink() or args.manifest.stat().st_mode & 0o077 or args.manifest.stat().st_uid != os.getuid():
            raise ValueError('Use a private manifest owned by you')
        data=ImportBatch.model_validate_json(args.manifest.read_text()).model_dump(mode='json')
        with client() as api:
            r=api.post('/api/imports',json=data,timeout=60)
            if r.is_error:
                raise SystemExit('Import failed with HTTP '+str(r.status_code)+'; inspect structured error without exposing payload')
            result=r.json();print(json.dumps({k:result[k] for k in ['id','phase','record_count','manifest_sha256']}))


if __name__=='__main__':
    main()
