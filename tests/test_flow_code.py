from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import threading

import pytest

from starforge_workbench.flow import init_profile, open_item
from starforge_workbench.flow_code import MissingBindingError, adopt, bind, workspace

SOURCE = 'https://github.com/example-org/example-repo/issues/42'


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def repository(path):
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(path)], check=True)
    git(path, 'config', 'user.name', 'Example Operator')
    git(path, 'config', 'user.email', 'operator@example.com')
    (path / 'README.md').write_text('base\n')
    git(path, 'add', 'README.md')
    git(path, 'commit', '-qm', 'Base')
    return git(path, 'rev-parse', 'HEAD')


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setattr('starforge_workbench.flow._inside_checkout', lambda root: False)
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(runtime))
    profile = tmp_path / 'private' / 'profile.json'
    init_profile(tmp_path / 'metadata', profile=profile)
    open_item(SOURCE, profile=profile)
    return profile, tmp_path


def test_new_branch_uses_explicit_base_and_resume_preserves_dirty_state(flow):
    profile, home = flow
    repo = home / 'code'
    base = repository(repo)
    git(repo, 'switch', '-qc', 'unrelated')
    (repo / 'README.md').write_text('unrelated\n')
    git(repo, 'commit', '-qam', 'Unrelated feature')
    unrelated = git(repo, 'rev-parse', 'HEAD')
    marker = home / 'hook-ran'
    hook = repo / '.git/hooks/post-checkout'
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
    hook.chmod(0o700)
    root = home / 'worktrees'
    root.mkdir()
    bind(SOURCE, name='api', repository=repo, worktree_root=root, base_ref='main', profile=profile)
    prepared = workspace(SOURCE, profile=profile, create=True)['repositories'][0]
    assert prepared['status'] == 'created'
    assert prepared['head'] == base != unrelated
    assert prepared['starting_commit'] == base
    assert not marker.exists()
    worktree = Path(prepared['worktree'])
    (worktree / 'README.md').write_text('local edit\n')
    resumed = workspace(SOURCE, profile=profile, create=True)['repositories'][0]
    assert resumed['status'] == 'resumed'
    assert resumed['dirty'] is True
    assert resumed['head'] == base
    assert (worktree / 'README.md').read_text() == 'local edit\n'
    assert git(repo, 'rev-parse', 'HEAD') == unrelated
    assert len([path for path in root.iterdir() if path.is_dir()]) == 1
    lock_dir = home / 'runtime/starforge-ai-workbench/flow-repo-locks'
    assert lock_dir.is_dir()
    assert lock_dir.stat().st_mode & 0o077 == 0
    assert len(list(lock_dir.glob('*.lock'))) == 1


def test_two_repositories_partial_failure_then_retry(flow):
    profile, home = flow
    first, second = home / 'first', home / 'second'
    repository(first)
    repository(second)
    root = home / 'worktrees'
    root.mkdir()
    bind(SOURCE, name='first', repository=first, worktree_root=root, base_ref='main', profile=profile)
    bind(SOURCE, name='second', repository=second, worktree_root=root, base_ref='release', profile=profile)
    initial = workspace(SOURCE, profile=profile, create=True)['repositories']
    assert [row['status'] for row in initial] == ['created', 'error']
    git(second, 'branch', 'release', 'main')
    retry = workspace(SOURCE, profile=profile, create=True)['repositories']
    assert [row['status'] for row in retry] == ['resumed', 'created']
    assert retry[0]['worktree'] == initial[0]['worktree']


def test_interrupted_registry_write_recovers_worktree(flow, monkeypatch):
    profile, home = flow
    repo = home / 'code'
    repository(repo)
    root = home / 'worktrees'
    root.mkdir()
    bind(SOURCE, name='api', repository=repo, worktree_root=root, base_ref='main', profile=profile)
    from starforge_workbench import flow_code
    real_write = flow_code._atomic_json
    writes = 0
    def interrupted(path, data):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError('simulated registry interruption')
        return real_write(path, data)
    monkeypatch.setattr(flow_code, '_atomic_json', interrupted)
    first = workspace(SOURCE, profile=profile, create=True)['repositories'][0]
    assert first['status'] == 'error'
    monkeypatch.setattr(flow_code, '_atomic_json', real_write)
    recovered = workspace(SOURCE, profile=profile, create=True)['repositories'][0]
    assert recovered['status'] == 'recovered'
    assert workspace(SOURCE, profile=profile, create=False)['repositories'][0]['status'] == 'resumed'
    assert len(list(root.iterdir())) == 1


