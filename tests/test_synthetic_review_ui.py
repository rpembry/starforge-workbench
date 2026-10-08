"""In-process synthetic UI only: no listener, credentials, model or guest."""
import html
import json
import re

from fastapi.testclient import TestClient
import pytest

from coordinator.supervisor import Supervisor
from starforge_workbench.diagnostic_batch import DiagnosticWorker, MockQwenAdapter
from starforge_workbench.diagnostic_release import FakeReleaseEndpoint, ReleaseBroker, ReleaseError, ReleasePolicy, STATUSES
from starforge_workbench.diagnostic_vm_bridge import SupervisorDiagnosticFence
from starforge_workbench.synthetic_review_authority import SyntheticHumanAuthority, SyntheticSessionSeed
from starforge_workbench.synthetic_review_ui import COOKIE, create_synthetic_review_app
from test_coordinator_supervisor import Runtime, plan

ORIGIN = 'http://review-ui-' + 'a'*32 + '.localhost:8765'
HUMAN = 'synthetic-human-cookie-12345678901234567890'
CSRF = 'synthetic-human-csrf-12345678901234567890'
PAYLOAD = b'<script>alert("OUTBOUND_CANARY")</script>\x00\xff\r\n'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('No process launch in UI tests'))
    paths = {name: tmp_path/name for name in ('supervisor', 'attempt', 'inbox', 'authority')}
    for path in paths.values():
        path.mkdir(mode=0o700)
    now = [1000.0]
    clock = lambda: now[0]
    supervisor = Supervisor(paths['supervisor'], Runtime(), clock=lambda: 1000.0)
    generation = supervisor.acquire('controller', lease_seconds=60)['generation']
    p = plan()
    p['deadline_seconds'] = 600
    supervisor.launch(p, controller='controller', generation=generation, operation_id='launch')
    fence = SupervisorDiagnosticFence(supervisor, job_id='job-1', attempt_id='attempt-1',
          incarnation='inc-1', controller='controller', generation=generation)
    worker = DiagnosticWorker(paths['attempt'], 'job-1', 'attempt-1', fence.ownership, clock=clock)
    request = {'protocol': 'diagnostic.batch.v1', 'job_id': 'job-1', 'attempt_id': 'attempt-1',
               'operation_id': 'synthetic-ui-op', 'diagnostic_type': 'capacity.v1', 'target_id': 'synthetic-1',
               'parameters': {}, 'deadline': 1100, 'budget': {'steps': 1, 'max_output_bytes': 4096}}
    assert worker.run(request, MockQwenAdapter())['status'] == 'report_ready'
    seeds = (SyntheticSessionSeed(HUMAN, CSRF, 'synthetic-human', 'synthetic_human', 2000),
             SyntheticSessionSeed('synthetic-service-cookie-12345678901234567890', CSRF,
                                  'synthetic-service', 'service', 2000),
             SyntheticSessionSeed('synthetic-machine-cookie-12345678901234567890', CSRF,
                                  'operator', 'machine', 2000))
    authority = SyntheticHumanAuthority(paths['authority'], seeds, clock=clock)
    broker = ReleaseBroker(paths['attempt'], job_id='job-1', attempt_id='attempt-1', authenticate=authority,
              endpoint=FakeReleaseEndpoint(paths['inbox']), policy=ReleasePolicy('coordinator.synthetic', STATUSES),
              ownership=fence.ownership, clock=clock)
    snapshot = broker.prepare(PAYLOAD)
    app = create_synthetic_review_app(broker=broker, authority=authority, worker=worker,
                                     fence=fence, origin=ORIGIN, enabled=True, clock=clock)
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set(COOKIE, HUMAN)
    return client, broker, worker, supervisor, fence, now, paths, authority, snapshot


def form(client, decision='approve'):
    page = client.get('/review')
    assert page.status_code == 200
    handle = re.search(r'name="snapshot" value="([^"]+)"', page.text).group(1)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    return {'snapshot': handle, 'csrf_token': html.unescape(csrf), 'decision': decision}


def post(client, data, **kwargs):
    return client.post('/review/decision', data=data, headers={'Origin': ORIGIN, **kwargs.pop('headers', {})},
                       follow_redirects=False, **kwargs)


def test_disabled_factory_has_no_review_or_status_routes():
    client = TestClient(create_synthetic_review_app())
    assert client.get('/review').status_code == 404
    assert client.get('/status').status_code == 404


