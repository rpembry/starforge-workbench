from pathlib import Path
import hashlib
import json
import subprocess

import pytest

from starforge_workbench.flow import init_profile, open_item
from starforge_workbench.flow_history import operation_state
from starforge_workbench.flow_markdown import parse
from starforge_workbench.flow_tasks import inspect, main, mutate

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


def test_lossless_parser_preserves_foreign_content_and_crlf():
    raw = ('# Tasks\r\n\r\n<!-- FLOW work-item: wi-example -->\r\n'
           '<!-- Source: https://example.com/work -->\r\n\r\n## P1\r\n\r\n'
           '- [ ] Café parser\r\n  - **ID**: parser-01\r\n'
           '  - **Unknown**: keep me\r\n  - **Details**: first line\r\n'
           '    second line\r\n  - [ ] nested example\r\n\r\n'
           '```markdown\r\n- [ ] fenced example\r\n```\r\n\r\n'
           'Prose outside the task.\r\n')
    doc = parse(raw)
    assert len(doc.tasks) == 1
    assert doc.tasks[0].task_id == 'parser-01'
    assert 'Unknown' in doc.tasks[0].fields
    assert doc.text == raw


def test_next_and_history_before_first_checkpoint(workspace):
    profile, _ = workspace
    assert inspect(SOURCE, 'next', profile=profile)['suggestion'] is None
    assert inspect(SOURCE, 'history', profile=profile)['entries'] == []


def test_add_edit_block_complete_and_history(workspace):
    profile, root = workspace
    added = mutate(SOURCE, 'add', profile=profile, title='Cover Unicode', acceptance='Review passes',
                   operation_id='add-01')
    legacy_arguments = ['add', None, 'Cover Unicode', None, 'Review passes',
                        None, '', 'P2', None, 'local operator']
    legacy_hash = hashlib.sha256(json.dumps(legacy_arguments, ensure_ascii=False).encode()).hexdigest()
    assert operation_state(SOURCE, 'add-01', profile=profile)['client_hash'] == legacy_hash
    assert mutate(SOURCE, 'add', profile=profile, title='Cover Unicode', acceptance='Review passes',
                  operation_id='add-01') == added
    with pytest.raises(ValueError, match='different command arguments'):
        mutate(SOURCE, 'add', profile=profile, title='Different task', operation_id='add-01')
    task_id = inspect(SOURCE, 'list', profile=profile)['tasks'][0]['task_id']
    assert task_id and added['checkpoint']
    mutate(SOURCE, 'update', profile=profile, task_id=task_id, details='Use synthetic fixtures',
           operation_id='update-01')
    mutate(SOURCE, 'block', profile=profile, task_id=task_id, reason='Waiting for review',
           operation_id='block-01')
    assert inspect(SOURCE, 'next', profile=profile)['suggestion'] is None
    saved = inspect(SOURCE, 'show', profile=profile, task_id=task_id)
    assert 'Review passes' in saved['raw'] and 'Waiting for review' in saved['raw']
    outcome = mutate(SOURCE, 'complete', profile=profile, task_id=task_id,
                     evidence='example-review', operation_id='complete-01')
    assert outcome['checkpoint']
    completed = inspect(SOURCE, 'show', profile=profile, task_id=task_id)
    assert completed['status'] == 'complete'
    assert completed['checkpoint'] == outcome['checkpoint']
    assert not inspect(SOURCE, 'list', profile=profile)['tasks']
    history = inspect(SOURCE, 'history', profile=profile, task_id=task_id)['entries']
    assert {'add', 'update', 'block', 'complete'} <= {entry['operation'] for entry in history}
    assert (root / 'work/github/github.com/example-org/example-repo/issue-42/TASKS.md').read_text().startswith('# Tasks')
    source_reopen = next(entry['checkpoint'] for entry in history if entry['operation'] == 'block')
    mutate(SOURCE, 'reopen', profile=profile, task_id=task_id, from_revision=source_reopen,
           operation_id='reopen-01')
    assert inspect(SOURCE, 'show', profile=profile, task_id=task_id)['task']['task_id'] == task_id


