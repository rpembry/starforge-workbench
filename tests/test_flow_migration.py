import subprocess

import pytest

from starforge_workbench.flow import init_profile, open_item
from starforge_workbench.flow_migration import apply, preview
from starforge_workbench.flow_tasks import inspect

SOURCE = 'https://github.com/example/repo/issues/42'


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr('starforge_workbench.flow._inside_checkout', lambda root: False)
    profile, root = tmp_path / 'private' / 'profile.json', tmp_path / 'metadata'
    init_profile(root, profile=profile)
    subprocess.run(['git', '-C', str(root), 'config', 'user.name', 'Operator'], check=True)
    subprocess.run(['git', '-C', str(root), 'config', 'user.email', 'operator@example.org'], check=True)
    open_item(SOURCE, profile=profile)
    selected = tmp_path / 'TODO.md'
    selected.write_text('# Checklist\n- [ ] Active step\n- [x] Historical claim\n```md\n- [ ] Example only\n```\n')
    return profile, selected


def test_preview_first_preserves_original_and_checked_claim(fixture):
    profile, selected = fixture
    before = selected.read_bytes()
    plan = preview(SOURCE, selected, profile=profile)
    assert [row['classification'] for row in plan['entries']] == [
        'proposed_active', 'checked_claim_requires_review']
    assert not inspect(SOURCE, 'list', profile=profile)['tasks']
    result = apply(SOURCE, selected, profile=profile,
                   expected_source_hash=plan['source_hash'], expected_revision=plan['target_revision'],
                   expected_target_hash=plan['target_hash'])
    assert result['checked_claims_skipped'] == 1
    assert result['imported'][0]['task_id'] == plan['entries'][0]['proposed_task_id']
    assert len(inspect(SOURCE, 'list', profile=profile)['tasks']) == 1
    assert selected.read_bytes() == before
    with pytest.raises(ValueError, match='changed since preview'):
        apply(SOURCE, selected, profile=profile,
              expected_source_hash=plan['source_hash'], expected_revision=plan['target_revision'],
              expected_target_hash=plan['target_hash'])


def test_stale_source_and_nested_content_block_adoption(fixture):
    profile, selected = fixture
    plan = preview(SOURCE, selected, profile=profile)
    selected.write_text(selected.read_text() + '- [ ] New step\n')
    with pytest.raises(ValueError, match='changed since preview'):
        apply(SOURCE, selected, profile=profile,
              expected_source_hash=plan['source_hash'], expected_revision=plan['target_revision'],
              expected_target_hash=plan['target_hash'])
    selected.write_text('# Tasks\n- [ ] Parent\n  - [ ] Nested\n')
    plan = preview(SOURCE, selected, profile=profile)
    assert plan['unsupported']
    with pytest.raises(ValueError, match='Unsupported'):
        apply(SOURCE, selected, profile=profile,
              expected_source_hash=plan['source_hash'], expected_revision=plan['target_revision'],
              expected_target_hash=plan['target_hash'])


def test_interrupted_multi_row_adoption_repreviews_without_duplicate(fixture, monkeypatch):
    import starforge_workbench.flow_migration as migration
    profile, selected = fixture
    selected.write_text('# Checklist\n- [ ] First\n- [ ] Second\n')
    first = preview(SOURCE, selected, profile=profile)
    real_mutate = migration.mutate
    calls = [0]

    def interrupted(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError('synthetic interruption before second checkpoint')
        return real_mutate(*args, **kwargs)

    monkeypatch.setattr(migration, 'mutate', interrupted)
    with pytest.raises(OSError, match='synthetic interruption'):
        apply(SOURCE, selected, profile=profile,
              expected_source_hash=first['source_hash'], expected_revision=first['target_revision'],
              expected_target_hash=first['target_hash'])
    assert len(inspect(SOURCE, 'list', profile=profile)['tasks']) == 1
    with pytest.raises(ValueError, match='changed since preview'):
        apply(SOURCE, selected, profile=profile,
              expected_source_hash=first['source_hash'], expected_revision=first['target_revision'],
              expected_target_hash=first['target_hash'])
    monkeypatch.setattr(migration, 'mutate', real_mutate)
    resumed = preview(SOURCE, selected, profile=profile)
    result = apply(SOURCE, selected, profile=profile,
                   expected_source_hash=resumed['source_hash'], expected_revision=resumed['target_revision'],
                   expected_target_hash=resumed['target_hash'])
    assert len(inspect(SOURCE, 'list', profile=profile)['tasks']) == 2
    assert [entry['task_id'] for entry in result['imported']] == [
        entry['proposed_task_id'] for entry in first['entries']]


def test_selected_symlink_is_rejected(fixture, tmp_path):
    profile, selected = fixture
    alias = tmp_path / 'TASKS.md'
    alias.symlink_to(selected)
    with pytest.raises(ValueError, match='nonsymlink'):
        preview(SOURCE, alias, profile=profile)
