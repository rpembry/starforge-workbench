"""Synthetic FLOW capture, interruption, publication, review, and recovery journey."""
import subprocess

from test_api import action, api, repo
from starforge_workbench.flow import init_profile, open_item, show
from starforge_workbench.flow_history import snapshot
from starforge_workbench.flow_packets import build_packet
from starforge_workbench.flow_publication import queue, replay
from starforge_workbench.flow_source import normalize_github, normalize_jira, record, status
from starforge_workbench.flow_tasks import inspect, mutate

GITHUB = 'https://github.com/example-org/repo/issues/42'
JIRA = 'https://issues.example.org/browse/DEMO-12'


class API:
    def __init__(self, client):
        self.client = client

    def get(self, identity):
        response = self.client.get('/api/flow/work-items/' + identity)
        return None if response.status_code == 404 else response.json()

    def publish(self, payload):
        response = self.client.post('/api/flow/work-items', json=payload)
        assert response.status_code == 201, response.text
        return response.json()


def test_end_to_end_interruption_and_exact_scope_review(api, tmp_path, monkeypatch):
    monkeypatch.setattr('starforge_workbench.flow._inside_checkout', lambda root: False)
    profile, root = tmp_path / 'private' / 'profile.json', tmp_path / 'metadata'
    init_profile(root, profile=profile, jira_sites={'demo': 'https://issues.example.org'})
    subprocess.run(['git', '-C', str(root), 'config', 'user.name', 'Operator'], check=True)
    subprocess.run(['git', '-C', str(root), 'config', 'user.email', 'operator@example.org'], check=True)
    gh = open_item(GITHUB, profile=profile)
    jira = open_item(JIRA, profile=profile)
    assert open_item('jira:demo:DEMO-12', profile=profile)['work_item_id'] == jira['work_item_id']
    assert open_item(GITHUB, profile=profile)['work_item_id'] == gh['work_item_id']
    assert not (root / '.git' / 'worktrees').exists()
    assert not api.get('/api/flow/work-items').json()['items']

    record(GITHUB, normalize_github({'id': 42, 'html_url': GITHUB, 'title': 'Example',
        'state': 'open', 'updated_at': '2026-09-26T12:00:00Z'}, {'id': 77}), profile=profile)
    record(JIRA, normalize_jira({'id': '123', 'key': 'DEMO-12', 'fields': {
        'summary': 'Example', 'status': {'name': 'Open'},
        'updated': '2026-09-26T12:00:00Z'}}, 'https://issues.example.org'), profile=profile)
    assert status(JIRA, profile=profile, error='unavailable')['source']['status'] == 'Open'
    first = mutate(GITHUB, 'add', title='Review implementation', acceptance='Run tests',
                   operation_id='add-test', profile=profile)
    task_id = first['task_id']
    revised = mutate(GITHUB, 'update', task_id=task_id, acceptance='Run tests and review',
                     operation_id='revise-test', profile=profile)
    assert inspect(GITHUB, 'show', task_id=task_id, profile=profile)['task']['fields']['Acceptance'] == 'Run tests and review'
    packet = build_packet(GITHUB, profile=profile)
    assert packet['next_suggestion']['task_id'] == task_id
    assert packet['metadata_revision'] == revised['after_revision']
    assert 'conversation' not in str(packet).lower()

    pending = queue(GITHUB, profile=profile, operation_id='publish-journey')
    assert pending['status'] == 'publication_pending'
    publication = replay('publish-journey', API(api), profile=profile)
    assert publication['status'] == 'published'
    assert replay('publish-journey', API(api), profile=profile) == publication
    accepted = action(api, title='Review planned implementation', status='accepted')
    path = f'/api/flow/work-items/{gh["work_item_id"]}/actions/{accepted["id"]}'
    assert api.post(path + '/link', json={'operation_id': 'link-journey', 'work_item_version': 1,
        'action_version': accepted['version']}).status_code == 200
    assert len(api.get('/api/reports/todo').json()['plan']) == 1

    mutate(GITHUB, 'complete', task_id=task_id, evidence='Synthetic test result',
           operation_id='complete-test', profile=profile)
    history = inspect(GITHUB, 'history', task_id=task_id, profile=profile)['entries']
    assert history[0]['operation'] == 'complete'
    assert inspect(GITHUB, 'show', task_id=task_id, profile=profile)['status'] == 'complete'
    assert snapshot(GITHUB, profile=profile)['revision'] != revised['after_revision']
    assert api.get('/api/actions/' + accepted['id']).json()['status'] == 'accepted'
    assert api.get('/api/flow/work-items/' + gh['work_item_id']).json()['document_revision'] == revised['after_revision']
    assert status(GITHUB, profile=profile)['source']['status'] == 'open'
    next_publication = queue(GITHUB, profile=profile, operation_id='publish-completion', expected_version=1)
    assert replay(next_publication['operation_id'], API(api), profile=profile)['status'] == 'published'
    assert api.get('/api/flow/work-items/' + gh['work_item_id']).json()['actions'][0]['definition_drift']
    assert api.get('/api/actions/' + accepted['id']).json()['status'] == 'accepted'
