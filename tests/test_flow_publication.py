"""Offline outbox and ambiguous API response recovery with synthetic state."""
import subprocess

import pytest

from starforge_workbench.flow import init_profile, open_item
from starforge_workbench.flow_publication import queue, replay
from starforge_workbench.flow_tasks import mutate

SOURCE = 'https://github.com/example-org/example-repo/issues/42'


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr('starforge_workbench.flow._inside_checkout', lambda root: False)
    path, root = tmp_path / 'private' / 'profile.json', tmp_path / 'metadata'
    init_profile(root, profile=path)
    subprocess.run(['git', '-C', str(root), 'config', 'user.name', 'Operator'], check=True)
    subprocess.run(['git', '-C', str(root), 'config', 'user.email', 'operator@example.com'], check=True)
    open_item(SOURCE, profile=path)
    mutate(SOURCE, 'add', profile=path, title='Review', operation_id='add-review')
    return path


class Transport:
    def __init__(self):
        self.current = None
        self.calls = 0
        self.fail_after_commit = False

    def get(self, identity):
        return self.current

    def publish(self, payload):
        self.calls += 1
        self.current = {**payload, 'version': (self.current or {}).get('version', 0) + 1}
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise ConnectionError('response lost')
        return {'projection': self.current}


def test_offline_replay_and_lost_response(profile):
    pending = queue(SOURCE, profile=profile, operation_id='publish-one')
    assert pending['status'] == 'publication_pending'
    assert queue(SOURCE, profile=profile, operation_id='publish-one') == pending
    transport = Transport()
    transport.fail_after_commit = True
    assert replay('publish-one', transport, profile=profile)['status'] == 'publication_pending'
    assert replay('publish-one', transport, profile=profile)['status'] == 'published'
    assert replay('publish-one', transport, profile=profile)['status'] == 'published'
    assert transport.calls == 2
    with pytest.raises(ValueError, match='different content'):
        queue(SOURCE, profile=profile, operation_id='publish-one', expected_version=1)


def test_stale_version_needs_reconciliation(profile):
    queue(SOURCE, profile=profile, operation_id='publish-two')
    transport = Transport()
    transport.current = {'version': 2, 'source_ref': SOURCE,
                         'document_revision': 'a' * 40, 'document_hash': 'b' * 64}
    result = replay('publish-two', transport, profile=profile)
    assert result['status'] == 'reconciliation_needed'
    assert result['reason'] == 'version_conflict'
    assert transport.calls == 0