def test_duplicate_ids_cycles_and_unknown_blockers():
    initial = '# Tasks\n\n## P1\n\n- [ ] One\n  - **ID**: one\n\n- [ ] Two\n  - **ID**: one\n'
    with pytest.raises(ValueError, match='Duplicate FLOW task IDs'):
        parse(initial)
    cyclic = '# Tasks\n\n## P1\n\n- [ ] One\n  - **ID**: one\n  - **Blocked by**: two\n\n- [ ] Two\n  - **ID**: two\n  - **Blocked by**: one\n'
    with pytest.raises(ValueError, match='cycle'):
        parse(cyclic)
    idless_child = ('# Tasks\n\n## P1\n\n- [ ] Parent\n  - **ID**: parent\n\n'
                    '- [ ] Child\n  - **Parent task**: parent\n')
    with pytest.raises(ValueError, match='explicit task ID'):
        parse(idless_child)


def test_stale_preview_and_manual_claim_require_reconciliation(workspace):
    profile, root = workspace
    preview = mutate(SOURCE, 'add', profile=profile, title='One', operation_id='add-01', preview=True)
    mutate(SOURCE, 'add', profile=profile, title='One', operation_id='add-01',
           expected_revision=preview['revision'], expected_hash=preview['document_hash'])
    with pytest.raises(ValueError, match='Stale'):
        mutate(SOURCE, 'add', profile=profile, title='Two', expected_revision=preview['revision'])
    target = root / 'work/github/github.com/example-org/example-repo/issue-42/TASKS.md'
    target.write_text(target.read_text().replace('- [ ] One', '- [x] One'))
    report = inspect(SOURCE, 'reconcile', profile=profile)
    assert report['manual_checked']
    with pytest.raises(ValueError, match='Manual'):
        mutate(SOURCE, 'add', profile=profile, title='Two')


def test_crlf_and_unrelated_prose_remain_byte_identical(workspace):
    profile, root = workspace
    target = root / 'work/github/github.com/example-org/example-repo/issue-42/TASKS.md'
    original = target.read_bytes().replace(b'\n', b'\r\n') + b'\r\nUnrelated prose.\r\n'
    target.write_bytes(original)
    mutate(SOURCE, 'add', profile=profile, title='One', operation_id='add-crlf', checkpoint_pending=True)
    raw = target.read_bytes()
    assert b'Unrelated prose.\r\n' in raw
    assert raw.count(b'\n') == raw.count(b'\r\n')
    assert b'- [ ] One\r\n' in raw


def test_unknown_and_canceled_blockers_do_not_become_success(workspace):
    profile, root = workspace
    mutate(SOURCE, 'add', profile=profile, title='Candidate', operation_id='candidate-01')
    task_id = inspect(SOURCE, 'list', profile=profile)['tasks'][0]['task_id']
    target = root / 'work/github/github.com/example-org/example-repo/issue-42/TASKS.md'
    target.write_text(target.read_text().replace(f'**ID**: {task_id}',
        f'**ID**: {task_id}\n  - **Blocked by**: unknown-01'))
    report = inspect(SOURCE, 'next', profile=profile)
    assert report['suggestion'] is None
    assert inspect(SOURCE, 'list', profile=profile)['tasks'][0]['unresolved_dependencies'] == ['unknown-01']


def test_reopened_prerequisite_blocks_dependent_again(workspace):
    profile, root = workspace
    mutate(SOURCE, 'add', profile=profile, title='Prerequisite', operation_id='pre-01')
    prerequisite = inspect(SOURCE, 'list', profile=profile)['tasks'][0]['task_id']
    mutate(SOURCE, 'add', profile=profile, title='Dependent', operation_id='dep-01')
    target = root / 'work/github/github.com/example-org/example-repo/issue-42/TASKS.md'
    target.write_text(target.read_text().replace('- [ ] Dependent\n',
        f'- [ ] Dependent\n  - **Blocked by**: {prerequisite}\n'))
    terminal = mutate(SOURCE, 'complete', profile=profile, task_id=prerequisite,
                      evidence='reviewed', operation_id='complete-pre', checkpoint_pending=True)
    assert inspect(SOURCE, 'next', profile=profile)['suggestion']['title'] == 'Dependent'
    mutate(SOURCE, 'reopen', profile=profile, task_id=prerequisite,
           from_revision=terminal['definition_checkpoint'], operation_id='reopen-pre')
    assert inspect(SOURCE, 'next', profile=profile)['suggestion']['title'] == 'Prerequisite'


