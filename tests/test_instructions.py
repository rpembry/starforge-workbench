import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.main import create_app
from workbench.models import RegisteredSessionIn
from workbench.repository import Problem, SQLiteRepository


OPERATOR = 'operator-instruction-' + 'x' * 32
COLLECTOR = 'collector-instruction-' + 'y' * 32
SESSION_ID = 'instruction_session_0001'


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(tmp_path / 'state' / 'workbench.sqlite')


@pytest.fixture
def api(repo):
    with TestClient(create_app(repo, Auth({'operator': OPERATOR, 'collector': COLLECTOR}),
                               instruction_claims_enabled=True)) as client:
        client.headers['Authorization'] = 'Bearer ' + OPERATOR
        yield client


def stamp(seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def session_body(identity=SESSION_ID, **extra):
    value = dict(id=identity, collector_source='instruction-collector', host='synthetic-host',
                 display_name='Synthetic OpenCode', provider='opencode', evidence_state='present',
                 reason='process_observed', summary='synthetic', observation_sequence=1,
                 observed_at=stamp())
    value.update(extra)
    return value


def register_session(api, identity=SESSION_ID, **extra):
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    heartbeat = api.post('/api/collectors/heartbeat', json={
        'source': 'instruction-collector', 'instance_id': 'synthetic-instance',
        'scope': 'synthetic host', 'status': 'ok', 'reason': 'scan_complete'})
    assert heartbeat.status_code == 200, heartbeat.text
    created = api.post('/api/registered-sessions', json=session_body(identity, **extra))
    assert created.status_code == 201, created.text
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    return created.json()


def instruction_body(number=1, session_id=SESSION_ID, **extra):
    value = {'idempotency_key': f'synthetic-key-{number:04d}',
             'registered_session_id': session_id,
             'text': f'Synthetic instruction {number}', 'expiry_minutes': 15}
    value.update(extra)
    return value


def create_instruction(api, number=1, session_id=SESSION_ID, **extra):
    response = api.post('/api/instructions', json=instruction_body(number, session_id, **extra))
    assert response.status_code == 201, response.text
    return response.json()


def claim(api, session_id=SESSION_ID):
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    return api.post('/api/instructions/claim', json={'registered_session_id': session_id})


def test_create_is_bounded_idempotent_and_does_not_echo_invalid_text(api):
    register_session(api)
    body = instruction_body(text='  preserve $HOME; newline\nsecond line  ')
    first = api.post('/api/instructions', json=body)
    again = api.post('/api/instructions', json=body)
    assert first.status_code == again.status_code == 201
    assert first.json()['id'] == again.json()['id']
    assert first.json()['text'] == 'preserve $HOME; newline\nsecond line'
    assert api.post('/api/instructions', json={**body, 'text': 'changed'}).status_code == 409
    assert api.post('/api/instructions', json={**body, 'expiry_minutes': 16}).status_code == 409
    invalid = api.post('/api/instructions', json=instruction_body(2, text='DO_NOT_ECHO\x00'))
    assert invalid.status_code == 422
    assert 'DO_NOT_ECHO' not in invalid.text
    assert api.post('/api/instructions', json=instruction_body(3, text='x' * 2001)).status_code == 422


def test_roles_are_separated_and_worker_reads_only_by_claim(api):
    register_session(api)
    item = create_instruction(api)
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    assert api.get('/api/instructions').status_code == 403
    assert api.get('/api/instructions/' + item['id']).status_code == 403
    assert api.post('/api/instructions', json=instruction_body(2)).status_code == 403
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    assert api.post('/api/instructions/claim', json={'registered_session_id': SESSION_ID}).status_code == 403
    api.headers.clear()
    assert api.get('/api/instructions').status_code == 401
    assert api.post('/api/instructions/claim', json={'registered_session_id': SESSION_ID}).status_code == 401


@pytest.mark.parametrize('change,code', [
    ({'evidence_state': 'unknown'}, 'target_uncontrollable'),
    ({'evidence_state': 'stopped'}, 'target_uncontrollable'),
    ({'provider': 'other'}, 'target_uncontrollable'),
])
def test_create_rejects_uncontrollable_targets(api, change, code):
    register_session(api, **change)
    response = api.post('/api/instructions', json=instruction_body())
    assert response.status_code == 409
    assert response.json()['error']['code'] == code
    assert api.post('/api/instructions', json=instruction_body(session_id='missing_session_001')).status_code == 404


def test_create_rejects_stale_and_offline_targets(api, repo):
    register_session(api)
    with repo.connection() as db:
        db.execute('UPDATE registered_sessions SET heartbeat_at=?', (stamp(-31),))
        db.commit()
    assert api.post('/api/instructions', json=instruction_body()).json()['error']['code'] == 'target_unavailable'
    with repo.connection() as db:
        db.execute('UPDATE registered_sessions SET heartbeat_at=?, evidence_state=?', (stamp(), 'present'))
        db.execute('UPDATE collectors SET heartbeat_at=?', (stamp(-91),))
        db.commit()
    assert api.post('/api/instructions', json=instruction_body()).json()['error']['code'] == 'target_unavailable'


def test_claim_retry_receipt_response_and_audit_boundaries(api, repo):
    register_session(api)
    secret = 'SYNTHETIC PRIVATE INSTRUCTION'
    item = create_instruction(api, text=secret)
    first_claim = claim(api)
    assert first_claim.status_code == 200
    assert first_claim.json()['text'] == secret
    first_token = first_claim.json()['lease_token']
    renewed = api.post(f'/api/instructions/{item["id"]}/renew', json={'lease_token': first_token})
    assert renewed.status_code == 200
    assert 'text' not in renewed.json() and 'lease_token' not in renewed.json()
    released = api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': first_token, 'outcome': 'retryable', 'reason_code': 'provider_unavailable'})
    assert released.json()['state'] == 'queued'
    second_claim = claim(api)
    assert second_claim.status_code == 200
    second_token = second_claim.json()['lease_token']
    assert second_token != first_token
    replay = api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': first_token, 'outcome': 'received', 'reason_code': 'provider_accepted'})
    assert replay.status_code == 409
    received = api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': second_token, 'outcome': 'received', 'reason_code': 'provider_accepted'})
    assert received.json()['state'] == 'received'
    responded = api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': second_token, 'outcome': 'responded',
        'reason_code': 'provider_response_without_error'})
    assert responded.json()['state'] == 'responded'
    assert api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': second_token, 'outcome': 'responded',
        'reason_code': 'provider_response_without_error'}).status_code == 409
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    history = api.get('/api/instructions/' + item['id']).json()['history']
    assert [entry['state'] for entry in history] == ['queued', 'claimed', 'queued', 'claimed', 'received', 'responded']
    assert secret not in str(history)
    assert all('complete' not in entry['reason_code'] for entry in history)
    with repo.connection() as db:
        audit = '\n'.join(str(dict(row)) for row in db.execute('SELECT * FROM instruction_audit'))
        assert secret not in audit
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM instruction_audit')


