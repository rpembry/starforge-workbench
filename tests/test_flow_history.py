from pathlib import Path
import subprocess
import json
import fcntl

import pytest

from starforge_workbench.flow import init_profile, open_item
from starforge_workbench.flow_history import mutate_document, snapshot


SOURCE = 'https://github.com/example-org/example-repo/issues/42'


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr('starforge_workbench.flow._inside_checkout', lambda root: False)
    profile, root = tmp_path / 'private' / 'profile.json', tmp_path / 'metadata'
    init_profile(root, profile=profile)
    subprocess.run(['git', '-C', str(root), 'config', 'user.name', 'Example Operator'], check=True)
    subprocess.run(['git', '-C', str(root), 'config', 'user.email', 'operator@example.com'], check=True)
    open_item(SOURCE, profile=profile)
    return profile, root


def change(profile, proposal, operation='add', operation_id='operation-01', **kwargs):
    state = snapshot(SOURCE, profile=profile)
    return mutate_document(SOURCE, profile=profile, operation_id=operation_id,
                           expected_revision=state['revision'], expected_hash=state['document_hash'],
                           proposed=proposal(state['document']), operation=operation, actor='Example Operator',
                           **kwargs)


def test_add_material_edit_complete_retains_definition_after_gc(workspace):
    profile, root = workspace
    added = change(profile, lambda text: text + '\n## P1\n\n- [ ] Repair parser\n  - **ID**: repair-01\n  - **Acceptance**: Basic check.\n')
    assert added['checkpoint'] != 'UNBORN'
    document = root / 'work/github/github.com/example-org/example-repo/issue-42/TASKS.md'
    document.write_text(document.read_text().replace('Basic check.', 'Reviewed regression check.'))
    current = snapshot(SOURCE, profile=profile)
    result = mutate_document(SOURCE, profile=profile, operation_id='complete-01',
        expected_revision=current['revision'], expected_hash=current['document_hash'],
        proposed=current['document'].split('\n## P1')[0] + '\n', operation='complete',
        task_id='repair-01', actor='Example Operator', evidence='PR example/reviewed',
        checkpoint_pending=True)
    assert result['definition_checkpoint']
    subprocess.run(['git', '-C', str(root), 'gc', '--prune=now'], check=True)
    saved = subprocess.run(['git', '-C', str(root), 'show', result['definition_checkpoint'] + ':work/github/github.com/example-org/example-repo/issue-42/TASKS.md'],
                           capture_output=True, text=True, check=True).stdout
    assert 'Reviewed regression check.' in saved
    assert 'Repair parser' not in document.read_text()
    assert result['publication'] == 'pending'


def test_staged_unrelated_work_refused_and_unchanged(workspace):
    profile, root = workspace
    unrelated = root / 'local-note.txt'
    unrelated.write_text('Do not include this note')
    subprocess.run(['git', '-C', str(root), 'add', '-f', 'local-note.txt'], check=True)
    with pytest.raises(ValueError, match='staged content'):
        change(profile, lambda text: text + '\n## P1\n\n- [ ] Add task\n')
    staged = subprocess.run(['git', '-C', str(root), 'diff', '--cached', '--name-only'], capture_output=True, text=True, check=True).stdout
    assert staged.strip() == 'local-note.txt'


def test_stale_revision_refused_without_write(workspace):
    profile, root = workspace
    state = snapshot(SOURCE, profile=profile)
    with pytest.raises(ValueError, match='Stale'):
        mutate_document(SOURCE, profile=profile, operation_id='operation-01',
            expected_revision='bad', expected_hash=state['document_hash'],
            proposed=state['document'] + '\n## P2\n\n- [ ] Task\n', operation='add', actor='Example Operator')
    assert snapshot(SOURCE, profile=profile)['document'] == state['document']


def test_hook_failure_is_not_completion(workspace):
    profile, root = workspace
    hook = root / '.git/hooks/pre-commit'
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text('#!/bin/sh\nexit 1\n')
    hook.chmod(0o700)
    with pytest.raises(ValueError, match='Git commit failed'):
        change(profile, lambda text: text + '\n## P1\n\n- [ ] Task\n')
    assert snapshot(SOURCE, profile=profile)['revision'] == 'UNBORN'
    assert 'operation-01' in profile.with_suffix('.operations.json').read_text()
    hook.unlink()
    state = snapshot(SOURCE, profile=profile)
    recovered = mutate_document(SOURCE, profile=profile, operation_id='operation-01',
        expected_revision='UNBORN', expected_hash=json.loads(profile.with_suffix('.operations.json').read_text())['operations']['operation-01']['before_hash'],
        proposed=state['document'], operation='add', actor='Example Operator')
    assert recovered['checkpoint'] == snapshot(SOURCE, profile=profile)['revision']


