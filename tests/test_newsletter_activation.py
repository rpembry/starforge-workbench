import json
from pathlib import Path
import time

import httpx
import pytest

pytest.importorskip('bs4')
from workbench.newsletter_activate import activate, live_proof
from workbench.newsletter_download import DownloadError, GRANT, MAX_BYTES, read_html, validate_url
from workbench.newsletter_job import JobError, Remote, atomic_json, once, protected_read

FILE = 'https://files.todoist.com/user_upload/example/file.html'
CDN = 'https://todoist.b-cdn.net/uploads/file.html?token=synthetic&expires=9999999999'
HTML = '<html><body><h1>TLDR IT 2026-01-02</h1><a href="https://tldr.tech/it">Sign up</a><p>Full newsletter body.</p></body></html>'


def test_api_credentials_never_follow_redirect_even_with_ambient_client_setting():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(302,headers={'location':'https://evil.test/steal'})
    with httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=True) as client:
        with pytest.raises(JobError,match='remote_rejected'):
            Remote('synthetic-token',client).task('task1')
    assert len(calls)==1 and calls[0].url.host=='api.todoist.com'


def test_explicit_download_strips_credentials_and_cookies_on_cdn_hop():
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == 'files.todoist.com':
            return httpx.Response(302, headers={'location':CDN})
        return httpx.Response(200, text=HTML, headers={'content-type':'text/html'})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True,
                      headers={'Authorization':'ambient-secret'}, cookies={'ambient':'private'},
                      auth=('ambient-user','ambient-password')) as client:
        assert read_html(client, FILE, token='synthetic-token') == HTML
    assert len(calls) == 2
    assert calls[0].headers['Authorization'] == 'Bearer synthetic-token'
    assert 'Authorization' not in calls[1].headers
    assert all('Cookie' not in request.headers for request in calls)


@pytest.mark.parametrize('url', [
    'http://files.todoist.com/user_upload/a/file.html',
    'https://files.todoist.com.evil.test/user_upload/a/file.html',
    'https://files.todoist.com:443/user_upload/a/file.html',
    'https://user@files.todoist.com/user_upload/a/file.html',
    'https://files.todoist.com/user_upload/a/file.html#fragment',
    'https://files.todoist.com/api/v1/tasks',
    'https://files.todoist.com/user_upload/a/../file.html',
    'https://files.todoist.com/user_upload/a/%2e%2e/file.html',
    'https://files.todoist.com/user_upload/a/%252e%252e/file.html',
    'https://files.todoist.com/user_upload/a/%0afile.html',
    'https://files.todoist.com/user_upload/a/file.html?token=x',
    'https://files.todoist.com/user_upload/',
    'https://files.todoist.com/user_upload/a/file.pdf',
    'https://[bad/user_upload/a/file.html',
    'https://files.todoist.com/user_upload/a/\\file.html',
])
def test_invalid_source_url_rejected_before_any_request(url):
    def handler(request):
        pytest.fail('network must not be called')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DownloadError):
            read_html(client,url,token='synthetic-token')


@pytest.mark.parametrize('target', [
    'https://evil.test/file.html?token=x',
    'https://app.todoist.com/user_upload/a/file.html',
    FILE,
    'https://todoist.b-cdn.net/uploads/file.html',
    'http://todoist.b-cdn.net/uploads/file.html?token=x',
    'https://todoist.b-cdn.net:443/uploads/file.html?token=x',
    'https://user@todoist.b-cdn.net/uploads/file.html?token=x',
    'https://todoist.b-cdn.net/uploads/%2e%2e/file.html?token=x',
    'https://todoist.b-cdn.net/uploads/file.html?token=%0ax',
    '//todoist.b-cdn.net/uploads/file.html?token=x',
])
def test_invalid_redirect_never_followed(target):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302,headers={'location':target})
    with httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=True) as client:
        with pytest.raises(DownloadError):
            read_html(client,FILE,token='synthetic-token')
    assert len(calls)==1


def test_second_redirect_is_not_followed_or_reauthenticated():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(302,headers={'location':CDN if len(calls)==1 else FILE})
    with httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=True) as client:
        with pytest.raises(DownloadError,match='attachment_unavailable'):
            read_html(client,FILE,token='synthetic-token')
    assert len(calls)==2 and 'Authorization' not in calls[1].headers


def test_cloudfront_is_a_valid_credential_free_destination():
    validate_url('https://d1ysz50cxb9zwl.cloudfront.net/uploads/file.html?Signature=synthetic',cdn=True)


