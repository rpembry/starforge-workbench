import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
import httpx
import pytest
from workbench.opencode_observer import scan, scan_attention


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


def test_verified_plugin_contract_minimizes_content_and_ignores_late_completion(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required to execute the synthetic OpenCode plugin fixture')
    queue = tmp_path/'attention.jsonl'
    plugin = Path(__file__).parents[1]/'config/opencode-attention.example.js'
    test_plugin = tmp_path/'opencode-attention.mjs'
    shutil.copyfile(plugin, test_plugin)
    script = f'''import plugin from {json.dumps(test_plugin.as_uri())};
const hooks = await plugin();
const user = (id, created) => hooks["chat.message"]({{sessionID:"ses_test",messageID:id}},
  {{message:{{id,role:"user",time:{{created}},system:"PRIVATE PROMPT"}},parts:[{{text:"PRIVATE"}}]}});
const assistant = (id, parentID, extra={{}}) => hooks.event({{event:{{type:"message.updated",properties:{{info:
  {{id,sessionID:"ses_test",role:"assistant",parentID,time:{{created:Date.now()}},providerID:"fixture",modelID:"fixture",...extra,text:"PRIVATE RESPONSE"}}}}}}}});
const tool = (messageID, callID, status="running") => hooks.event({{event:{{type:"message.part.updated",properties:{{part:
  {{id:"part_"+callID,sessionID:"ses_test",messageID,type:"tool",callID,tool:"question",state:{{status,input:{{question:"PRIVATE"}}}}}}}}}}}});
await user("msg_old", Date.now()-1000);
await assistant("asst_old", "msg_old");
await tool("asst_old", "call_one");
await tool("asst_old", "call_late");
await hooks.event({{event:{{type:"permission.updated",properties:{{id:"per_one",sessionID:"ses_test",messageID:"asst_old",metadata:{{secret:"PRIVATE"}}}}}}}});
await hooks["tool.execute.before"]({{tool:"question",sessionID:"ses_test",callID:"call_one"}},{{args:{{question:"PRIVATE QUESTION"}}}});
await hooks.event({{event:{{type:"permission.replied",properties:{{sessionID:"ses_test",permissionID:"per_one",response:"once"}}}}}});
await tool("asst_old", "call_one", "completed");
await assistant("asst_old", "msg_old", {{finish:"tool-calls",time:{{created:Date.now()-500,completed:Date.now()}}}});
await assistant("asst_step_two", "msg_old", {{finish:"stop",time:{{created:Date.now()-200,completed:Date.now()}}}});
await hooks.event({{event:{{type:"session.status",properties:{{sessionID:"ses_test",status:{{type:"busy"}}}}}}}});
await hooks.event({{event:{{type:"session.status",properties:{{sessionID:"ses_test",status:{{type:"retry",attempt:1,message:"PRIVATE",next:Date.now()}}}}}}}});
await user("msg_new", Date.now()+1);
await hooks.event({{event:{{type:"session.status",properties:{{sessionID:"ses_test",status:{{type:"busy"}}}}}}}});
await hooks.event({{event:{{type:"session.status",properties:{{sessionID:"ses_test",status:{{type:"idle"}}}}}}}});
await hooks.event({{event:{{type:"session.idle",properties:{{sessionID:"ses_test"}}}}}});
await hooks["tool.execute.before"]({{tool:"question",sessionID:"ses_test",callID:"call_late"}},{{args:{{question:"PRIVATE DELAYED"}}}});
await assistant("asst_new", "msg_new", {{error:{{name:"APIError",data:{{message:"PRIVATE ERROR"}}}}}});
await assistant("asst_late", "msg_old", {{time:{{created:Date.now(),completed:Date.now()+1}}}});
await user("msg_resume", Date.now()+2);
await hooks.event({{event:{{type:"session.status",properties:{{sessionID:"ses_test",status:{{type:"idle"}}}}}}}});
'''
    env = dict(os.environ, WB_OPENCODE_ATTENTION_EVENTS=str(queue))
    subprocess.run([node, '--input-type=module', '-e', script], check=True, env=env)
    raw = queue.read_text()
    records = [json.loads(line) for line in raw.splitlines()]
    assert 'PRIVATE' not in raw
    assert [record['kind'] for record in records] == ['generation', 'observation', 'observation', 'observation', 'observation', 'generation', 'observation', 'generation']
    assert [record.get('reason') for record in records if record['kind'] == 'observation'] == [
        'permission_wait', 'user_question', 'permission_wait', 'user_question', 'provider_error']
    observations = [record for record in records if record['kind'] == 'observation']
    assert [record['state'] for record in observations] == ['open', 'open', 'resolved', 'resolved', 'open']
    assert observations[0]['incident_id'] == observations[2]['incident_id']
    assert observations[1]['incident_id'] == observations[3]['incident_id']
    assert all(len(record['incident_id']) == 64 for record in observations)
    assert observations[-1]['generation_id'] == 'msg_new'
    assert records[-1]['generation_id'] == 'msg_resume'


def test_attention_queue_restart_replay_and_failure_ordering(tmp_path):
    queue = tmp_path/'attention.jsonl'
    records = [
        {'kind': 'generation', 'provider': 'opencode', 'session_id': 'ses_test',
         'generation_id': 'msg_one', 'source': 'opencode-plugin', 'source_instance': 'instance_one',
         'started_at': '2026-09-12T12:00:00+00:00', 'provenance': 'opencode.chat.message'},
        {'kind': 'observation', 'provider': 'opencode', 'session_id': 'ses_test',
         'generation_id': 'msg_one', 'source': 'opencode-plugin', 'source_instance': 'instance_one',
         'incident_id': 'a'*64, 'sequence': 1, 'observed_at': '2026-09-12T12:00:01+00:00',
         'reason': 'permission_wait', 'state': 'open', 'provenance': 'opencode.permission.updated'},
    ]
    queue.write_text(''.join(json.dumps(record)+'\n' for record in records)+'{"partial":')
    calls = []
    def accept(request):
        calls.append(json.loads(request.content)); return httpx.Response(201, json={})
    state = {}
    with httpx.Client(transport=httpx.MockTransport(accept), base_url='https://test') as api:
        assert scan_attention(api, queue, state) == dict(submitted=2, malformed=0, rejected=0, failed=0)
        assert scan_attention(api, queue, state) == dict(submitted=0, malformed=0, rejected=0, failed=0)
    replay = {}
    def reject_duplicates(request):
        code = 'duplicate_observation' if request.url.path.endswith('observations') else None
        return httpx.Response(409, json={'error': {'code': code}}) if code else httpx.Response(201, json={})
    with httpx.Client(transport=httpx.MockTransport(reject_duplicates), base_url='https://test') as api:
        assert scan_attention(api, queue, replay) == dict(submitted=1, malformed=0, rejected=1, failed=0)
    failed = {}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)), base_url='https://test') as api:
        assert scan_attention(api, queue, failed)['failed'] == 1
        assert failed.get('attention_offset', 0) == 0


