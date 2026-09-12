import json
import uuid
from datetime import datetime,timezone
import httpx
from workbench.claude_observer import event_from_record
from workbench.codex_observer import observe


def test_claude_metadata_only_identity_and_restart_dedup(tmp_path):
    row=dict(type='assistant',timestamp='2026-09-12T11:00:00Z',sessionId=str(uuid.uuid4()),uuid=str(uuid.uuid4()),message={'content':[{'type':'text','text':'PRIVATE RESPONSE'},{'type':'tool_use','input':{'private':'PRIVATE TOOL INPUT'}}]})
    path=tmp_path/'fixture.jsonl';path.write_text(json.dumps(row)+'\n')
    calls=[]
    def accept(request):calls.append(json.loads(request.content));return httpx.Response(201,json={})
    state={}
    with httpx.Client(transport=httpx.MockTransport(accept),base_url='https://fixture.example') as api:
        assert observe(api,tmp_path,state,'2026-09-11T00:00:00Z',set(),parser=event_from_record)['submitted']==1
        state=json.loads(json.dumps(state))
        assert observe(api,tmp_path,state,'2026-09-11T00:00:00Z',set(),parser=event_from_record)['submitted']==0
    assert 'PRIVATE' not in json.dumps(calls)
    assert calls[0]['kind']=='observation'
    assert json.loads(calls[0]['details'])['session_id']==row['sessionId']
    row['timestamp']='invalid'
    path.write_text(json.dumps(row)+'\n'+json.dumps({**row,'timestamp':'2026-09-12T11:00:00Z'})+'\n')
    with httpx.Client(transport=httpx.MockTransport(accept),base_url='https://fixture.example') as api:
        result=observe(api,tmp_path,{},'2026-09-11T00:00:00Z',set(),parser=event_from_record)
        assert result['malformed']==1 and result['submitted']==1


def test_claude_lost_ack_and_cursor_loss_replay_use_one_api_event(tmp_path):
    from fastapi.testclient import TestClient
    from workbench.auth import Auth
    from workbench.main import create_app
    from workbench.repository import SQLiteRepository
    repo = SQLiteRepository(tmp_path/'state'/'workbench.sqlite')
    sessions = tmp_path/'sessions'
    sessions.mkdir()
    row = dict(type='assistant', timestamp='2026-09-12T11:00:00Z',
               sessionId=str(uuid.uuid4()), uuid=str(uuid.uuid4()),
               message={'content': [{'type': 'text', 'text': 'PRIVATE RESPONSE'}]})
    (sessions/'fixture.jsonl').write_text(json.dumps(row)+'\n')
    state = {}
    with TestClient(create_app(repo, Auth({'operator': 'o'*40, 'collector': 'c'*40}))) as server:
        lose_ack = True
        def deliver(request):
            nonlocal lose_ack
            response = server.post('/api/events', json=json.loads(request.content),
                                   headers={'Authorization': 'Bearer '+'c'*40})
            assert response.status_code == 201
            if lose_ack:
                lose_ack = False
                raise httpx.ReadError('Acknowledgement lost', request=request)
            return httpx.Response(response.status_code, json=response.json())
        with httpx.Client(transport=httpx.MockTransport(deliver), base_url='https://fixture.example') as api:
            assert observe(api, sessions, state, '2026-09-11T00:00:00Z', set(), parser=event_from_record)['failed_files'] == 1
            state = json.loads(json.dumps(state))
            assert observe(api, sessions, state, '2026-09-11T00:00:00Z', set(), parser=event_from_record)['submitted'] == 1
            assert observe(api, sessions, {}, '2026-09-11T00:00:00Z', set(), parser=event_from_record)['submitted'] == 1
    assert len(repo.list('events')) == 1
    assert not repo.list('actions')
    assert 'PRIVATE RESPONSE' not in json.dumps(repo.list('events'))
