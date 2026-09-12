import copy
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.main import create_app
from workbench.repository import SQLiteRepository
from workbench.worklog_import import plan


@pytest.fixture
def snapshot(tmp_path):
    path=tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE events(id INTEGER PRIMARY KEY, occurred_at TEXT,kind TEXT,title TEXT,details TEXT,project TEXT,source TEXT,created_at TEXT,updated_at TEXT);
CREATE TABLE tasks(id INTEGER PRIMARY KEY,status TEXT,title TEXT,details TEXT,project TEXT,priority INTEGER,due_at TEXT,source TEXT,created_at TEXT,updated_at TEXT,completed_at TEXT);''')
        for identity,details in [(1,'ordinary work context'),(2,'password'+'=synthetic-value-only')]:
            db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',(identity,'2026-07-01 12:00:00','accomplishment','A useful accomplishment',details,'Fixture','codex:local-reference#'+str(identity),'2026-07-01','2026-07-01'))
        for identity,source,status in [(1,'user:explicit task','next'),(2,'codex:inferred','next'),(3,'/local/imported.md','next'),(4,'user:finished','done')]:
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)',(identity,status,'Legacy task '+str(identity),'Details','Fixture',50,None,source,'2026-07-01','2026-07-01',None))
    path.chmod(0o600)
    return path


@pytest.fixture
def api(tmp_path):
    repo=SQLiteRepository(tmp_path/'state'/'workbench.sqlite')
    with TestClient(create_app(repo,Auth({'operator':'o'*40,'collector':'c'*40}))) as c:
        c.headers['Authorization']='Bearer '+'o'*40
        yield c


def test_plan_preserves_time_and_hashes_and_withholds_sensitive_details(snapshot):
    events=plan(snapshot,'events')
    assert events['records'][0]['event']['occurred_at']=='2026-07-01T12:00:00-04:00'
    assert events['records'][1]['disposition']=='redacted'
    assert 'synthetic-value-only' not in json.dumps(events)
    assert 'codex:local-reference' not in json.dumps(events)
    tasks=plan(snapshot,'tasks')
    assert [r['disposition'] for r in tasks['records']]==['imported','quarantined','quarantined','imported']
    assert tasks['records'][-1]['action']['status']=='done'
    assert all(len(r['row_sha256'])==64 for r in tasks['records'])


def test_repeat_import_preserves_count_provenance_and_quarantines(snapshot,api):
    for phase in ['events','tasks']:
        data=plan(snapshot,phase)
        first=api.post('/api/imports',json=data)
        assert first.status_code==201,first.text
        repeat=api.post('/api/imports',json=data)
        assert repeat.json()['id']==first.json()['id']
    assert len(api.get('/api/events').json()['items'])==2
    assert len(api.get('/api/actions').json()['items'])==2
    dash=api.get('/api/dashboard').json()
    assert len(dash['next'])==1 and not dash['suggestions']
    assert len(api.get('/api/import_records').json()['items'])==6
    assert len(api.get('/api/import_batches').json()['items'])==2


def test_changed_legacy_row_conflicts_without_partial_writes(snapshot,api):
    data=plan(snapshot,'events')
    assert api.post('/api/imports',json=data).status_code==201
    changed=copy.deepcopy(data)
    changed['snapshot_sha256']='a'*64
    changed['records'].insert(0,{**copy.deepcopy(data['records'][0]),'legacy_id':100})
    changed['records'][1]['event']['summary']='Changed interpretation'
    assert api.post('/api/imports',json=changed).status_code==409
    assert len(api.get('/api/events').json()['items'])==2
    assert len(api.get('/api/import_batches').json()['items'])==1
    api.headers['Authorization']='Bearer '+'c'*40
    assert api.post('/api/imports',json=data).status_code==403


def test_new_snapshot_same_records_deduplicates_and_live_changes_survive(snapshot,api):
    data=plan(snapshot,'tasks')
    api.post('/api/imports',json=data)
    action=next(a for a in api.get('/api/actions').json()['items'] if a['status']=='accepted')
    api.patch('/api/actions/'+action['id'],json={'version':1,'status':'done'})
    data['snapshot_sha256']='b'*64
    assert api.post('/api/imports',json=data).status_code==201
    assert len(api.get('/api/actions').json()['items'])==2
    assert not api.get('/api/dashboard').json()['next']


def test_overlap_with_new_observer_links_existing_event(snapshot,api):
    data=plan(snapshot,'events')
    first=data['records'][0]
    existing=api.post('/api/events',json={'summary':'Codex reported the accomplishment','kind':'accomplishment','source':'starforge:codex-observer','source_id':first['source_sha256']}).json()
    assert api.post('/api/imports',json=data).status_code==201
    assert len(api.get('/api/events').json()['items'])==2
    imported=next(r for r in api.get('/api/import_records').json()['items'] if r['legacy_id']==first['legacy_id'])
    assert imported['target_id']==existing['id']


def test_reports_use_central_records_and_exclude_quarantine(snapshot,api):
    from datetime import datetime, timezone
    from workbench.reports import report
    for phase in ['events','tasks']:
        assert api.post('/api/imports',json=plan(snapshot,phase)).status_code==201
    r=api.get('/api/reports/accomplishments')
    assert r.status_code==200 and len(r.json()['accomplishments'])==2
    assert len(api.get('/api/reports/todo').json()['plan'])==1
    window=report(api.app.state.repository,'standup',datetime(2026,7,2,14,tzinfo=timezone.utc))
    assert len(window['accomplishments'])==2
    assert api.get('/reports/standup').status_code==200
    api.headers['Authorization']='Bearer '+'c'*40
    assert api.get('/api/reports/accomplishments').status_code==403
    assert api.get('/reports/standup').status_code==403


@pytest.mark.parametrize('metadata', ['invalid-json', '[]', 'null', '{"occurred_at": 123}', '{"occurred_at": "2026-99-99"}'])
def test_invalid_import_metadata_cannot_poison_reports(snapshot, api, metadata):
    data = plan(snapshot, 'events')
    data['records'][0]['metadata'] = metadata
    assert api.post('/api/imports', json=data).status_code == 422
    assert not api.get('/api/import_records').json()['items']
    for kind in ['standup', 'todo', 'accomplishments']:
        assert api.get('/api/reports/'+kind).status_code == 200
