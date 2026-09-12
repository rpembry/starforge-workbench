import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from workbench.codex_observer import observe
from workbench.reports import event_time, standup_window


def record(text,stamp='2026-09-12T10:00:00Z'):
    return (json.dumps({'type':'response_item','timestamp':stamp,'payload':{'phase':'final_answer','content':[{'text':text}]}})+'\n').encode()


def test_observer_cursor_partial_line_replay_rotation_and_no_task_inference(tmp_path):
    sessions=tmp_path/'sessions';sessions.mkdir();path=sessions/'fixture.jsonl'
    path.write_bytes(record('Fixed the fixture issue.\nRaw detail must stay local.')+record('Next step: fix missing things.')+b'{"type":')
    calls=[]
    def accept(request):
        calls.append(json.loads(request.content))
        return httpx.Response(201,json={'id':'fixture'})
    state={}
    with httpx.Client(transport=httpx.MockTransport(accept),base_url='https://fixture.example') as api:
        assert observe(api,sessions,state,'2026-09-01T00:00:00Z',set())['submitted']==1
        assert observe(api,sessions,state,'2026-09-01T00:00:00Z',set())['submitted']==0
        assert len(calls)==1 and calls[0]['summary']=='Codex reported: Fixed the fixture issue.'
        assert 'Raw detail' not in json.dumps(calls)
        assert len(state['_references'])==1
        path.unlink();path.write_bytes(record('Added a new fixture.','2026-09-12T11:00:00Z'))
        assert observe(api,sessions,state,'2026-09-01T00:00:00Z',set())['submitted']==1


def test_delivery_failure_keeps_cursor_for_retry_and_sensitive_summary_is_withheld(tmp_path):
    path=tmp_path/'fixture.jsonl';path.write_bytes(record('Updated password'+'=synthetic-value-only'))
    state={}
    def fail(request):
        return httpx.Response(503)
    with httpx.Client(transport=httpx.MockTransport(fail),base_url='https://fixture.example') as api:
        assert observe(api,tmp_path,state,'2026-09-01T00:00:00Z',set())['failed_files']==1
    assert next(iter(state.values()))['offset']==0
    calls=[]
    def accept(request):
        calls.append(json.loads(request.content));return httpx.Response(201,json={})
    with httpx.Client(transport=httpx.MockTransport(accept),base_url='https://fixture.example') as api:
        assert observe(api,tmp_path,state,'2026-09-01T00:00:00Z',set())['withheld']==1
    assert 'synthetic-value-only' not in json.dumps(calls)


def test_standup_weekend_and_date_only_legacy_precision():
    start,end=standup_window(datetime(2026,9,12,14,tzinfo=timezone.utc))
    assert start.isoformat()=='2026-09-10T09:00:00-04:00'
    assert end.isoformat()=='2026-09-11T09:00:00-04:00'
    event={'id':'legacy','occurred_at':'2026-09-10T00:00:00-04:00'}
    occurred=event_time(event,{'legacy':{'occurred_at':'2026-09-10'}})
    assert occurred.hour==12 and start<=occurred<=end


def test_malformed_timestamp_cannot_starve_same_or_later_files_after_restart(tmp_path):
    from workbench.codex_observer import atomic_state
    sessions=tmp_path/'sessions';sessions.mkdir()
    bad=record('Fixed malformed fixture.','invalid-timestamp-do-not-emit')
    (sessions/'a.jsonl').write_bytes(bad+record('Added valid activity in same file.'))
    (sessions/'b.jsonl').write_bytes(record('Updated valid activity in later file.'))
    calls=[]
    def accept(request):
        calls.append(json.loads(request.content));return httpx.Response(201,json={})
    state={}
    with httpx.Client(transport=httpx.MockTransport(accept),base_url='https://fixture.example') as api:
        first=observe(api,sessions,state,'2026-09-01T00:00:00Z',set())
        assert first['submitted']==2 and first['malformed']==1
        path=tmp_path/'cursors.json';atomic_state(path,state)
        state=json.loads(path.read_text())  # A process restart retains diagnostic evidence.
        with (sessions/'b.jsonl').open('ab') as f:f.write(record('Verified later activity.','2026-09-12T11:00:00Z'))
        again=observe(api,sessions,state,'2026-09-01T00:00:00Z',set())
        assert again['submitted']==1 and again['malformed']==1
        assert observe(api,sessions,state,'2026-09-01T00:00:00Z',set())['submitted']==0
    assert len({e['source_id'] for e in calls})==3
    problem=next(iter(state['_problems'].values()))
    assert problem['path']==str(sessions/'a.jsonl') and problem['offset']==0
    assert 'invalid-timestamp-do-not-emit' not in json.dumps(state)
    assert path.stat().st_mode & 0o777==0o600


def test_file_and_delivery_failures_do_not_starve_unaffected_files(tmp_path):
    blocked=tmp_path/'a.jsonl';blocked.write_bytes(record('Fixed retry fixture.'))
    (tmp_path/'b.jsonl').write_bytes(record('Added unrelated activity.'))
    state={};calls=[]
    def fail_one(request):
        data=json.loads(request.content)
        if 'retry fixture' in data['summary']:return httpx.Response(503)
        calls.append(data);return httpx.Response(201,json={})
    with httpx.Client(transport=httpx.MockTransport(fail_one),base_url='https://fixture.example') as api:
        result=observe(api,tmp_path,state,'2026-09-01T00:00:00Z',set())
        assert result['submitted']==1 and result['failed_files']==1
    def accept(request):
        calls.append(json.loads(request.content));return httpx.Response(201,json={})
    with httpx.Client(transport=httpx.MockTransport(accept),base_url='https://fixture.example') as api:
        result=observe(api,tmp_path,state,'2026-09-01T00:00:00Z',set())
        assert result['submitted']==1 and result['failed_files']==0
        assert not state['_file_errors']
    assert len(calls)==2 and len({e['source_id'] for e in calls})==2
    original=Path.open
    def inaccessible(path,*args,**kwargs):
        if path==blocked:raise PermissionError('private source details')
        return original(path,*args,**kwargs)
    with httpx.Client(transport=httpx.MockTransport(accept),base_url='https://fixture.example') as api,patch.object(Path,'open',inaccessible):
        result=observe(api,tmp_path,{},'2026-09-01T00:00:00Z',set())
        assert result['failed_files']==1 and result['submitted']==1
