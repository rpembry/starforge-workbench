from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.main import create_app
from workbench.models import RegisteredSessionIn
from workbench.repository import Problem, SQLiteRepository


OPERATOR = 'operator-session-' + 'x' * 32
COLLECTOR = 'collector-session-' + 'y' * 32


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(tmp_path / 'state' / 'workbench.sqlite')


@pytest.fixture
def api(repo):
    with TestClient(create_app(repo, Auth({'operator': OPERATOR, 'collector': COLLECTOR}))) as client:
        client.headers['Authorization'] = 'Bearer ' + OPERATOR
        yield client


def stamp(seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def body(**extra):
    value = dict(id='registered_session_0001', collector_source='synthetic-collector',
                 host='synthetic-host', display_name='Synthetic OpenCode',
                 provider='opencode', evidence_state='present', reason='process_observed', summary='synthetic',
                 observation_sequence=1, observed_at=stamp())
    value.update(extra)
    return value


def register_host(api):
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    result = api.post('/api/collectors/heartbeat', json={
        'source': 'synthetic-collector', 'instance_id': 'synthetic-instance', 'scope': 'synthetic host',
        'status': 'ok', 'reason': 'scan_complete'})
    assert result.status_code == 200, result.text


def test_registered_sessions_are_collector_owned_and_operator_read_only(api):
    register_host(api)
    created = api.post('/api/registered-sessions', json=body())
    assert created.status_code == 201
    assert created.json()['visibility'] == 'fresh'
    assert api.get('/api/registered-sessions').status_code == 403
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    listed = api.get('/api/registered-sessions').json()['items']
    assert listed[0]['id'] == 'registered_session_0001'
    assert 'owner' not in listed[0]


def test_registered_session_rejects_takeover_bad_sequence_and_bad_clock(api, repo):
    register_host(api)
    assert api.post('/api/registered-sessions', json=body()).status_code == 201
    assert api.post('/api/registered-sessions', json=body()).status_code == 409
    assert api.post('/api/registered-sessions', json=body(observation_sequence=2, observed_at=stamp(-1))).status_code == 409
    assert api.post('/api/registered-sessions', json=body(observation_sequence=2, observed_at=stamp(301))).status_code == 422
    with pytest.raises(Problem) as raised:
        repo.register_session(body(observation_sequence=2, observed_at=stamp(1)), 'different-collector-principal')
    assert getattr(raised.value, 'code', None) == 'registered_session_owner'


def test_observation_clock_compares_instants_not_iso_strings(api, repo):
    register_host(api)
    def observation(**extra):
        return RegisteredSessionIn.model_validate(body(**extra)).model_dump(mode='json')

    first = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=5)
    repo.register_session(observation(observed_at=first.isoformat()), 'collector')
    later = first + timedelta(seconds=1)
    later_with_offset = later.astimezone(timezone(timedelta(hours=-4))).isoformat()
    second = observation(observation_sequence=2, observed_at=later_with_offset)
    assert repo.register_session(second, 'collector')['observation_sequence'] == 2
    with pytest.raises(Problem) as raised:
        equal_instant = observation(observation_sequence=3,
                                    observed_at=later.isoformat(timespec='microseconds'))
        repo.register_session(equal_instant, 'collector')
    assert raised.value.code == 'observation_clock'


def test_registered_session_visibility_is_server_derived(api, repo):
    register_host(api)
    assert api.post('/api/registered-sessions', json=body()).status_code == 201
    with repo.connection() as db:
        db.execute('UPDATE registered_sessions SET heartbeat_at=?', (stamp(-40),))
        db.commit()
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    assert api.get('/api/registered-sessions/registered_session_0001').json()['visibility'] == 'stale'
    with repo.connection() as db:
        db.execute('UPDATE collectors SET heartbeat_at=? WHERE source=?', (stamp(-91), 'synthetic-collector'))
        db.commit()
    assert api.get('/api/registered-sessions/registered_session_0001').json()['visibility'] == 'offline'


def test_registration_requires_owned_host_and_verified_action_link(api):
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    assert api.post('/api/registered-sessions', json=body()).status_code == 403
    register_host(api)
    assert api.post('/api/registered-sessions', json=body(action_id='unlinked')).status_code == 409
    assert api.post('/api/registered-sessions', json=body(collector_source='other')).status_code == 403
