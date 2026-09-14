import json
from zoneinfo import ZoneInfo
from unittest.mock import Mock

import pytest
from workbench import report_ai as ai
from workbench.report_suggestions import snapshot, digest, validate_output, view
from test_api import repo, api


def idea(ref='actions:a'):
    return {'kind':'opportunity','title':'Explore a reusable checklist',
            'rationale':'The report describes recurring review work; reuse may help.',
            'next_question':'Would a checklist help future reviews?', 'evidence':[ref]}


def test_snapshot_bounded_excludes_details_and_volatile_clock():
    rows=[{'id':str(i),'summary':'Visible summary','details':'PRIVATE RAW DATA','occurred_at':'2026-09-14'} for i in range(80)]
    data={'recent':rows,'generated_at':'one','collectors':[{'id':'c','source':'fixture','health':'ok','heartbeat_at':'one'}]}
    first=snapshot('dashboard',data)
    data['generated_at']='two';data['collectors'][0]['heartbeat_at']='two'
    assert digest(first)==digest(snapshot('dashboard',data))
    assert len(first['evidence'])==21
    assert 'PRIVATE RAW DATA' not in json.dumps(first)


def test_output_requires_real_evidence_and_deduplicates():
    evidence={'actions:a':{'title':'review','updated_at':'2026-09-14'}}
    assert len(validate_output({'items':[idea(),idea()]},evidence))==1
    with pytest.raises(ValueError):validate_output({'items':[idea('invented')]},evidence)
    with pytest.raises(ValueError):validate_output({'items':[dict(idea(),extra='no')]},evidence)


def test_persistence_change_failure_and_retry(repo):
    from workbench.models import ActionIn
    action=repo.create('actions',ActionIn(title='Review a reusable workflow',status='accepted').model_dump(), 'fixture')
    generation=Mock(return_value={'items':[idea('actions:'+action['id'])]})
    settings=ai.Settings(model='fixture',interval_seconds=300)
    stamp=[1000.]
    ai.tick(repo,settings,ZoneInfo('UTC'),generator=generation,clock=lambda:stamp[0])
    assert generation.call_count==4
    data=ai.report_data(repo,'todo',ZoneInfo('UTC'))
    assert view(repo,'todo',data,current=1000)['status']=='ready'
    # Saved output survives repository instances; repeated reads never generate.
    from workbench.repository import SQLiteRepository
    assert view(SQLiteRepository(repo.path),'todo',data,current=1000)['items']
    ai.tick(repo,settings,ZoneInfo('UTC'),generator=generation,clock=lambda:1001)
    assert generation.call_count==4
    repo.patch('actions',action['id'],{'version':action['version'],'title':'Changed commitment'},'fixture')
    data=ai.report_data(repo,'todo',ZoneInfo('UTC'))
    assert view(repo,'todo',data,current=1002)['status']=='stale'
    ai.tick(repo,settings,ZoneInfo('UTC'),generator=Mock(side_effect=ValueError('PRIVATE ERROR')),clock=lambda:1301)
    state=view(repo,'todo',data,current=1301)
    assert state['generation_status']=='failed' and state['items']
    assert 'PRIVATE ERROR' not in json.dumps(state)
    with repo.connection() as db:
        assert db.execute('select count(*) from actions').fetchone()[0]==1
    retry=Mock(return_value={'items':[]})
    ai.tick(repo,settings,ZoneInfo('UTC'),generator=retry,clock=lambda:1302)
    assert not retry.called
    ai.tick(repo,settings,ZoneInfo('UTC'),generator=retry,clock=lambda:1602)
    assert retry.call_count==4


def test_crash_lease_prevents_immediate_duplicate_generation(repo):
    from workbench.models import ActionIn
    repo.create('actions',ActionIn(title='Review',status='accepted').model_dump(),'fixture')
    settings=ai.Settings(model='fixture',interval_seconds=300)
    with pytest.raises(SystemExit):
        ai.tick(repo,settings,ZoneInfo('UTC'),generator=Mock(side_effect=SystemExit),clock=lambda:1000)
    with repo.connection() as db:
        assert db.execute("select lease_until from report_suggestions where kind='dashboard'").fetchone()[0]==1300
    generator=Mock(return_value={'items':[]})
    ai.tick(repo,settings,ZoneInfo('UTC'),generator=generator,clock=lambda:1001)
    assert generator.call_count==3


def test_html_and_json_share_saved_suggestions_and_escape(api,repo):
    action=api.post('/api/actions',json={'title':'Review','status':'accepted'}).json()
    output=idea('actions:'+action['id']);output['title']='<script>bad()</script>'
    ai.tick(repo,ai.Settings(model='fixture'),ZoneInfo('UTC'),generator=lambda *_:{'items':[output]})
    for kind in ('standup','todo','accomplishments'):
        data=api.get('/api/reports/'+kind).json()
        assert data['report_suggestions']['items'][0]['title']==output['title']
        html=api.get('/reports/'+kind)
        assert html.status_code==200
        assert '&lt;script&gt;' in html.text and '<script>bad()' not in html.text
    assert api.get('/api/dashboard').json()['report_suggestions']['items']
    assert '&lt;script&gt;' in api.get('/').text


def test_settings_reject_remote_plaintext_and_default_unconfigured(monkeypatch):
    monkeypatch.delenv('WB_REPORT_AI_CONFIG',raising=False)
    assert ai.load_settings() is None
    with pytest.raises(ValueError):ai.Settings(model='fixture',url='http://example.com')
    with pytest.raises(ValueError):ai.Settings(model='fixture',interval_seconds=1)


def test_ollama_request_is_bounded_structured_and_has_no_tools(monkeypatch):
    import httpx
    original = httpx.Client
    calls=[]
    def reply(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200,json={'done':True,'response':json.dumps({'items':[]})})
    monkeypatch.setattr(ai.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(reply),**kw))
    assert ai.generate(ai.Settings(model='already-installed'),{'evidence':{}})=={'items':[]}
    assert calls[0]['stream'] is False and calls[0]['keep_alive']==0
    assert calls[0]['options']['num_predict']==1800
    assert 'tools' not in calls[0] and calls[0]['format']['type']=='object'


def test_reads_do_not_generate_and_collectors_cannot_read_suggestions(api,monkeypatch):
    from test_api import COLLECTOR
    generator=Mock(side_effect=AssertionError('GET must not invoke inference'))
    monkeypatch.setattr(ai,'generate',generator)
    for path in ['/api/reports/standup','/api/dashboard','/reports/standup','/']:
        assert api.get(path).status_code==200
    assert not generator.called
    api.headers['Authorization']='Bearer '+COLLECTOR
    for path in ['/api/reports/standup','/api/dashboard','/reports/standup','/']:
        assert api.get(path).status_code==403


def test_service_lifecycle_starts_and_stops_optional_worker(repo,monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from workbench.main import create_app
    from workbench.auth import Auth
    from test_api import OPERATOR,COLLECTOR
    stop=threading.Event()
    start=Mock(return_value=(stop,None))
    monkeypatch.setattr(ai,'load_settings',lambda:ai.Settings(model='fixture'))
    monkeypatch.setattr(ai,'start',start)
    with TestClient(create_app(repo,Auth({'operator':OPERATOR,'collector':COLLECTOR}))):
        assert start.call_count==1
        assert not stop.is_set()
    assert stop.is_set()
