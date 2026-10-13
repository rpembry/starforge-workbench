from datetime import datetime, timedelta, timezone
import json

from test_api import COLLECTOR, OPERATOR, action, api, repo


def run(api, source_id='process-one', status='running'):
    body = dict(source='fixture', source_id=source_id, context='Fixture context',
                provider='codex', actor='Fixture agent', status=status,
                started_at=datetime.now(timezone.utc).isoformat())
    response = api.post('/api/runs', json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


def agent_action(api, title='Exact fixture action'):
    return action(api, title=title, status='accepted', execution_mode='agent')


def link(api, action_record, run_record, **extra):
    return api.post(f'/api/actions/{action_record["id"]}/runs/{run_record["id"]}/link', json={
        'action_version': action_record['version'], 'run_version': run_record['version'], **extra})


def test_exact_link_removes_false_attention_survives_heartbeat_and_audits(api):
    assigned = agent_action(api)
    observed, heartbeat = run(api)
    alert = next(item for item in api.get('/api/attention').json()['items'] if item['id'] == 'actions:'+assigned['id'])
    assert alert['kind'] == 'agent_without_active_run'

    response = link(api, assigned, observed)
    assert response.status_code == 200
    result = response.json()
    assert result['changed'] is True
    assert result['action']['id'] == assigned['id'] and result['action']['status'] == 'accepted'
    assert result['run']['id'] == observed['id'] and result['run']['status'] == 'running'
    assert result['run']['action_id'] == assigned['id'] and result['run']['version'] == 2
    evidence = json.loads(result['audit_event']['details'])
    assert evidence == {'action_id': assigned['id'], 'action_version': 1,
                        'previous_action_id': None, 'run_id': observed['id'], 'run_version': 2,
                        'run_source': 'fixture', 'run_source_id': 'process-one',
                        'run_started_at': observed['started_at']}
    assert not any(item['id'] == 'actions:'+assigned['id'] for item in api.get('/api/attention').json()['items'])

    api.headers['Authorization'] = 'Bearer '+COLLECTOR
    replay = api.post('/api/runs', json=heartbeat)
    assert replay.status_code == 201 and replay.json()['action_id'] == assigned['id']
    assert replay.json()['version'] == 3
    api.headers['Authorization'] = 'Bearer '+OPERATOR
    event = api.get('/api/events/'+result['audit_event']['id']).json()
    assert event['kind'] == 'decision' and event['action_id'] == assigned['id'] and event['run_id'] == observed['id']


def test_versions_exact_ids_and_explicit_reassignment(api):
    first = agent_action(api, 'First action')
    second = agent_action(api, 'Second action')
    observed, _ = run(api)
    linked = link(api, first, observed).json()['run']
    current_second = api.patch('/api/actions/'+second['id'], json={
        'version': second['version'], 'title': 'Current second action'}).json()
    assert link(api, second, linked).json()['error']['code'] == 'version_conflict'
    second = current_second
    assert link(api, second, linked).json()['error']['code'] == 'assignment_conflict'
    assert link(api, second, linked, replace_action_id='wrong').json()['error']['code'] == 'assignment_conflict'
    reassigned = link(api, second, linked, replace_action_id=first['id'])
    assert reassigned.status_code == 200
    result = reassigned.json()
    assert result['run']['action_id'] == second['id']
    assert json.loads(result['audit_event']['details'])['previous_action_id'] == first['id']
    current = result['run']
    unchanged = link(api, second, current)
    assert unchanged.status_code == 200 and unchanged.json()['changed'] is False
    assert link(api, second, {**current, 'version': current['version']-1}).json()['error']['code'] == 'version_conflict'
    assert api.post(f'/api/actions/missing/runs/{current["id"]}/link', json={
        'action_version': 1, 'run_version': current['version']}).status_code == 404
    assert api.post(f'/api/actions/{second["id"]}/runs/missing/link', json={
        'action_version': second['version'], 'run_version': 1}).status_code == 404


def test_stale_and_stopped_runs_reject_new_links_but_linked_history_is_truthful(api, repo):
    assigned = agent_action(api)
    stale, _ = run(api, 'stale')
    with repo.connection() as db:
        db.execute('UPDATE runs SET heartbeat_at=? WHERE id=?',
                   ((datetime.now(timezone.utc)-timedelta(seconds=91)).isoformat(), stale['id']))
        db.commit()
    assert link(api, assigned, stale).json()['error']['code'] == 'incompatible_run'
    stopped, _ = run(api, 'stopped', status='stopped')
    assert link(api, assigned, stopped).json()['error']['code'] == 'incompatible_run'

    fresh, _ = run(api, 'linked')
    linked = link(api, assigned, fresh).json()['run']
    with repo.connection() as db:
        db.execute('UPDATE runs SET heartbeat_at=? WHERE id=?',
                   ((datetime.now(timezone.utc)-timedelta(seconds=91)).isoformat(), linked['id']))
        db.commit()
    alert = next(item for item in api.get('/api/attention').json()['items'] if item['id'] == 'actions:'+assigned['id'])
    assert alert['progress'] == 'unknown' and 'failure is not established' in alert['reason']
    stopped_link = api.patch('/api/runs/'+linked['id'], json={'version': linked['version'], 'status': 'stopped'})
    assert stopped_link.status_code == 200 and stopped_link.json()['action_id'] == assigned['id']


def test_collector_cannot_assign_or_reassign_process_generations(api):
    assigned = agent_action(api)
    observed, heartbeat = run(api)
    api.headers['Authorization'] = 'Bearer '+COLLECTOR
    forbidden = api.post('/api/runs', json={**heartbeat, 'source_id': 'new-generation',
                                            'action_id': assigned['id']})
    assert forbidden.status_code == 409 and forbidden.json()['error']['code'] == 'explicit_link_required'
    assert link(api, assigned, observed).status_code == 403
    created = api.post('/api/runs', json={**heartbeat, 'source_id': 'new-generation'})
    assert created.status_code == 201 and created.json()['action_id'] is None
    api.headers['Authorization'] = 'Bearer '+OPERATOR
    first = link(api, assigned, observed).json()['run']
    newer = created.json()
    assert first['action_id'] == assigned['id'] and newer['action_id'] is None
    reconciled = link(api, assigned, newer)
    assert reconciled.status_code == 200 and reconciled.json()['run']['action_id'] == assigned['id']


def test_linking_requires_committed_agent_action(api):
    observed, _ = run(api)
    human = action(api, status='accepted', execution_mode='human')
    assert link(api, human, observed).json()['error']['code'] == 'incompatible_action'
    proposed = action(api, status='proposed', execution_mode='agent')
    assert link(api, proposed, observed).json()['error']['code'] == 'incompatible_action'
