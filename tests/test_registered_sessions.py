from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.main import create_app
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
    value = dict(id='registered_session_0001', host='synthetic-host', display_name='Synthetic OpenCode',
                 provider='opencode', evidence_state='present', reason='process_observed', summary='synthetic',
                 observation_sequence=1, observed_at=stamp())
    value.update(extra)
    return value


def test_registered_sessions_are_collector_owned_and_operator_read_only(api):
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    created = api.post('/api/registered-sessions', json=body())
    assert created.status_code == 201
    assert created.json()['visibility'] == 'fresh'
    assert api.get('/api/registered-sessions').status_code == 403
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    listed = api.get('/api/registered-sessions').json()['items']
    assert listed[0]['id'] == 'registered_session_0001'
    assert 'owner' not in listed[0]


def test_registered_session_rejects_takeover_bad_sequence_and_bad_clock(api, repo):
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    assert api.post('/api/registered-sessions', json=body()).status_code == 201
    assert api.post('/api/registered-sessions', json=body()).status_code == 409
    assert api.post('/api/registered-sessions', json=body(observation_sequence=2, observed_at=stamp(-1))).status_code == 409
    assert api.post('/api/registered-sessions', json=body(observation_sequence=2, observed_at=stamp(301))).status_code == 422
    with pytest.raises(Problem) as raised:
        repo.register_session(body(observation_sequence=2, observed_at=stamp(1)), 'different-collector-principal')
    assert getattr(raised.value, 'code', None) == 'registered_session_owner'


def test_registered_session_visibility_is_server_derived(api, repo):
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    assert api.post('/api/registered-sessions', json=body()).status_code == 201
    with repo.connection() as db:
        db.execute('UPDATE registered_sessions SET heartbeat_at=?', (stamp(-91),))
        db.commit()
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    assert api.get('/api/registered-sessions/registered_session_0001').json()['visibility'] == 'offline'