def test_exact_escaped_preview_and_local_detail_status_exclusion(setup, capsys, caplog):
    client, broker, _, _, _, _, paths, _, snap = setup
    page = client.get('/review')
    assert page.status_code == 200
    assert '<script>' not in page.text
    assert '&lt;script&gt;' in page.text and 'OUTBOUND_CANARY' in page.text
    assert snap.payload.hex(' ') in page.text
    assert '\\u0000' in page.text and '\\r\\n' in page.text and '\\xff' in page.text
    assert 'coordinator.synthetic' in page.text
    assert 'SYNTHETIC_SOURCE_CANARY' in page.text
    assert page.headers['cache-control'] == 'no-store, max-age=0'
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']
    status = client.get('/status')
    assert status.json() == {'protocol': 'diagnostic.status.v1', 'status': 'report_ready'}
    assert 'CANARY' not in status.text and 'payload' not in status.text
    assert 'same OS user or browser access' in page.text
    assert not (paths['inbox']/'inbox.json').exists()
    assert capsys.readouterr() == ('', '')
    assert 'CANARY' not in caplog.text


def test_snapshot_only_approval_no_ui_dispatch_and_exact_fake_send(setup):
    client, broker, _, _, fence, _, paths, _, snap = setup
    assert post(client, form(client)).status_code == 303
    assert client.post('/dispatch', headers={'Origin': ORIGIN}).status_code != 200
    assert not (paths['inbox']/'inbox.json').exists()
    with fence.scope():
        broker.dispatch()
    ledger = json.loads((paths['inbox']/'inbox.json').read_text())
    assert ledger == {snap.operation_id: snap.binding()}


@pytest.mark.parametrize('change', ['missing', 'changed', 'failed', 'cancelled'])
def test_status_requires_positive_unchanged_local_report_evidence(setup, change):
    client, _, _, _, _, _, paths, _, _ = setup
    from starforge_workbench.docker_worker import atomic
    if change == 'missing':
        (paths['attempt']/'local-report.json').unlink()
    elif change == 'changed':
        atomic(paths['attempt']/'local-report.json', {'summary': 'PRIVATE_REPORT_CANARY', 'findings': []})
    else:
        receipt = json.loads((paths['attempt']/'diagnostic.json').read_text())
        receipt['state'] = change
        atomic(paths['attempt']/'diagnostic.json', receipt)
    response = client.get('/status')
    assert response.status_code == 403
    assert response.json() == {'protocol': 'diagnostic.status.v1', 'status': 'failed'}
    assert 'CANARY' not in response.text


@pytest.mark.parametrize('path', ['/review/', '/review/?q=PRIVATE_CANARY',
                                 '/review?q=PRIVATE_CANARY', '/PRIVATE_PATH_CANARY'])
def test_unmatched_and_query_requests_have_no_redirect_or_echo(setup, path):
    client, _, _, _, _, _, _, _, _ = setup
    response = client.get(path, follow_redirects=False)
    assert response.status_code in {403, 404}
    assert response.json() == {'protocol': 'diagnostic.status.v1', 'status': 'failed'}
    assert 'location' not in response.headers and 'PRIVATE' not in response.text
    assert response.headers['cache-control'].startswith('no-store')


def test_wrong_method_has_canonical_content_free_http_error(setup):
    client, _, _, _, _, _, _, _, _ = setup
    response = client.put('/review', data=form(client), headers={'Origin': ORIGIN})
    assert response.status_code == 405
    assert response.json() == {'protocol': 'diagnostic.status.v1', 'status': 'failed'}
    assert 'location' not in response.headers and 'detail' not in response.text


@pytest.mark.parametrize('headers', [
    {'Authorization': 'Bearer synthetic-operator-token'},
    {'Cf-Access-Jwt-Assertion': 'synthetic-browser-or-service-assertion'},
    {'CF-Access-Client-Id': 'synthetic-service'},
    {'X-Forwarded-For': '127.0.0.1'}, {'Forwarded': 'for=127.0.0.1'},
    {'Host': 'public.example.invalid'},
])
def test_bearer_service_proxy_headers_never_grant_local_review(setup, headers):
    client, _, _, _, _, _, _, _, _ = setup
    response = client.get('/review', headers=headers)
    assert response.status_code == 403
    assert response.json() == {'protocol': 'diagnostic.status.v1', 'status': 'failed'}
    assert 'CANARY' not in response.text
    assert response.headers['cache-control'].startswith('no-store')


@pytest.mark.parametrize('token', [None, 'synthetic-service-cookie-12345678901234567890',
    'synthetic-machine-cookie-12345678901234567890', '{"actor":"human","approved":true}'])
def test_agent_service_and_caller_labels_deny(setup, token):
    client, _, _, _, _, _, _, _, _ = setup
    client.cookies.clear()
    if token is not None:
        client.cookies.set(COOKIE, token)
    assert client.get('/review').status_code == 403


