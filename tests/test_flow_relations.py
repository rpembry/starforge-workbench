"""A local FLOW document can be projected without silently committing server work."""
from test_api import COLLECTOR, OPERATOR, action, api, repo
from workbench.repository import SQLiteRepository


WORK_ITEM = 'wi-' + 'a' * 32
SOURCE = 'https://example.com/work-items/plan'
REVISION = 'b' * 40
HASH = 'c' * 64


def publication(**overrides):
    return {
        'operation_id': 'publish-one', 'work_item_id': WORK_ITEM,
        'source_ref': SOURCE, 'document_revision': REVISION,
        'document_hash': HASH, 'task_ids': ['step-one'], **overrides,
    }


def test_projection_is_operator_only_and_does_not_create_commitments(api):
    body = publication()
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    assert api.post('/api/flow/work-items', json=body).status_code == 403
    assert api.get('/api/flow/work-items').status_code == 403
    api.headers.clear()
    assert api.post('/api/flow/work-items', json=body).status_code == 401
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    first = api.post('/api/flow/work-items', json=body)
    assert first.status_code == 201, first.text
    assert first.json()['status'] == 'created'
    assert first.json()['projection']['projection_state'] == 'proposed'
    assert first.json()['projection']['task_ids'] == ['step-one']
    assert first.json()['source_tracker_status'] == 'not_changed'
    assert api.get('/api/actions').json()['items'] == []
    assert api.get('/api/events').json()['items'] == []
    assert api.get('/api/flow/work-items').json()['items'][0]['work_item_id'] == WORK_ITEM


def test_publication_replay_conflict_and_definition_drift(api, repo):
    accepted = action(api, title='Review planned work', status='accepted',
                      source='manual', source_id='existing-commitment')
    body = publication()
    first = api.post('/api/flow/work-items', json=body).json()
    assert api.post('/api/flow/work-items', json=body).json() == first
    changed = api.post('/api/flow/work-items', json={**body, 'document_hash': 'd' * 64})
    assert changed.status_code == 409
    assert changed.json()['error']['code'] == 'operation_conflict'

    link = {'operation_id': 'link-one', 'work_item_version': 1,
            'action_version': accepted['version']}
    path = f'/api/flow/work-items/{WORK_ITEM}/actions/{accepted["id"]}/link'
    linked = api.post(path, json=link)
    assert linked.status_code == 200, linked.text
    assert linked.json()['status'] == 'linked'
    assert linked.json()['action_changed'] is False
    assert api.post(path, json=link).json() == linked.json()
    current = api.get('/api/actions/' + accepted['id']).json()
    assert current['status'] == 'accepted' and current['version'] == accepted['version']
    assert current['source'] == 'manual' and current['source_id'] == 'existing-commitment'

    next_body = publication(operation_id='publish-two', expected_version=1,
                            document_revision='e' * 40, document_hash='f' * 64)
    assert api.post('/api/flow/work-items', json=next_body).json()['status'] == 'updated'
    projection = api.get('/api/flow/work-items/' + WORK_ITEM).json()
    assert projection['version'] == 2
    assert projection['actions'][0]['definition_drift'] is True
    assert projection['actions'][0]['action_status'] == 'accepted'
    assert api.post(path, json={**link, 'operation_id': 'link-two',
                                'work_item_version': 2}).json()['error']['code'] == 'definition_drift'
    assert api.post('/api/flow/work-items', json=publication(
        operation_id='publish-three', expected_version=1,
        document_revision='a' * 40)).json()['error']['code'] == 'version_conflict'
    reopened = SQLiteRepository(repo.path)
    assert reopened.get_work_item(WORK_ITEM)['actions'][0]['definition_drift'] is True


def test_exact_links_versions_and_identity_guards(api):
    accepted = action(api, title='Unrelated similar title', status='accepted')
    first = api.post('/api/flow/work-items', json=publication())
    assert first.status_code == 201
    path = f'/api/flow/work-items/{WORK_ITEM}/actions/{accepted["id"]}/link'
    link = {'operation_id': 'link-stale', 'work_item_version': 1, 'action_version': 99}
    assert api.post(path, json=link).json()['error']['code'] == 'version_conflict'
    assert api.get('/api/flow/work-items/' + WORK_ITEM).json()['actions'] == []
    assert api.post(path, json={**link, 'operation_id': 'link-current',
                                'action_version': accepted['version']}).status_code == 200
    assert api.post('/api/flow/work-items', json=publication(
        operation_id='publish-new-source', expected_version=1,
        source_ref='https://example.org/work-items/other')).json()['error']['code'] == 'identity_conflict'
    assert api.post('/api/flow/work-items', json=publication(
        operation_id='publish-new-id', work_item_id='wi-' + 'd' * 32,
        source_ref=SOURCE)).json()['error']['code'] == 'identity_conflict'
    assert api.post('/api/flow/work-items', json=publication(
        operation_id='publish-bad-path', source_ref='/private/work/plan')).status_code == 422
    assert api.post('/api/flow/work-items', json=publication(
        operation_id='publish-bad-url', source_ref='https://user:secret@example.com/plan')).status_code == 422


