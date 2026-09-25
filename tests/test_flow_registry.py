import pytest

from starforge_workbench.flow import init_profile, list_items, open_item, show


@pytest.fixture(autouse=True)
def isolated_filesystem(monkeypatch):
    # The test runner's /tmp is itself a Git checkout in this environment.
    monkeypatch.setattr('starforge_workbench.flow._inside_checkout', lambda root: False)


def configured(tmp_path):
    profile = tmp_path / 'config' / 'profile.json'
    root = tmp_path / 'metadata'
    init_profile(root, profile=profile,
                 github_hosts=['github.com', 'code.example.com'],
                 github_repositories=['https://github.com/example-org/example-repo',
                                      'https://code.example.com/other/example-repo'],
                 jira_sites={'demo': 'https://jira.example.com', 'second': 'https://other.example.com'})
    return profile, root


def test_preview_and_lookup_create_no_workspace(tmp_path):
    profile, root = configured(tmp_path)
    source = 'https://github.com/example-org/example-repo/issues/42'
    assert open_item(source, profile=profile, dry_run=True)['status'] == 'preview'
    assert show(source, profile=profile)['status'] == 'unverified'
    assert list_items(profile=profile)['items'] == []
    assert not (root / 'work').exists()


def test_open_is_idempotent_and_alias_keeps_identity(tmp_path):
    profile, root = configured(tmp_path)
    source = 'https://github.com/example-org/example-repo/issues/42'
    created = open_item(source, profile=profile)
    assert created['status'] == 'created'
    assert open_item('github:github.com/example-org/example-repo#42', profile=profile)['work_item_id'] == created['work_item_id']
    assert open_item(source, profile=profile)['status'] == 'existing'
    pr = 'https://github.com/example-org/example-repo/pull/7'
    assert open_item(pr, profile=profile, link_to=source, dry_run=True)['link_to'] == created['work_item_id']
    linked = open_item(pr, profile=profile, link_to=source)
    assert linked['work_item_id'] == created['work_item_id']
    assert len(list_items(profile=profile)['items']) == 1
    assert (root / 'work/github/github.com/example-org/example-repo/issue-42/TASKS.md').exists()
    assert not (root / 'work/github/github.com/example-org/example-repo/pull-7').exists()


def test_ambiguous_sources_do_not_create_work(tmp_path):
    profile, root = configured(tmp_path)
    with pytest.raises(ValueError, match='ambiguous'):
        open_item('example-repo#42', profile=profile)
    with pytest.raises(ValueError, match='ambiguous'):
        open_item('DEMO-123', profile=profile)
    assert open_item('jira:demo:DEMO-123', profile=profile)['status'] == 'created'
    assert not (root / 'work/github').exists()


@pytest.mark.parametrize('reference', [
    'http://github.com/example-org/example-repo/issues/42',
    'https://user:secret@github.com/example-org/example-repo/issues/42',
    'https://github.com/example-org/example-repo/issues/0',
    'https://github.com/example-org/example-repo/issues/42?x=y',
    'https://github.com/example-org/../issues/42',
    'https://unknown.example.com/example-org/example-repo/issues/42',
])
def test_malformed_reference_rejected_without_writes(tmp_path, reference):
    profile, root = configured(tmp_path)
    with pytest.raises(ValueError):
        open_item(reference, profile=profile)
    assert not (root / 'work').exists()


def test_symlink_escape_and_unmanaged_directory_refused(tmp_path):
    profile, root = configured(tmp_path)
    (root / 'work').symlink_to(tmp_path / 'outside', target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        open_item('https://github.com/example-org/example-repo/issues/42', profile=profile)
    assert not (tmp_path / 'outside').exists()


def test_recover_interrupted_registry_write_without_reusing_id(tmp_path):
    profile, root = configured(tmp_path)
    source = 'https://github.com/example-org/example-repo/issues/42'
    created = open_item(source, profile=profile)
    registry = profile.with_suffix('.registry.json')
    registry.write_text('{"version":1,"items":[]}')
    recovered = open_item(source, profile=profile)
    assert recovered['status'] == 'recovered'
    assert recovered['work_item_id'] == created['work_item_id']


def test_case_and_unicode_identity_normalize_without_collision(tmp_path):
    profile, root = configured(tmp_path)
    upper = open_item('https://github.com/Éxample/Example-Repo/issues/42', profile=profile)
    lower = open_item('https://github.com/éxample/example-repo/issues/42', profile=profile)
    assert upper['work_item_id'] == lower['work_item_id']
    other = open_item('https://code.example.com/other/example-repo/issues/42', profile=profile)
    assert other['work_item_id'] != upper['work_item_id']
    assert len(list_items(profile=profile)['items']) == 2