@pytest.mark.parametrize('kind', ['length','stream','type','encoding','compressed'])
def test_response_limits_and_type(kind):
    class LargeStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'x'*MAX_BYTES
            yield b'x'
    def handler(request):
        if kind=='length':
            return httpx.Response(200,headers={'content-length':str(MAX_BYTES+1),'content-type':'text/html'})
        if kind=='stream':
            return httpx.Response(200,stream=LargeStream(),headers={'content-type':'text/html'})
        if kind=='type':
            return httpx.Response(200,content=b'picture',headers={'content-type':'image/png'})
        if kind=='compressed':
            import gzip
            return httpx.Response(200,content=gzip.compress(b'html'),headers={'content-type':'text/html','content-encoding':'gzip'})
        return httpx.Response(200,content=b'\xff',headers={'content-type':'text/html'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DownloadError):
            read_html(client,FILE,token='synthetic-token')


class ProofRemote:
    html_text=HTML
    def __init__(self,*args,attachment_auth=False):
        assert attachment_auth
    def tasks(self,project):
        return [{'id':'task1','project_id':project,'checked':False,'content':'Newsletter title',
                 'description':'Already converted; never clear or overwrite'}]
    def task(self,task_id):
        return self.tasks('exampleProject')[0]
    def comments(self,task_id):
        return [{'id':'comment1','task_id':task_id,'attachment':{
            'file_name':'Newsletter title','file_type':'text/html','file_url':FILE}}]
    def html(self,url):
        assert url==FILE
        return self.html_text
    def update(self,*args):
        pytest.fail('proof must never update a task')


def setup_activation(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    units=tmp_path/'.config/systemd/user'
    units.mkdir(parents=True)
    root=Path(__file__).parents[1]
    for name in ['newsletter-inline.service','newsletter-inline.timer']:
        (units/name).write_bytes((root/'deploy'/name).read_bytes())
    state=tmp_path/'state'; state.mkdir(mode=0o700)
    token=tmp_path/'existing-token'; token.write_text('synthetic-token'); token.chmod(0o600)
    config_path=tmp_path/'config.json'
    config={'enabled':False,'project_id':'exampleProject',
            'credential_grant':'read-newsletter-attachments-and-update-existing-descriptions.v1',
            'state_dir':str(state),'token_file':str(token)}
    atomic_json(config_path,config)
    calls=[]
    def runner(args):
        calls.append(args)
        if 'start' in args:
            proof=json.loads(protected_read(state/'attachment-proof.json'))
            assert proof['live_attachment_read_and_parsed']
            assert json.loads(protected_read(config_path))['enabled']
            atomic_json(state/'last-run.json',{'status':'success','finished_at':time.time(),
                        'updated':0,'unchanged':1,'failed':0})
        if 'Result' in args: return 'success'
        if 'is-enabled' in args: return 'enabled'
        if 'is-active' in args: return 'active'
        if 'NextElapseUSecRealtime' in args: return 'synthetic next trigger'
        return ''
    return config_path,state,calls,runner


def test_personal_confirmation_required_before_config_read(tmp_path):
    with pytest.raises(JobError,match='personal_confirmation_required'):
        activate(tmp_path/'absent.json',confirmed=False)


def test_live_nonblank_attachment_proof_precedes_enabling(tmp_path,monkeypatch):
    path,state,calls,runner=setup_activation(tmp_path,monkeypatch)
    result=activate(path,confirmed=True,run=runner,remote_factory=ProofRemote)
    assert result['live_attachment_read_and_parsed'] is True
    assert result['source_bytes']==len(HTML.encode())
    assert result['initial_service_report']['updated']==0
    assert json.loads(protected_read(path))['attachment_grant']==GRANT
    assert any('enable' in call for call in calls)
    assert (state/'attachment-proof.json').stat().st_mode & 0o777 == 0o600


def test_noop_cannot_substitute_for_matching_attachment_proof(tmp_path,monkeypatch):
    path,state,calls,runner=setup_activation(tmp_path,monkeypatch)
    class LoginRemote(ProofRemote):
        html_text='<html><body>Login</body></html>'
    with pytest.raises(JobError,match='representative_attachment_not_found'):
        activate(path,confirmed=True,run=runner,remote_factory=LoginRemote)
    assert not any('start' in call or 'enable' in call for call in calls)
    assert not (state/'attachment-proof.json').exists()
    assert json.loads(protected_read(path))['enabled'] is False


def test_activation_failure_rolls_back_config_and_timer(tmp_path,monkeypatch):
    path,state,calls,runner=setup_activation(tmp_path,monkeypatch)
    def fail_enable(args):
        result=runner(args)
        if 'enable' in args: raise JobError('systemd_step_failed')
        return result
    with pytest.raises(JobError):
        activate(path,confirmed=True,run=fail_enable,remote_factory=ProofRemote)
    assert json.loads(protected_read(path))['enabled'] is False
    assert any('disable' in call and '--now' in call for call in calls)


def test_worker_rejects_missing_proof_before_token_read(tmp_path,monkeypatch):
    path,state,calls,runner=setup_activation(tmp_path,monkeypatch)
    config=json.loads(protected_read(path))
    config.update(enabled=True,attachment_grant=GRANT,token_file=str(tmp_path/'no-token'))
    atomic_json(path,config)
    with pytest.raises(FileNotFoundError) as error:
        once(path,apply=True)
    assert error.value.filename==str(state/'attachment-proof.json')


@pytest.mark.parametrize('field,value', [('implementation_hash','0'*64),
    ('project_id','otherProject'), ('live_attachment_read_and_parsed',False), ('source_hash','bad')])
def test_proof_is_bound_to_exact_implementation_and_project(tmp_path,monkeypatch,field,value):
    path,state,calls,runner=setup_activation(tmp_path,monkeypatch)
    activate(path,confirmed=True,run=runner,remote_factory=ProofRemote)
    proof=json.loads(protected_read(state/'attachment-proof.json'))
    proof[field]=value
    atomic_json(state/'attachment-proof.json',proof)
    with pytest.raises(JobError,match='live_attachment_proof_required'):
        once(path,apply=True)


def test_worker_uses_explicit_attachment_mode_only_after_valid_proof(tmp_path,monkeypatch):
    path,state,calls,runner=setup_activation(tmp_path,monkeypatch)
    activate(path,confirmed=True,run=runner,remote_factory=ProofRemote)
    monkeypatch.setattr('workbench.newsletter_job.Remote',ProofRemote)
    report=once(path,apply=True)
    assert report['status']=='success' and report['skipped']==1
    assert report['updated']==0