def test_exact_relations_enrich_existing_commitment_without_double_counting(api):
    accepted = action(api, title='One commitment', status='accepted')
    assert len(api.get('/api/reports/todo').json()['plan']) == 1
    api.post('/api/flow/work-items', json=publication())
    api.post(f'/api/flow/work-items/{WORK_ITEM}/actions/{accepted["id"]}/link',
             json={'operation_id': 'link-report', 'work_item_version': 1,
                   'action_version': accepted['version']})
    report = api.get('/api/reports/todo').json()
    assert len(report['plan']) == 1
    assert report['plan'][0]['flow_links'][0]['work_item_id'] == WORK_ITEM
    assert report['plan'][0]['flow_links'][0]['freshness'] == 'last_published_only'
    dashboard = api.get('/api/dashboard').json()
    assert len(dashboard['next']) == 1
    assert dashboard['next'][0]['flow_links'][0]['definition_drift'] is False
    api.post('/api/flow/work-items', json=publication(
        operation_id='publish-report-update', expected_version=1,
        document_revision='d' * 40, document_hash='e' * 64))
    assert api.get('/api/reports/todo').json()['plan'][0]['flow_links'][0]['definition_drift'] is True


def test_explicit_operator_reconciliation_preserves_action_and_audits_scope(api):
    accepted = action(api, title='Review', status='accepted')
    api.post('/api/flow/work-items', json=publication())
    path = f'/api/flow/work-items/{WORK_ITEM}/actions/{accepted["id"]}'
    api.post(path + '/link', json={'operation_id': 'link-review', 'work_item_version': 1,
                                   'action_version': accepted['version']})
    api.post('/api/flow/work-items', json=publication(
        operation_id='publish-review', expected_version=1,
        document_revision='e' * 40, document_hash='f' * 64))
    body = {'operation_id': 'review-scope', 'work_item_version': 2,
            'action_version': accepted['version'], 'decision': 'accept_revised_definition',
            'reason': 'Reviewed changed task definition'}
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    assert api.post(path + '/reconcile', json=body).status_code == 403
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    assert api.post(path + '/reconcile', json={**body, 'work_item_version': 1}).status_code == 409
    result = api.post(path + '/reconcile', json=body)
    assert result.status_code == 200, result.text
    assert result.json()['projection']['actions'][0]['definition_drift'] is False
    assert api.post(path + '/reconcile', json=body).json() == result.json()
    assert api.post(path + '/reconcile', json={**body, 'reason': 'different'}).status_code == 409
    current = api.get('/api/actions/' + accepted['id']).json()
    assert current['version'] == accepted['version'] and current['status'] == 'accepted'
    assert len([e for e in api.get('/api/events').json()['items']
                if e['summary'] == 'FLOW revised definition accepted by operator']) == 1


def test_exact_code_commit_evidence_is_reported_not_verified_or_completed(api):
    accepted = action(api, title='Still active', status='accepted')
    api.post('/api/flow/work-items', json=publication())
    evidence = {'operation_id': 'evidence-one', 'work_item_version': 1,
                'repository_url': 'https://github.com/example/repo', 'commit_sha': 'a' * 40}
    path = f'/api/flow/work-items/{WORK_ITEM}/evidence'
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    assert api.post(path, json=evidence).status_code == 403
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    first = api.post(path, json=evidence)
    assert first.status_code == 200, first.text
    assert first.json()['evidence_verification'].startswith('caller_reported')
    assert api.post(path, json=evidence).json() == first.json()
    assert api.post(path, json={**evidence, 'commit_sha': 'b' * 40}).status_code == 409
    assert api.post(path, json={**evidence, 'operation_id': 'evidence-two'}).json()['status'] == 'existing'
    assert api.post(path, json={**evidence, 'operation_id': 'evidence-three',
                                'repository_url': 'javascript:alert(1)'}).status_code == 422
    projection = api.get('/api/flow/work-items/' + WORK_ITEM).json()
    assert len(projection['evidence_commits']) == 1
    assert projection['evidence_commits'][0]['provenance'] == 'operator_reported'
    assert api.get('/api/actions/' + accepted['id']).json()['status'] == 'accepted'
    assert not api.get('/api/reports/accomplishments').json()['accomplishments']


def test_version_eight_upgrade_keeps_existing_actions(api, repo):
    existing = action(api, title='Existing committed work', status='accepted')
    with repo.connection() as db:
        db.execute('DROP TABLE allowance_observations')
        db.execute('DROP TABLE flow_evidence_commits')
        db.execute('DROP TABLE flow_publication_ops')
        db.execute('DROP TABLE flow_action_links')
        db.execute('DROP TABLE flow_work_items')
        db.execute('DELETE FROM schema_migrations WHERE version IN (9,10,11,12,13)')
        db.commit()
    upgraded = SQLiteRepository(repo.path)
    assert upgraded.get('actions', existing['id'])['status'] == 'accepted'
    assert upgraded.list_work_items() == []
    with upgraded.connection() as db:
        assert [row[0] for row in db.execute(
            'SELECT version FROM schema_migrations ORDER BY version')] == list(range(1, 14))
