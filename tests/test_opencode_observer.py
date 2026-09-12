import json
import sqlite3
import httpx
import pytest
from workbench.opencode_observer import scan


def database(tmp_path):
    path=tmp_path/'opencode.db'
    with sqlite3.connect(path) as db:
        db.execute('create table session(id text,parent_id text)')
        db.execute('create table message(id text,session_id text,time_created integer,data text)')
        db.execute("insert into session values('ses_test',null)")
        db.execute("insert into session values('ses_child','ses_test')")
    return path


def add(path,identity,data,session='ses_test'):
    with sqlite3.connect(path) as db:
        db.execute('insert into message values(?,?,?,?)',(identity,session,1000,json.dumps(data)))


def record(completed=2000):
    return dict(role='assistant',time=dict(created=1000,completed=completed),providerID='openai',
                modelID='fixture-model',text='PRIVATE RESPONSE',tool={'input':'PRIVATE TOOL INPUT'})


def test_metadata_restart_replay_and_streaming(tmp_path):
    path=database(tmp_path);add(path,'msg_done',record());add(path,'msg_stream',record(None))
    add(path,'msg_child',record(),session='ses_child');add(path,'msg_user',{'role':'user','text':'PRIVATE PROMPT'})
    calls=[]
    def accept(req):calls.append(json.loads(req.content));return httpx.Response(201)
    state={'cutoff_ms':0}
    with httpx.Client(transport=httpx.MockTransport(accept),base_url='https://test') as api:
        assert scan(api,path,state)['submitted']==1
        assert 'PRIVATE' not in json.dumps(calls)
        assert scan(api,path,json.loads(json.dumps(state)))['submitted']==0
        assert scan(api,path,{'cutoff_ms':0})['submitted']==1
        assert calls[0]['source_id']==calls[1]['source_id']
        with sqlite3.connect(path) as db:db.execute('update message set data=? where id=?',(json.dumps(record()),'msg_stream'))
        assert scan(api,path,state)['submitted']==1


def test_bad_record_and_api_failure_do_not_starve_later_rows(tmp_path):
    path=database(tmp_path);add(path,'msg_bad',record('invalid'));add(path,'msg_fail',record());add(path,'msg_ok',record())
    def receive(req):
        message=json.loads(json.loads(req.content)['details'])['message_id']
        return httpx.Response(503 if message=='msg_fail' else 201)
    state={'cutoff_ms':0}
    with httpx.Client(transport=httpx.MockTransport(receive),base_url='https://test') as api:
        assert scan(api,path,state)==dict(submitted=1,malformed=1,failed=1)
        assert scan(api,path,state)==dict(submitted=0,malformed=1,failed=1)
    with sqlite3.connect(path) as db:db.execute('update message set data=? where id=?',(json.dumps(record()),'msg_bad'))
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(201)),base_url='https://test') as api:
        assert scan(api,path,state)==dict(submitted=2,malformed=0,failed=0)
        assert not state['diagnostics']


def test_missing_database_is_not_created(tmp_path):
    path=tmp_path/'missing.db'
    with pytest.raises(sqlite3.OperationalError):scan(None,path,{'cutoff_ms':0})
    assert not path.exists()