def test_invalid_transitions_and_reason_pairs_fail_closed(api):
    register_session(api)
    item = create_instruction(api)
    claimed = claim(api).json()
    bad_reason = api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': claimed['lease_token'], 'outcome': 'received', 'reason_code': 'provider_rejected'})
    assert bad_reason.status_code == 422
    too_early = api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': claimed['lease_token'], 'outcome': 'responded',
        'reason_code': 'provider_response_error'})
    assert too_early.status_code == 409
    failed = api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': claimed['lease_token'], 'outcome': 'failed', 'reason_code': 'provider_rejected'})
    assert failed.json()['state'] == 'failed'
    assert api.post(f'/api/instructions/{item["id"]}/renew',
                    json={'lease_token': claimed['lease_token']}).status_code == 409


def test_expiry_and_abandoned_lease_are_terminal_without_redelivery(api, repo):
    register_session(api)
    queued = create_instruction(api, 1)
    with repo.connection() as db:
        db.execute('UPDATE instructions SET expires_at=? WHERE id=?', (stamp(-1), queued['id']))
        db.commit()
    assert claim(api).status_code == 404
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    assert api.get('/api/instructions/' + queued['id']).json()['state'] == 'expired'

    active = create_instruction(api, 2)
    claimed = claim(api).json()
    with repo.connection() as db:
        db.execute('UPDATE instructions SET lease_until=? WHERE id=?', (stamp(-1), active['id']))
        db.commit()
    assert claim(api).status_code == 404
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    assert api.get('/api/instructions/' + active['id']).json()['state'] == 'uncertain'
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    late = api.post(f'/api/instructions/{active["id"]}/results', json={
        'lease_token': claimed['lease_token'], 'outcome': 'received', 'reason_code': 'provider_accepted'})
    assert late.status_code == 409