@pytest.mark.parametrize('change', ['csrf', 'origin', 'extra', 'duplicate', 'destination', 'raw_payload'])
def test_form_injection_and_csrf_fail_before_approval(setup, change):
    client, broker, _, _, _, _, _, _, _ = setup
    data = form(client)
    headers = {}
    if change == 'csrf': data['csrf_token'] = 'PRIVATE_CSRF_CANARY'
    elif change == 'origin': headers['Origin'] = 'https://example.invalid'
    elif change == 'extra': data['actor'] = 'human'
    elif change == 'destination': data['destination'] = 'example.invalid'
    elif change == 'raw_payload': data['payload'] = 'PRIVATE_PAYLOAD_CANARY'
    elif change == 'duplicate':
        response = client.post('/review/decision', content='snapshot=x&snapshot=y&csrf_token=x&decision=approve',
                    headers={'Origin': ORIGIN, 'Content-Type': 'application/x-www-form-urlencoded'})
        assert response.status_code == 422
        return
    response = post(client, data, headers=headers)
    assert response.status_code in {403, 422}
    assert 'CANARY' not in response.text
    with pytest.raises(ReleaseError): broker.dispatch()


@pytest.mark.parametrize('change', ['edit', 'expire_view', 'cancel', 'takeover', 'expire_session', 'lease'])
def test_stale_view_and_attempt_fences_deny(setup, change):
    client, broker, _, supervisor, fence, now, _, _, _ = setup
    data = form(client)
    if change == 'edit': broker.prepare(b'edited synthetic bytes')
    elif change == 'expire_view': now[0] += 121
    elif change == 'cancel': supervisor.cancel('attempt-1', controller='controller', generation=fence.binding['generation'], operation_id='cancel')
    elif change == 'takeover': supervisor.acquire('replacement', owner_takeover=True)
    elif change == 'expire_session': now[0] = 2001
    elif change == 'lease': supervisor.clock = lambda: 1061.0
    assert post(client, data).status_code in {403, 409}
    with pytest.raises(ReleaseError): broker.dispatch()


def test_single_use_preview_and_revocation_prevent_dispatch(setup):
    client, broker, _, _, _, _, _, _, _ = setup
    data = form(client)
    assert post(client, data).status_code == 303
    assert post(client, data).status_code == 409
    assert post(client, form(client, 'revoke')).status_code == 303
    with pytest.raises(ReleaseError): broker.dispatch()


def test_ui_restart_invalidates_view_and_approval_expiry_denies(setup):
    client, broker, worker, _, fence, now, _, authority, _ = setup
    data = form(client)
    app = create_synthetic_review_app(broker=broker, authority=authority, worker=worker,
                  fence=fence, origin=ORIGIN, enabled=True, clock=lambda: now[0])
    restarted = TestClient(app, base_url=ORIGIN)
    restarted.cookies.set(COOKIE, HUMAN)
    assert post(restarted, data).status_code == 409
    assert post(client, data).status_code == 303
    now[0] += 301
    with pytest.raises(ReleaseError): broker.dispatch()


@pytest.mark.parametrize('operation', ['approve', 'revoke', 'preview', 'status'])
def test_session_expiring_during_fence_wait_is_rechecked(setup, monkeypatch, operation):
    from contextlib import contextmanager
    client, broker, _, _, fence, now, _, _, snapshot = setup
    data = form(client, 'revoke' if operation == 'revoke' else 'approve')
    original = fence.scope
    @contextmanager
    def delayed_fence():
        with original():
            now[0] = 2001
            yield
    monkeypatch.setattr(fence, 'scope', delayed_fence)
    response = client.get('/review' if operation == 'preview' else '/status') if operation in {'preview', 'status'} else post(client, data)
    assert response.status_code == 403
    assert response.json() == {'protocol': 'diagnostic.status.v1', 'status': 'failed'}
    assert broker.review() == snapshot


@pytest.mark.parametrize('kind', ['large', 'json', 'missing_origin', 'invalid_ascii'])
def test_untrusted_body_errors_are_content_free(setup, kind):
    client, _, _, _, _, _, _, _, _ = setup
    headers = {'Origin': ORIGIN, 'Content-Type': 'application/x-www-form-urlencoded'}
    body = b'PRIVATE_BODY_CANARY' * 1024 if kind == 'large' else b'PRIVATE_BODY_CANARY'
    if kind == 'json': headers['Content-Type'] = 'application/json'
    elif kind == 'missing_origin': del headers['Origin']
    elif kind == 'invalid_ascii': body = b'\xffPRIVATE_BODY_CANARY'
    response = client.post('/review/decision', content=body, headers=headers)
    assert response.status_code in {403, 413, 422}
    assert 'PRIVATE' not in response.text
    assert response.json() == {'protocol': 'diagnostic.status.v1', 'status': 'failed'}