@pytest.mark.parametrize('relation', ['smaller', 'equal', 'larger'])
def test_attention_queue_replacement_resets_cursor_by_file_identity(tmp_path, relation):
    generation = {'kind': 'generation', 'provider': 'opencode', 'session_id': 'ses_new',
        'generation_id': 'msg_new', 'source': 'opencode-plugin', 'source_instance': 'instance_new',
        'started_at': '2026-09-12T12:00:00+00:00', 'provenance': 'opencode.chat.message'}
    observation = {'kind': 'observation', 'provider': 'opencode', 'session_id': 'ses_new',
        'generation_id': 'msg_new', 'source': 'opencode-plugin', 'source_instance': 'instance_new',
        'incident_id': 'b'*64, 'sequence': 1, 'observed_at': '2026-09-12T12:00:01+00:00',
        'reason': 'user_question', 'state': 'open', 'provenance': 'opencode.tool.question'}
    replacement = ''.join(json.dumps(record)+'\n' for record in (generation, observation)).encode()
    old = {**generation, 'session_id': 'ses_old', 'generation_id': 'msg_old'}
    delta = {'smaller': -10, 'equal': 0, 'larger': 10}[relation]
    old_size = len(replacement)+delta
    serialized = json.dumps(old).encode()
    assert old_size > len(serialized)+1
    queue = tmp_path/'attention.jsonl'
    queue.write_bytes(serialized+b' '*(old_size-len(serialized)-1)+b'\n')
    calls = []
    def accept(request):
        calls.append(request.url.path); return httpx.Response(201, json={})
    state = {}
    with httpx.Client(transport=httpx.MockTransport(accept), base_url='https://test') as api:
        scan_attention(api, queue, state)
        calls.clear()
        rotated = tmp_path/'replacement.jsonl'
        rotated.write_bytes(replacement)
        os.replace(rotated, queue)
        result = scan_attention(api, queue, state)
    assert result == dict(submitted=2, malformed=0, rejected=0, failed=0)
    assert calls == ['/api/provider-attention/generations', '/api/provider-attention/observations']