def test_managed_dependencies_gate_next_and_can_be_cleared(workspace):
    profile, _ = workspace
    prerequisite = mutate(SOURCE, 'add', profile=profile, title='Stage fixture',
                          operation_id='stage-fixture', priority='P2')['task_id']
    validation_add = mutate(SOURCE, 'add', profile=profile, title='Validate fixture',
                            operation_id='validate-fixture', priority='P1',
                            blocked_by=[prerequisite])
    validation = validation_add['task_id']
    assert inspect(SOURCE, 'next', profile=profile)['suggestion']['task_id'] == prerequisite
    with pytest.raises(ValueError, match='known active or completed'):
        mutate(SOURCE, 'update', profile=profile, task_id=validation,
               blocked_by=['unknown-task'], operation_id='unknown-dependency')
    with pytest.raises(ValueError, match='Duplicate FLOW dependency'):
        mutate(SOURCE, 'update', profile=profile, task_id=validation,
               blocked_by=[prerequisite, prerequisite], operation_id='duplicate-dependency')
    with pytest.raises(ValueError, match='cycle'):
        mutate(SOURCE, 'update', profile=profile, task_id=prerequisite,
               blocked_by=[validation], operation_id='cyclic-dependency')
    with pytest.raises(ValueError, match='itself'):
        mutate(SOURCE, 'update', profile=profile, task_id=validation,
               blocked_by=[validation], operation_id='self-dependency')
    mutate(SOURCE, 'complete', profile=profile, task_id=prerequisite,
           evidence='Synthetic staging reviewed', operation_id='complete-stage')
    assert inspect(SOURCE, 'list', profile=profile)['tasks'][0]['unresolved_dependencies'] == []
    assert inspect(SOURCE, 'next', profile=profile)['suggestion']['task_id'] == validation
    satisfied = mutate(SOURCE, 'add', profile=profile, title='Follow-up',
                       blocked_by=[prerequisite], operation_id='after-completion')['task_id']
    assert inspect(SOURCE, 'show', profile=profile,
                   task_id=satisfied)['task']['unresolved_dependencies'] == []
    mutate(SOURCE, 'reopen', profile=profile, task_id=prerequisite,
           from_revision=validation_add['checkpoint'], operation_id='reopen-stage')
    assert inspect(SOURCE, 'next', profile=profile)['suggestion']['task_id'] == prerequisite
    cleared = mutate(SOURCE, 'update', profile=profile, task_id=validation,
                     blocked_by=[], operation_id='clear-dependency')
    assert mutate(SOURCE, 'update', profile=profile, task_id=validation,
                  blocked_by=[], operation_id='clear-dependency') == cleared
    assert 'Blocked by' not in inspect(SOURCE, 'show', profile=profile,
                                      task_id=validation)['task']['fields']
    assert inspect(SOURCE, 'next', profile=profile)['suggestion']['task_id'] == validation


def test_parent_hierarchy_is_organizational_and_keeps_parent_active(workspace, capsys):
    profile, _ = workspace
    parent = mutate(SOURCE, 'add', profile=profile, title='Synthetic phase',
                    operation_id='phase-01')['task_id']
    child = mutate(SOURCE, 'add', profile=profile, title='Verify phase input',
                   parent_task_id=parent, operation_id='child-01')['task_id']
    tasks = inspect(SOURCE, 'list', profile=profile)['tasks']
    assert tasks[0]['child_task_ids'] == [child]
    assert tasks[1]['parent_task_id'] == parent
    assert main(['--profile', str(profile), '--format', 'text', 'list', SOURCE]) == 0
    assert f'  P2 [{child}] Verify phase input' in capsys.readouterr().out
    with pytest.raises(ValueError, match='parent cycle'):
        mutate(SOURCE, 'update', profile=profile, task_id=parent,
               parent_task_id=child, operation_id='parent-cycle')
    with pytest.raises(ValueError, match='another active task'):
        mutate(SOURCE, 'add', profile=profile, title='Orphan',
               parent_task_id='unknown-parent', operation_id='orphan')
    with pytest.raises(ValueError, match='parent task must remain active'):
        mutate(SOURCE, 'complete', profile=profile, task_id=parent,
               evidence='Premature completion', operation_id='premature-parent')
    mutate(SOURCE, 'update', profile=profile, task_id=child,
           clear_parent=True, operation_id='clear-parent')
    assert inspect(SOURCE, 'show', profile=profile, task_id=child)['task']['parent_task_id'] is None
    mutate(SOURCE, 'complete', profile=profile, task_id=parent,
           evidence='Phase no longer owns active children', operation_id='complete-parent')


