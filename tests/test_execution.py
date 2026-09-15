import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from starforge_workbench.execution import ProfileError, load_profile, parse_profile, prepare_execution

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def docker():
    return yaml.safe_load((ROOT/'config/execution.docker.example.yaml').read_text())


def test_native_default_does_not_probe_docker():
    with patch('subprocess.run', side_effect=AssertionError('unexpected process')):
        assert load_profile() == parse_profile({}) == load_profile(ROOT/'config/execution.tmux.example.yaml')
        assert load_profile().metadata() == dict(backend='tmux', repository_strategy='existing', mounts=[])


def test_docker_example_and_canonical_metadata(docker, tmp_path):
    target = tmp_path/'worker.json'
    profile = prepare_execution(docker, target)
    receipt = json.loads(target.read_text())
    assert receipt == dict(phase='planned', execution=profile.metadata())
    assert target.stat().st_mode & 0o777 == 0o600
    assert profile.backend == 'docker' and profile.cpus == 2
    assert 'secret_refs' not in receipt['execution']
    reordered = copy.deepcopy(docker); reordered['mounts'].reverse()
    assert parse_profile(reordered) == profile
    with pytest.raises(FileExistsError):prepare_execution(docker, target)
    assert json.loads(target.read_text()) == receipt


@pytest.mark.parametrize('field,value', [
    ('backend', 'podman'), ('backend', []), ('image', 'python:latest'),
    ('image', 'https://name:PRIVATE@example.invalid/image'), ('image', 'x@sha256:bad'),
    ('network', 'host'), ('network', 'bridge'), ('network', None),
    ('repository_strategy', 'existing'), ('user', 0), ('group', 0),
    ('cpus', True), ('cpus', float('nan')), ('cpus', float('inf')), ('cpus', 10**400),
    ('memory_mb', -1), ('pids_limit', 0), ('timeout_seconds', '300'),
    ('secret_refs', {'token': 'PRIVATE'}), ('secret_refs', ['/home/user/auth.json']),
    ('secret_refs', ['PRIVATE=value']), ('secret_refs', ['provider_login']),
    ('privileged', True), ('environment', {'TOKEN': 'PRIVATE'}),
    ('mounts', [{'source': '/var/run/docker.sock', 'target': '/workspace', 'read_only': False}]),
    ('mounts', [{'source': 'worktree', 'target': '/workspace/../etc', 'read_only': False}]),
    ('mounts', [{'source': 'worktree', 'target': '/workspace', 'read_only': 'false'}]),
    ('mounts', [{'source': 'worktree', 'target': '/workspace', 'read_only': False, 'propagation': 'shared'}]),
    ('mounts', [{'source': 'scratch', 'target': '/scratch', 'read_only': False}]),
    ('mounts', [{'source': 'worktree', 'target': '/workspace', 'read_only': False}]*2),
])
def test_invalid_profiles_fail_before_metadata_or_runtime(docker, tmp_path, field, value):
    docker[field] = value
    target = tmp_path/'planned.json'
    with patch('subprocess.run', side_effect=AssertionError('unexpected launch')):
        with pytest.raises(ProfileError) as error:prepare_execution(docker, target)
    assert not target.exists()
    assert 'PRIVATE' not in str(error.value)


def test_missing_docker_fields_and_native_options_rejected(docker):
    for field in ('image','toolchain','user','group','cpus','memory_mb','pids_limit','timeout_seconds','network','mounts'):
        candidate = dict(docker);candidate.pop(field)
        with pytest.raises(ProfileError):parse_profile(candidate)
    with pytest.raises(ProfileError):parse_profile({'backend':'tmux','image':docker['image']})
    with pytest.raises(ProfileError):parse_profile({'backend':'tmux','repository_strategy':'per-task-worktree'})


def test_explicit_file_errors_never_fall_back_or_echo_contents(tmp_path):
    p = tmp_path/'profile.yaml'
    for text in ('', 'image: [PRIVATE', '[]', 'backend: docker'):
        p.write_text(text)
        with pytest.raises(ProfileError) as error:load_profile(p)
        assert 'PRIVATE' not in str(error.value)
    with pytest.raises(ProfileError):load_profile(tmp_path/'missing')


def test_metadata_path_permissions_and_aliases(docker, tmp_path):
    public = tmp_path/'public';public.mkdir(mode=0o755)
    with pytest.raises(ProfileError):prepare_execution(docker, public/'worker.json')
    alias = tmp_path/'alias';alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ProfileError):prepare_execution(docker, alias/'worker.json')
    existing = tmp_path/'existing';existing.write_text('retain')
    link = tmp_path/'worker.json';link.symlink_to(existing)
    with pytest.raises(FileExistsError):prepare_execution(docker, link)
    assert existing.read_text() == 'retain'


def test_existing_native_launch_dispatch_is_unchanged():
    from starforge_workbench import cli
    manifest = ROOT/'config/workbench.example.yaml'
    context = cli.load(manifest)['contexts'][0]
    with patch.object(cli, 'up') as up, patch('subprocess.run', side_effect=AssertionError('runtime probe')):
        cli.main(['--manifest', str(manifest), 'up', context['id'], '--headless'])
    up.assert_called_once_with(context, manifest, True)