def test_offline_queue_survives_until_owned_worker_recovers(api, repo):
    register_session(api)
    item = create_instruction(api)
    with repo.connection() as db:
        db.execute('UPDATE collectors SET heartbeat_at=?', (stamp(-91),))
        db.commit()
    unavailable = claim(api)
    assert unavailable.status_code == 409
    assert unavailable.json()['error']['code'] == 'target_unavailable'
    heartbeat = api.post('/api/collectors/heartbeat', json={
        'source': 'instruction-collector', 'instance_id': 'synthetic-instance',
        'scope': 'synthetic host', 'status': 'ok', 'reason': 'scan_complete'})
    assert heartbeat.status_code == 200
    recovered = claim(api)
    assert recovered.status_code == 200
    assert recovered.json()['id'] == item['id']


def test_queue_survives_server_restart_and_expires_while_worker_offline(tmp_path):
    database = tmp_path / 'restart-state' / 'workbench.sqlite'
    original = SQLiteRepository(database)
    auth = Auth({'operator': OPERATOR, 'collector': COLLECTOR})
    with TestClient(create_app(original, auth, instruction_claims_enabled=True)) as api:
        api.headers['Authorization'] = 'Bearer ' + OPERATOR
        register_session(api)
        item = create_instruction(api)

    restarted = SQLiteRepository(database)
    with restarted.connection() as db:
        db.execute('UPDATE collectors SET heartbeat_at=?', (stamp(-91),))
        db.execute('UPDATE instructions SET expires_at=? WHERE id=?', (stamp(-1), item['id']))
        db.commit()
    with TestClient(create_app(restarted, auth, instruction_claims_enabled=True)) as api:
        api.headers['Authorization'] = 'Bearer ' + COLLECTOR
        unavailable = claim(api)
        assert unavailable.status_code == 409
        assert unavailable.json()['error']['code'] == 'target_unavailable'
        api.headers['Authorization'] = 'Bearer ' + OPERATOR
        stored = api.get('/api/instructions/' + item['id']).json()
        assert stored['state'] == 'expired'
        assert [entry['state'] for entry in stored['history']] == ['queued', 'expired']


def test_foreign_worker_cannot_read_claim_or_report(repo):
    principal = 'owner-collector'
    repo.collector_heartbeat({'source': 'owned-source', 'instance_id': 'instance', 'scope': 'host',
                              'status': 'ok', 'reason': 'scan_complete', 'observed_runs': 0}, principal)
    session = RegisteredSessionIn.model_validate(session_body(
        collector_source='owned-source')).model_dump(mode='json')
    repo.register_session(session, principal)
    created = repo.create_instruction(instruction_body(), 'operator')
    with pytest.raises(Problem) as raised:
        repo.claim_instruction(SESSION_ID, 'foreign-collector', True)
    assert raised.value.code == 'not_found'
    claimed = repo.claim_instruction(SESSION_ID, principal, True)
    with pytest.raises(Problem) as raised:
        repo.report_instruction_result(created['id'], {
            'lease_token': claimed['lease_token'], 'outcome': 'received',
            'reason_code': 'provider_accepted'}, 'foreign-collector')
    assert raised.value.code == 'not_found'


def test_competing_claims_have_one_winner(api, repo):
    register_session(api)
    item = create_instruction(api)

    def attempt():
        try:
            return repo.claim_instruction(SESSION_ID, 'collector', True)['id']
        except Problem as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert results.count(item['id']) == 1
    assert results.count('no_instruction') == 1


