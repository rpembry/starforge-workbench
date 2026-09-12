from datetime import datetime, timedelta, timezone

from test_api import COLLECTOR, OPERATOR, action, api, repo
from workbench.repository import SQLiteRepository


SOURCE = 'opencode-plugin'
INSTANCE = 'fixture-instance'


def stamp(seconds=0):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


def generation(api, session='ses_fixture', identity='msg_generation', started_at=None):
    return api.post('/api/provider-attention/generations', json={
        'provider': 'opencode', 'session_id': session, 'generation_id': identity,
        'source': SOURCE, 'source_instance': INSTANCE, 'started_at': started_at or stamp(),
        'provenance': 'opencode.chat.message'})


def observation(api, reason, sequence=1, session='ses_fixture', identity='msg_generation',
                observed_at=None, incident=None, state='open', provenance=None):
    provenance = provenance or {
        ('permission_wait', 'open'): 'opencode.permission.updated',
        ('permission_wait', 'resolved'): 'opencode.permission.replied',
        ('user_question', 'open'): 'opencode.tool.question',
        ('user_question', 'resolved'): 'opencode.question.completed',
        ('provider_error', 'open'): 'opencode.message.error',
    }[(reason, state)]
    return api.post('/api/provider-attention/observations', json={
        'provider': 'opencode', 'session_id': session, 'generation_id': identity,
        'source': SOURCE, 'source_instance': INSTANCE, 'incident_id': incident or f'{sequence:064x}',
        'sequence': sequence, 'observed_at': observed_at or stamp(sequence),
        'reason': reason, 'state': state, 'provenance': provenance})


def test_supported_reasons_show_fresh_provenance_without_completing_actions(api):
    accepted = action(api, execution_mode='agent', status='accepted')
    api.headers['Authorization'] = 'Bearer '+COLLECTOR
    assert generation(api).status_code == 201
    for sequence, reason, kind in [
        (1, 'permission_wait', 'provider_permission_wait'),
        (2, 'user_question', 'provider_user_question'),
        (3, 'provider_error', 'provider_error'),
    ]:
        response = observation(api, reason, sequence)
        assert response.status_code == 201
        api.headers['Authorization'] = 'Bearer '+OPERATOR
        item = next(item for item in api.get('/api/attention').json()['items'] if item['kind'] == kind)
        evidence = item['evidence'][0]
        assert evidence['provenance'].startswith('opencode.')
        assert evidence['timestamps']['fresh_until'] > evidence['timestamps']['observed_at']
        assert api.get('/api/actions/'+accepted['id']).json()['status'] == 'accepted'
        assert evidence['generation_id'] == 'msg_generation' and evidence['sequence'] == sequence
        assert evidence['provenance'] in api.get('/').text
        api.headers['Authorization'] = 'Bearer '+COLLECTOR


def test_generation_authority_rejects_duplicate_out_of_order_and_late_evidence(api):
    api.headers['Authorization'] = 'Bearer '+COLLECTOR
    first_started = stamp(-10)
    assert generation(api, identity='msg_old', started_at=first_started).status_code == 201
    assert generation(api, identity='msg_old', started_at=first_started).status_code == 201
    assert observation(api, 'permission_wait', identity='msg_old', observed_at=stamp(-9)).status_code == 201
    assert observation(api, 'permission_wait', identity='msg_old', observed_at=stamp(-8)).json()['error']['code'] == 'duplicate_observation'
    assert observation(api, 'provider_error', sequence=3, identity='msg_old', observed_at=stamp(-7)).json()['error']['code'] == 'out_of_order_observation'
    assert generation(api, identity='msg_new', started_at=stamp(-5)).status_code == 201
    late = observation(api, 'provider_error', sequence=2, identity='msg_old', observed_at=stamp(-4))
    assert late.status_code == 409 and late.json()['error']['code'] == 'stale_generation'
    replay = generation(api, identity='msg_old', started_at=first_started)
    assert replay.status_code == 409 and replay.json()['error']['code'] == 'stale_generation'
    assert observation(api, 'user_question', identity='msg_new', observed_at=stamp(-3)).status_code == 201