def test_unrecorded_branch_and_missing_binding_fail_closed(flow):
    profile, home = flow
    with pytest.raises(MissingBindingError, match='No explicit repository binding'):
        workspace(SOURCE, profile=profile, create=True)
    repo = home / 'code'
    repository(repo)
    root = home / 'worktrees'
    root.mkdir()
    binding = bind(SOURCE, name='api', repository=repo, worktree_root=root,
                   base_ref='main', profile=profile)['binding']
    git(repo, 'branch', binding['branch'], 'main')
    result = workspace(SOURCE, profile=profile, create=True)['repositories'][0]
    assert result['status'] == 'error'
    assert 'Unrecorded work branch' in result['reason']
    assert not list(root.iterdir())


def test_simultaneous_prepare_creates_only_one_worktree(flow):
    profile, home = flow
    repo = home / 'code'
    repository(repo)
    root = home / 'worktrees'
    root.mkdir()
    bind(SOURCE, name='api', repository=repo, worktree_root=root, base_ref='main', profile=profile)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(workspace, SOURCE, profile=profile, create=True) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result()['repositories'][0]['status'])
            except ValueError as exc:
                assert 'another writer' in str(exc)
    assert 'created' in outcomes
    assert len(list(root.iterdir())) == 1


def test_tracked_filter_is_refused_before_worktree_creation(flow):
    profile, home = flow
    repo = home / 'code'
    repository(repo)
    (repo / '.gitattributes').write_text('*.txt filter=example\n')
    git(repo, 'add', '.gitattributes')
    git(repo, 'commit', '-qm', 'Declare filter')
    root = home / 'worktrees'
    root.mkdir()
    bind(SOURCE, name='api', repository=repo, worktree_root=root, base_ref='main', profile=profile)
    result = workspace(SOURCE, profile=profile, create=True)['repositories'][0]
    assert result['status'] == 'error'
    assert 'filter attributes' in result['reason']
    assert not list(root.iterdir())


def test_explicit_adoption_reuses_dirty_existing_worktree(flow):
    profile, home = flow
    repo = home / 'code'
    repository(repo)
    root = home / 'worktrees'
    root.mkdir()
    binding = bind(SOURCE, name='api', repository=repo, worktree_root=root,
                   base_ref='main', profile=profile)['binding']
    existing = root / 'existing-review'
    git(repo, 'worktree', 'add', '-b', binding['branch'], str(existing), 'main')
    (existing / 'README.md').write_text('authored edit\n')
    assert workspace(SOURCE, profile=profile, create=True)['repositories'][0]['status'] == 'error'
    result = adopt(SOURCE, name='api', worktree=existing, profile=profile)
    assert result['status'] == 'adopted'
    assert result['dirty'] is True
    assert result['actual_starting_commit'] == 'unknown'
    resumed = workspace(SOURCE, profile=profile, create=True)['repositories'][0]
    assert resumed['worktree'] == str(existing)
    assert resumed['dirty'] is True
    assert (existing / 'README.md').read_text() == 'authored edit\n'


def test_cli_prepare_and_read_only_lookup(flow, capsys):
    profile, home = flow
    repo = home / 'code'
    repository(repo)
    root = home / 'worktrees'
    root.mkdir()
    from starforge_workbench.cli import main
    assert main(['work', '--profile', str(profile), 'show', SOURCE]) == 0
    capsys.readouterr()
    assert not list(root.iterdir())
    bind(SOURCE, name='api', repository=repo, worktree_root=root, base_ref='main', profile=profile)
    assert main(['work', '--profile', str(profile), 'prepare', SOURCE]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['repositories'][0]['status'] == 'created'


def test_slow_clone_does_not_hold_profile_writer_lock(flow, monkeypatch):
    profile, home = flow
    repo = home / 'missing-clone'
    root = home / 'worktrees'
    root.mkdir()
    bind(SOURCE, name='api', repository=repo, worktree_root=root, base_ref='main',
         remote='https://example.invalid/example-repo.git', allow_remote_read=True, profile=profile)
    from starforge_workbench import flow_code
    original_run = flow_code.subprocess.run
    clone_started = threading.Event()
    release_clone = threading.Event()
    def delayed_clone(argv, *args, **kwargs):
        if len(argv) > 4 and argv[0] == 'git' and 'clone' in argv:
            clone_started.set()
            assert release_clone.wait(5)
            repository(repo)
            return subprocess.CompletedProcess(argv, 0, '', '')
        return original_run(argv, *args, **kwargs)
    monkeypatch.setattr(flow_code.subprocess, 'run', delayed_clone)
    with ThreadPoolExecutor(max_workers=2) as pool:
        prepared = pool.submit(workspace, SOURCE, profile=profile, create=True)
        assert clone_started.wait(5)
        other = pool.submit(open_item,
            'https://github.com/example-org/example-repo/issues/43', profile=profile)
        assert other.result(timeout=2)['status'] == 'created'
        release_clone.set()
        assert prepared.result(timeout=8)['repositories'][0]['status'] == 'created'