def test_creation_rate_limits_are_transactional_and_idempotent(api):
    register_session(api)
    for number in range(1, 6):
        create_instruction(api, number)
    limited_body = instruction_body(6)
    limited = api.post('/api/instructions', json=limited_body)
    assert limited.status_code == 429
    assert limited.json()['error']['code'] == 'session_rate_limited'
    assert 'Synthetic instruction 6' not in limited.text
    replay = api.post('/api/instructions', json=instruction_body(1))
    assert replay.status_code == 201

    for index in (2, 3):
        session_id = f'instruction_session_000{index}'
        register_session(api, session_id)
        count = 5 if index == 2 else 1
        for offset in range(count):
            response = api.post('/api/instructions', json=instruction_body(
                index * 100 + offset, session_id=session_id))
            if index == 3:
                assert response.status_code == 429
                assert response.json()['error']['code'] == 'principal_rate_limited'
            else:
                assert response.status_code == 201


def test_claim_kill_switch_preserves_queue(repo):
    principal = 'collector'
    repo.collector_heartbeat({'source': 'owned-source', 'instance_id': 'instance', 'scope': 'host',
                              'status': 'ok', 'reason': 'scan_complete', 'observed_runs': 0}, principal)
    session = RegisteredSessionIn.model_validate(session_body(
        collector_source='owned-source')).model_dump(mode='json')
    repo.register_session(session, principal)
    created = repo.create_instruction(instruction_body(), 'operator')
    with pytest.raises(Problem) as raised:
        repo.claim_instruction(SESSION_ID, principal)
    assert raised.value.code == 'instruction_claims_disabled'
    assert repo.get_instruction(created['id'])['state'] == 'queued'


def test_kill_switch_blocks_renewal_but_accepts_delivery_evidence(api, repo):
    register_session(api)
    item = create_instruction(api)
    claimed = claim(api).json()
    with pytest.raises(Problem) as raised:
        repo.renew_instruction_claim(item['id'], claimed['lease_token'], 'collector')
    assert raised.value.code == 'instruction_claims_disabled'
    reported = repo.report_instruction_result(item['id'], {
        'lease_token': claimed['lease_token'], 'outcome': 'received',
        'reason_code': 'provider_accepted'}, 'collector')
    assert reported['state'] == 'received'


def test_duplicate_result_race_records_one_transition(api, repo):
    register_session(api)
    item = create_instruction(api)
    claimed = claim(api).json()
    payload = {'lease_token': claimed['lease_token'], 'outcome': 'received',
               'reason_code': 'provider_accepted'}

    def report():
        try:
            return repo.report_instruction_result(item['id'], payload, 'collector')['state']
        except Problem as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: report(), range(2)))
    assert results.count('received') == 1
    assert results.count('invalid_transition') == 1
    assert [entry['state'] for entry in repo.get_instruction(item['id'])['history']].count('received') == 1


def test_upgrade_from_version_seven_preserves_registered_sessions(api, repo):
    register_session(api)
    with repo.connection() as db:
        db.execute('DROP TABLE instruction_audit')
        db.execute('DROP TABLE instructions')
        db.execute('DELETE FROM schema_migrations WHERE version=8')
        db.commit()
    upgraded = SQLiteRepository(repo.path)
    assert upgraded.get_registered_session(SESSION_ID)['display_name'] == 'Synthetic OpenCode'
    with upgraded.connection() as db:
        assert [row[0] for row in db.execute(
            'SELECT version FROM schema_migrations ORDER BY version')] == list(range(1, 9))
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='instructions'").fetchone()
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='instruction_audit'").fetchone()


def test_stopped_registration_cannot_retarget_queued_instruction(api):
    registered = register_session(api)
    item = create_instruction(api)
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    stopped = api.post('/api/registered-sessions', json=session_body(
        evidence_state='stopped', reason='process_stopped', observation_sequence=2,
        observed_at=stamp(1)))
    assert stopped.status_code == 201
    revived = api.post('/api/registered-sessions', json=session_body(
        observation_sequence=3, observed_at=stamp(2)))
    assert revived.status_code == 409
    assert revived.json()['error']['code'] == 'stopped_registration'
    unavailable = api.post('/api/instructions/claim', json={'registered_session_id': registered['id']})
    assert unavailable.status_code == 409
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    assert api.get('/api/instructions/' + item['id']).json()['state'] == 'queued'