def test_unresolved_expired_hook_evidence_remains_truthfully_actionable(api, repo):
    assert api.get('/api/dashboard').json()['provider_attention'] == []
    api.headers['Authorization'] = 'Bearer '+COLLECTOR
    generation(api, started_at=stamp(-200))
    observation(api, 'provider_error', observed_at=stamp(-190))
    api.headers['Authorization'] = 'Bearer '+OPERATOR
    dashboard = api.get('/api/dashboard').json()
    assert dashboard['provider_attention'][0]['fresh'] is False
    item = next(item for item in dashboard['attention']['items'] if item['kind'] == 'provider_error')
    assert item['progress'] == 'unknown' and 'current status is unverified' in item['reason']


def test_distinct_incidents_repeat_and_explicit_resolution(api):
    api.headers['Authorization'] = 'Bearer '+COLLECTOR
    generation(api)
    first, second = 'a'*64, 'b'*64
    assert observation(api, 'permission_wait', incident=first).status_code == 201
    assert observation(api, 'permission_wait', sequence=2, incident=first).status_code == 201
    assert observation(api, 'permission_wait', sequence=3, incident=second).status_code == 201
    api.headers['Authorization'] = 'Bearer '+OPERATOR
    items = [item for item in api.get('/api/attention').json()['items'] if item['kind'] == 'provider_permission_wait']
    assert len(items) == 2 and len({item['id'] for item in items}) == 2
    api.headers['Authorization'] = 'Bearer '+COLLECTOR
    assert observation(api, 'permission_wait', sequence=4, incident=first, state='resolved').status_code == 201
    api.headers['Authorization'] = 'Bearer '+OPERATOR
    items = [item for item in api.get('/api/attention').json()['items'] if item['kind'] == 'provider_permission_wait']
    assert len(items) == 1 and items[0]['evidence'][0]['incident_id'] == second


def test_auth_boundaries_validation_and_schema_upgrade(api, repo):
    assert generation(api).status_code == 403
    api.headers.clear()
    assert generation(api).status_code == 401
    api.headers['Authorization'] = 'Bearer '+COLLECTOR
    generation(api)
    unsupported = api.post('/api/provider-attention/observations', json={
        'provider': 'opencode', 'session_id': 'ses_fixture', 'generation_id': 'msg_generation',
        'source': SOURCE, 'source_instance': INSTANCE, 'incident_id': 'a'*64, 'sequence': 1,
        'observed_at': stamp(1), 'reason': 'idle', 'state': 'open', 'provenance': 'opencode.session.idle'})
    body = {
        'provider': 'opencode', 'session_id': 'ses_fixture', 'generation_id': 'msg_generation',
        'source': SOURCE, 'source_instance': INSTANCE, 'incident_id': 'a'*64,
        'sequence': 1, 'observed_at': stamp(1), 'reason': 'permission_wait',
        'state': 'resolved', 'provenance': 'opencode.message.error'}
    assert api.post('/api/provider-attention/observations', json=body).status_code == 422
    assert unsupported.status_code == 422
    with repo.connection() as db:
        assert [row[0] for row in db.execute('SELECT version FROM schema_migrations ORDER BY version')] == [1, 2, 3, 4]
        db.execute('DROP TABLE provider_attention_incidents')
        db.execute('DROP TABLE provider_attention')
        db.execute('DELETE FROM schema_migrations WHERE version=4')
        db.commit()
    upgraded = SQLiteRepository(repo.path)
    with upgraded.connection() as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_attention'").fetchone()
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_attention_incidents'").fetchone()
        assert [row[0] for row in db.execute('SELECT version FROM schema_migrations ORDER BY version')] == [1, 2, 3, 4]
