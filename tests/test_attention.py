from datetime import datetime, timedelta, timezone
import pytest
from test_api import api, repo, action, COLLECTOR


def run(api, action_id=None, status='running', source_id='fixture', provider='codex'):
    response = api.post('/api/runs', json=dict(source='fixture', source_id=source_id, context='Fixture agent',
        provider=provider, actor='Fixture agent', status=status,
        started_at=datetime.now(timezone.utc).isoformat()))
    assert response.status_code == 201
    result = response.json()
    if action_id:
        assigned = api.get('/api/actions/'+action_id).json()
        linked = api.post(f'/api/actions/{action_id}/runs/{result["id"]}/link', json={
            'action_version': assigned['version'], 'run_version': result['version']})
        assert linked.status_code == 200
        result = linked.json()['run']
    return result


def items(api):
    return api.get('/api/attention').json()['items']


def test_accepted_agent_gap_resolves_with_fresh_link_and_returns_when_stale(api, repo):
    a = action(api, title='Assigned fixture', execution_mode='agent', status='accepted')
    for mode, status in [('human', 'accepted'), ('agent', 'proposed'), ('agent', 'observed')]:
        action(api, execution_mode=mode, status=status)
    alert = items(api)[0]
    assert len(items(api)) == 1 and alert['progress'] == 'unknown'
    assert alert['evidence'][0]['timestamps']['updated_at'] == a['updated_at']
    r = run(api, a['id'])
    assert items(api) == []
    with repo.connection() as db:
        db.execute('UPDATE runs SET heartbeat_at=? WHERE id=?', ((datetime.now(timezone.utc)-timedelta(seconds=91)).isoformat(), r['id']))
        db.commit()
    assert items(api)[0]['id'] == alert['id']
    assert len(items(api)[0]['evidence']) == 2
    assert api.post('/api/actions/'+a['id']+'/transitions', json={'version': 1, 'transition': 'complete'}).status_code == 200
    assert items(api) == []


def test_approvals_coalesce_action_and_runs_and_remain_explicit_when_stale(api, repo):
    a = action(api, execution_mode='agent', status='accepted')
    api.post('/api/actions/'+a['id']+'/transitions', json={'version': 1, 'transition': 'request-approval'})
    run(api, a['id'], 'approval_needed', 'one')
    run(api, a['id'], 'approval_needed', 'two')
    assert len(items(api)) == 1
    alert = items(api)[0]
    assert alert['kind'] == 'approval_needed' and len(alert['evidence']) == 3
    with repo.connection() as db:
        db.execute("UPDATE runs SET heartbeat_at='2000-01-01T00:00:00+00:00'")
        db.commit()
    assert items(api)[0]['progress'] == 'unknown'
    assert 'stale' in items(api)[0]['reason']
    run(api, status='approval_needed', source_id='unlinked')
    assert len(items(api)) == 2


def test_collector_priority_dashboard_parity_and_read_only_access(api, repo):
    action(api, execution_mode='agent', status='accepted')
    run(api, status='approval_needed')
    api.post('/api/collectors/heartbeat', json=dict(source='fixture', instance_id='one', scope='fixture', status='degraded', reason='scan_failed'))
    result = items(api)
    assert [i['priority'] for i in result] == [0, 1, 2]
    assert result == api.get('/api/dashboard').json()['attention']['items']
    html = api.get('/').text
    for item in result:
        assert item['title'] in html and item['reason'] in html and item['next_action'] in html
    alert = result[1]
    assert alert['evidence'][0]['timestamps']['last_success_at'] is None
    with repo.connection() as db:
        db.execute("UPDATE collectors SET heartbeat_at='2000-01-01T00:00:00+00:00'")
        db.commit()
    assert items(api)[1]['id'] == alert['id']
    assert '90 seconds' in items(api)[1]['reason']
    api.post('/api/collectors/heartbeat', json=dict(source='fixture', instance_id='one', scope='fixture', status='ok', reason='scan_complete'))
    assert len(items(api)) == 2
    before = len(repo.list('events'))
    items(api)
    assert len(repo.list('events')) == before
    api.headers['Authorization'] = 'Bearer '+COLLECTOR
    assert api.get('/api/attention').status_code == 403
    api.headers.clear()
    assert api.get('/api/attention').status_code == 401


@pytest.mark.parametrize('provider', ['codex', 'claude', 'opencode', 'antigravity', 'ollama'])
def test_explicit_attention_reason_is_provider_agnostic(api, provider):
    r = run(api, status='approval_needed', provider=provider)
    alert = items(api)[0]
    assert alert['kind'] == 'approval_needed'
    assert alert['reason'] == 'An explicit approval request needs your decision.'
    assert alert['evidence'][0]['resource'] == 'runs'
    assert alert['evidence'][0]['id'] == r['id']