def test_response_preview_relays_live_and_never_persists(api):
    register_session(api)
    item = create_instruction(api)
    claimed = claim(api)
    token = claimed.json()['lease_token']
    admitted = api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': token, 'outcome': 'received', 'reason_code': 'provider_accepted'})
    assert admitted.status_code == 200

    subscriber = api.app.state.response_preview.subscribe()
    relayed = api.post(f'/api/instructions/{item["id"]}/response-preview', json={
        'lease_token': token, 'outcome': 'provider_response_without_error',
        'excerpt': 'SYNTHETIC LIVE EXCERPT'})
    assert relayed.status_code == 202
    assert relayed.json() == {'status': 'relayed'}
    assert subscriber.get_nowait() == {
        'instruction_id': item['id'], 'registered_session_id': SESSION_ID,
        'outcome': 'provider_response_without_error',
        'excerpt': 'SYNTHETIC LIVE EXCERPT'}

    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    stored = api.get('/api/instructions/' + item['id'])
    assert 'SYNTHETIC LIVE EXCERPT' not in stored.text


def test_response_preview_reports_dropped_without_a_subscriber(api):
    register_session(api)
    item = create_instruction(api)
    claimed = claim(api)
    token = claimed.json()['lease_token']
    api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': token, 'outcome': 'received', 'reason_code': 'provider_accepted'})

    response = api.post(f'/api/instructions/{item["id"]}/response-preview', json={
        'lease_token': token, 'outcome': 'provider_response_without_error',
        'excerpt': 'no one is watching'})
    assert response.status_code == 202
    assert response.json() == {'status': 'dropped'}


def test_response_preview_requires_matching_lease_and_collector_role(api):
    register_session(api)
    item = create_instruction(api)
    claimed = claim(api)
    token = claimed.json()['lease_token']

    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    forbidden = api.post(f'/api/instructions/{item["id"]}/response-preview', json={
        'lease_token': token, 'outcome': 'provider_response_without_error', 'excerpt': 'x'})
    assert forbidden.status_code == 403

    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    wrong_token = api.post(f'/api/instructions/{item["id"]}/response-preview', json={
        'lease_token': 'z' * 32, 'outcome': 'provider_response_without_error', 'excerpt': 'x'})
    assert wrong_token.status_code == 409

    missing = api.post('/api/instructions/missing_instruction_0001/response-preview', json={
        'lease_token': token, 'outcome': 'provider_response_without_error', 'excerpt': 'x'})
    assert missing.status_code == 404


def test_response_preview_is_allowed_only_between_received_and_responded(api):
    register_session(api)
    item = create_instruction(api)
    claimed = claim(api).json()
    payload = {'lease_token': claimed['lease_token'],
               'outcome': 'provider_response_without_error', 'excerpt': 'x'}

    before_received = api.post(
        f'/api/instructions/{item["id"]}/response-preview', json=payload)
    assert before_received.status_code == 409
    api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': claimed['lease_token'], 'outcome': 'received',
        'reason_code': 'provider_accepted'})
    assert api.post(
        f'/api/instructions/{item["id"]}/response-preview', json=payload).status_code == 202
    api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': claimed['lease_token'], 'outcome': 'responded',
        'reason_code': 'provider_response_without_error'})
    after_responded = api.post(
        f'/api/instructions/{item["id"]}/response-preview', json=payload)
    assert after_responded.status_code == 409


def test_response_preview_bounds_excerpt_and_rejects_control_characters(api):
    register_session(api)
    item = create_instruction(api)
    claimed = claim(api)
    token = claimed.json()['lease_token']
    api.post(f'/api/instructions/{item["id"]}/results', json={
        'lease_token': token, 'outcome': 'received', 'reason_code': 'provider_accepted'})

    too_long = api.post(f'/api/instructions/{item["id"]}/response-preview', json={
        'lease_token': token, 'outcome': 'provider_response_without_error', 'excerpt': 'x' * 501})
    assert too_long.status_code == 422

    control = api.post(f'/api/instructions/{item["id"]}/response-preview', json={
        'lease_token': token, 'outcome': 'provider_response_without_error',
        'excerpt': 'DO_NOT_ECHO\x00'})
    assert control.status_code == 422
    assert 'DO_NOT_ECHO' not in control.text


def test_preview_stream_requires_operator_role(api):
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    assert api.get('/api/instructions/preview-stream').status_code == 403
    api.headers.clear()
    assert api.get('/api/instructions/preview-stream').status_code == 401