def test_canceled_dependency_remains_unresolved_and_cli_sets_relations(workspace, capsys):
    profile, _ = workspace
    phase = mutate(SOURCE, 'add', profile=profile, title='Phase', operation_id='phase')
    prerequisite = mutate(SOURCE, 'add', profile=profile, title='Input', operation_id='input')
    assert main(['--profile', str(profile), 'add', SOURCE, 'Check input',
                 '--operation-id', 'cli-child', '--parent-task-id', phase['task_id'],
                 '--blocked-by', prerequisite['task_id']]) == 0
    child = json.loads(capsys.readouterr().out)['task_id']
    mutate(SOURCE, 'cancel', profile=profile, task_id=prerequisite['task_id'],
           reason='Fixture no longer available', operation_id='cancel-input')
    with pytest.raises(ValueError, match='known active or completed'):
        mutate(SOURCE, 'add', profile=profile, title='New dependent',
               blocked_by=[prerequisite['task_id']], operation_id='reject-canceled')
    selected = inspect(SOURCE, 'show', profile=profile, task_id=child)['task']
    assert selected['parent_task_id'] == phase['task_id']
    assert selected['unresolved_dependencies'] == [prerequisite['task_id']]
    assert main(['--profile', str(profile), 'update', SOURCE, child,
                 '--operation-id', 'cli-clear', '--clear-parent', '--clear-blocked-by']) == 0
    capsys.readouterr()
    selected = inspect(SOURCE, 'show', profile=profile, task_id=child)['task']
    assert selected['parent_task_id'] is None
    assert selected['unresolved_dependencies'] == []


def test_relation_edit_preserves_unknown_metadata_and_crlf(workspace):
    profile, root = workspace
    parent = mutate(SOURCE, 'add', profile=profile, title='Phase', operation_id='phase-01')['task_id']
    child = mutate(SOURCE, 'add', profile=profile, title='Step', operation_id='step-01')['task_id']
    target = root / 'work/github/github.com/example-org/example-repo/issue-42/TASKS.md'
    raw = target.read_bytes().replace(b'\n', b'\r\n')
    raw = raw.replace(f'**ID**: {child}\r\n'.encode(),
                      f'**ID**: {child}\r\n  - **Unknown**: preserve this\r\n'.encode())
    target.write_bytes(raw)
    mutate(SOURCE, 'update', profile=profile, task_id=child,
           parent_task_id=parent, blocked_by=[parent], operation_id='relations-01',
           checkpoint_pending=True)
    mutate(SOURCE, 'update', profile=profile, task_id=child,
           blocked_by=[], clear_parent=True, operation_id='relations-clear')
    saved = target.read_bytes()
    assert b'  - **Unknown**: preserve this\r\n' in saved
    assert saved.count(b'\n') == saved.count(b'\r\n')
    assert b'**Parent task**:' not in saved and b'**Blocked by**:' not in saved


def test_text_output_is_a_human_task_view(workspace, capsys):
    profile, _ = workspace
    mutate(SOURCE, 'add', profile=profile, title='Review example', operation_id='example-01')
    assert main(['--profile', str(profile), '--format', 'text', 'list', SOURCE]) == 0
    output = capsys.readouterr().out
    assert 'P2 [task-' in output and 'Review example' in output
    assert '{' not in output and '"tasks"' not in output
    assert main(['--profile', str(profile), '--format', 'text', 'next', SOURCE]) == 0
    assert 'Suggested: P2' in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        main(['--profile', str(profile), 'show', SOURCE, 'unknown-01'])
    assert error.value.code == 2
    assert 'no terminal outcome' in capsys.readouterr().err