def test_commit_then_journal_failure_retries_without_duplicate(workspace, monkeypatch):
    profile, root = workspace
    from starforge_workbench import flow_history
    real_save = flow_history._save
    calls = 0
    def interrupted(path, state):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('simulated crash after Git commit')
        return real_save(path, state)
    before = snapshot(SOURCE, profile=profile)
    proposal = before['document'] + '\n## P1\n\n- [ ] Add task\n  - **ID**: add-01\n'
    monkeypatch.setattr(flow_history, '_save', interrupted)
    with pytest.raises(OSError, match='simulated crash'):
        mutate_document(SOURCE, profile=profile, operation_id='crash-01',
            expected_revision=before['revision'], expected_hash=before['document_hash'],
            proposed=proposal, operation='add', actor='Example Operator')
    monkeypatch.setattr(flow_history, '_save', real_save)
    committed = snapshot(SOURCE, profile=profile)['revision']
    result = mutate_document(SOURCE, profile=profile, operation_id='crash-01',
        expected_revision=before['revision'], expected_hash=before['document_hash'],
        proposed=proposal, operation='add', actor='Example Operator')
    assert result['checkpoint'] == committed
    assert subprocess.run(['git', '-C', str(root), 'rev-list', '--count', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip() == '1'
    with pytest.raises(ValueError, match='different content'):
        mutate_document(SOURCE, profile=profile, operation_id='crash-01',
            expected_revision=before['revision'], expected_hash=before['document_hash'],
            proposed=proposal + 'different', operation='add', actor='Example Operator')


def test_definition_commit_then_journal_failure_retries(workspace, monkeypatch):
    profile, root = workspace
    from starforge_workbench import flow_history
    change(profile, lambda text: text + '\n## P1\n\n- [ ] Task\n  - **ID**: task-01\n')
    document = root / 'work/github/github.com/example-org/example-repo/issue-42/TASKS.md'
    document.write_text(document.read_text().replace('Task', 'Edited task'))
    before = snapshot(SOURCE, profile=profile)
    proposal = before['document'].split('\n## P1')[0] + '\n'
    real_save = flow_history._save
    calls = 0
    def interrupted(path, state):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('simulated definition boundary')
        return real_save(path, state)
    monkeypatch.setattr(flow_history, '_save', interrupted)
    with pytest.raises(OSError, match='definition boundary'):
        mutate_document(SOURCE, profile=profile, operation_id='remove-01',
            expected_revision=before['revision'], expected_hash=before['document_hash'],
            proposed=proposal, operation='complete', task_id='task-01',
            actor='Example Operator', checkpoint_pending=True)
    monkeypatch.setattr(flow_history, '_save', real_save)
    result = mutate_document(SOURCE, profile=profile, operation_id='remove-01',
        expected_revision=before['revision'], expected_hash=before['document_hash'],
        proposed=proposal, operation='complete', task_id='task-01',
        actor='Example Operator', checkpoint_pending=True)
    assert result['definition_checkpoint']
    assert result['checkpoint'] != result['definition_checkpoint']


def test_missing_retained_history_requires_reconciliation(workspace):
    profile, root = workspace
    change(profile, lambda text: text + '\n## P1\n\n- [ ] Task\n  - **ID**: task-01\n')
    subprocess.run(['git', '-C', str(root), 'update-ref', '-d', 'refs/heads/flow-history'], check=True)
    with pytest.raises(ValueError, match='history is missing'):
        snapshot(SOURCE, profile=profile)


def test_lock_conflict_preserves_document(workspace):
    profile, _ = workspace
    before = snapshot(SOURCE, profile=profile)
    lock = profile.with_suffix('.lock')
    with lock.open('w') as stream:
        lock.chmod(0o600)
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='another writer'):
            change(profile, lambda text: text + '\n## P1\n\n- [ ] Task\n')
    assert snapshot(SOURCE, profile=profile) == before


def test_failed_atomic_replace_does_not_claim_completion(workspace, monkeypatch):
    profile, root = workspace
    from starforge_workbench import flow_history
    before = snapshot(SOURCE, profile=profile)
    def readonly(*args):
        raise OSError('synthetic read-only filesystem')
    real_replace = flow_history._replace
    monkeypatch.setattr(flow_history, '_replace', readonly)
    with pytest.raises(OSError, match='read-only'):
        change(profile, lambda text: text + '\n## P1\n\n- [ ] Task\n')
    assert snapshot(SOURCE, profile=profile) == before
    monkeypatch.setattr(flow_history, '_replace', real_replace)
    proposal = before['document'] + '\n## P1\n\n- [ ] Task\n'
    retried = mutate_document(SOURCE, profile=profile, operation_id='operation-01',
        expected_revision=before['revision'], expected_hash=before['document_hash'],
        proposed=proposal, operation='add', actor='Example Operator')
    assert retried['checkpoint'] != 'UNBORN'


def test_missing_identity_and_dirty_target_require_reconciliation(workspace):
    profile, root = workspace
    target = root / 'work/github/github.com/example-org/example-repo/issue-42/TASKS.md'
    original = target.read_text()
    target.write_text(original.replace('FLOW work-item:', 'wrong identity:'))
    with pytest.raises(ValueError, match='identity differs'):
        snapshot(SOURCE, profile=profile)
    target.write_text(original)
    change(profile, lambda text: text + '\n## P1\n\n- [ ] Task\n  - **ID**: task-01\n')
    committed = snapshot(SOURCE, profile=profile)
    target.write_text(committed['document'].replace('Task\n', 'Edited task\n'))
    dirty = snapshot(SOURCE, profile=profile)
    with pytest.raises(ValueError, match='uncheckpointed edits'):
        mutate_document(SOURCE, profile=profile, operation_id='update-01',
            expected_revision=dirty['revision'], expected_hash=dirty['document_hash'],
            proposed=dirty['document'].replace('Edited task', 'Another edit'),
            operation='update', task_id='task-01', actor='Example Operator')
    assert target.read_text() == dirty['document']


def test_signing_failure_retains_authored_bytes_without_success(workspace):
    profile, root = workspace
    git = ['git', '-C', str(root)]
    subprocess.run(git + ['config', 'commit.gpgsign', 'true'], check=True)
    subprocess.run(git + ['config', 'gpg.program', '/bin/false'], check=True)
    with pytest.raises(ValueError, match='Git commit failed'):
        change(profile, lambda text: text + '\n## P1\n\n- [ ] Task\n  - **ID**: task-01\n')
    assert snapshot(SOURCE, profile=profile)['revision'] == 'UNBORN'
    assert 'task-01' in snapshot(SOURCE, profile=profile)['document']


def test_code_branch_squash_does_not_erase_metadata_history(workspace, tmp_path):
    profile, metadata = workspace
    change(profile, lambda text: text + '\n## P1\n\n- [ ] Capture regression\n  - **ID**: task-01\n')
    before = snapshot(SOURCE, profile=profile)
    completed = mutate_document(SOURCE, profile=profile, operation_id='complete-01',
        expected_revision=before['revision'], expected_hash=before['document_hash'],
        proposed=before['document'].split('\n## P1')[0] + '\n', operation='complete',
        task_id='task-01', actor='Example Operator', evidence='reviewed')
    code = tmp_path / 'code'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(code)], check=True)
    for key, value in [('user.name', 'Example Operator'), ('user.email', 'operator@example.com')]:
        subprocess.run(['git', '-C', str(code), 'config', key, value], check=True)
    (code / 'README.md').write_text('base\n')
    subprocess.run(['git', '-C', str(code), 'add', 'README.md'], check=True)
    subprocess.run(['git', '-C', str(code), 'commit', '-qm', 'Base'], check=True)
    subprocess.run(['git', '-C', str(code), 'switch', '-qc', 'feature'], check=True)
    (code / 'temporary.txt').write_text('short lived\n')
    subprocess.run(['git', '-C', str(code), 'add', 'temporary.txt'], check=True)
    subprocess.run(['git', '-C', str(code), 'commit', '-qm', 'Add temporary file'], check=True)
    (code / 'temporary.txt').unlink()
    (code / 'README.md').write_text('final\n')
    subprocess.run(['git', '-C', str(code), 'add', '-u'], check=True)
    subprocess.run(['git', '-C', str(code), 'commit', '-qm', 'Finish feature'], check=True)
    subprocess.run(['git', '-C', str(code), 'switch', '-q', 'main'], check=True)
    subprocess.run(['git', '-C', str(code), 'merge', '--squash', 'feature'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(code), 'commit', '-qm', 'Squashed feature'], check=True)
    assert not (code / 'temporary.txt').exists()
    subprocess.run(['git', '-C', str(metadata), 'gc', '--prune=now'], check=True)
    saved = subprocess.run(['git', '-C', str(metadata), 'show',
        completed['before_revision'] + ':work/github/github.com/example-org/example-repo/issue-42/TASKS.md'],
        capture_output=True, text=True, check=True).stdout
    assert 'Capture regression' in saved
