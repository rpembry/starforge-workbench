"""Synthetic host identity/file preflight; no account or permission grants."""
from dataclasses import replace
import errno
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import workbench.vm_identity as module
from workbench.vm_identity import (DedicatedIdentityProfile, FileGrant, ProfileError,
                                   RuntimeIdentity, open_exact_grants, operator_plan,
                                   verify_runtime_identity)


def profile(tmp_path):
    return DedicatedIdentityProfile(os.getuid(), os.getuid()+1, os.getgid(), os.getuid(),
                                    (tmp_path/'protected',),
                                    (FileGrant(tmp_path/'capacity.json', Path('/diagnostics/capacity.json')),))


def observation(selected):
    return RuntimeIdentity((selected.executor_uid,)*3, (selected.executor_gid,)*3, (), True, True, True, True)


def fixture(tmp_path, monkeypatch):
    selected = profile(tmp_path)
    selected.files[0].source.write_bytes(b'{"cpu_count":1}')
    selected.files[0].source.chmod(0o640)
    monkeypatch.setattr(module, 'inspect_runtime_identity', lambda:observation(selected))
    return selected


def test_plan_is_frozen_offline_proposal_and_requires_distinct_host_identity(tmp_path):
    selected = profile(tmp_path)
    plan = operator_plan(selected)
    assert plan['activation'] == 'unsupported' and plan['privileged_helper'] == 'none'
    assert plan['file_permissions']['mode'] == '0640'
    assert 'child_inherits_confinement' in plan['required_runtime']
    with pytest.raises(ProfileError):
        replace(selected, executor_uid=selected.controller_uid)
    with pytest.raises(ProfileError):
        replace(selected, executor_uid=selected.publisher_uid)
    with pytest.raises(ProfileError):
        replace(selected, executor_uid=0)
    with pytest.raises(Exception):
        selected.executor_uid = selected.controller_uid


@pytest.mark.parametrize('change', [
    {'initial_user_namespace':False}, {'no_new_privileges':False}, {'capabilities_clear':False},
    {'inherited_fds_closed':False}, {'groups':(99999,)}, {'uids':(True,True,True)},
    {'gids':(True,True,True)}, {'initial_user_namespace':1}, {'groups':[1]},
])
def test_runtime_preflight_refuses_missing_or_ineffective_boundary(tmp_path, change):
    selected = profile(tmp_path)
    with pytest.raises(ProfileError, match='^Dedicated diagnostic profile unavailable$'):
        verify_runtime_identity(selected, replace(observation(selected), **change))


def test_actual_same_user_runtime_cannot_supply_fabricated_observation(tmp_path, monkeypatch):
    selected = profile(tmp_path)
    same_user = replace(observation(selected), uids=(selected.controller_uid,)*3)
    monkeypatch.setattr(module, 'inspect_runtime_identity', lambda:same_user)
    with pytest.raises(ProfileError):
        open_exact_grants(selected)


def test_exact_inode_is_read_only_pinned_and_not_inheritable(tmp_path, monkeypatch):
    selected = fixture(tmp_path, monkeypatch)
    fds = open_exact_grants(selected)
    try:
        assert len(fds) == 1 and not os.get_inheritable(fds[0])
        selected.files[0].source.rename(tmp_path/'old')
        selected.files[0].source.write_bytes(b'SYNTHETIC_REPLACEMENT')
        assert os.pread(fds[0], 100, 0) == b'{"cpu_count":1}'
        with pytest.raises(OSError):
            os.write(fds[0], b'x')
    finally:
        for fd in fds:
            os.close(fd)


@pytest.mark.parametrize('change', [{'st_uid':99999}, {'st_gid':99999}])
def test_file_ownership_is_independent_of_runtime_identity(tmp_path, monkeypatch, change):
    selected = fixture(tmp_path, monkeypatch)
    real_stat = module.os.fstat
    def changed(fd):
        info = real_stat(fd)
        values = {key:getattr(info,key) for key in ('st_mode','st_nlink','st_uid','st_gid')}
        values.update(change)
        return SimpleNamespace(**values)
    monkeypatch.setattr(module.os, 'fstat', changed)
    with pytest.raises(ProfileError):
        open_exact_grants(selected)


def test_partial_grant_failure_closes_every_opened_fd(tmp_path, monkeypatch):
    selected = fixture(tmp_path, monkeypatch)
    bad = tmp_path/'bad.json'
    bad.write_bytes(b'SYNTHETIC_BAD_MODE')
    bad.chmod(0o644)
    selected = replace(selected, files=selected.files+(FileGrant(bad, Path('/diagnostics/connectivity.json')),))
    opened = []
    real_open = module._open_without_symlinks
    def capture(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd
    monkeypatch.setattr(module, '_open_without_symlinks', capture)
    with pytest.raises(ProfileError):
        open_exact_grants(selected)
    assert len(opened) == 2
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize('kind', ['leaf_symlink', 'parent_symlink', 'hardlink', 'world_readable', 'writable_group', 'fifo', 'directory'])
def test_open_grants_deny_aliases_or_expanded_permissions(tmp_path, monkeypatch, kind):
    selected = fixture(tmp_path, monkeypatch)
    source = selected.files[0].source
    if kind == 'leaf_symlink':
        source.rename(tmp_path/'target')
        source.symlink_to(tmp_path/'target')
    elif kind == 'parent_symlink':
        (tmp_path/'real').mkdir()
        source.rename(tmp_path/'real'/'capacity.json')
        (tmp_path/'alias').symlink_to(tmp_path/'real', target_is_directory=True)
        selected = replace(selected, files=(FileGrant(tmp_path/'alias'/'capacity.json', Path('/diagnostics/capacity.json')),))
    elif kind == 'hardlink':
        os.link(source, tmp_path/'alias')
    elif kind in ('fifo', 'directory'):
        source.unlink()
        if kind == 'fifo':
            os.mkfifo(source, 0o640)
        else:
            source.mkdir(mode=0o640)
    else:
        source.chmod(0o644 if kind == 'world_readable' else 0o660)
    with pytest.raises(ProfileError):
        open_exact_grants(selected)


@pytest.mark.parametrize('source', ['/proc/self/fd/3', '/proc/42/root/private/file', '/dev/fd/3',
                                    '/sys/kernel/file', '/tmp/../private/file', '/tmp/file%name', '/tmp/file\nname'])
def test_configuration_cannot_request_procfd_or_rendering_injection(source):
    with pytest.raises(ProfileError):
        FileGrant(Path(source), Path('/diagnostics/capacity.json'))


def test_protected_overlap_denial_is_configuration_only(tmp_path):
    selected = profile(tmp_path)
    with pytest.raises(ProfileError):
        replace(selected, protected=(tmp_path,))


def test_preflight_detects_inherited_descriptors_and_child_close_fds(tmp_path):
    # No file contents or host secrets are accessed. A deliberately inherited
    # synthetic descriptor is detected in a real harmless child. This verifies
    # FD hygiene, not deployed mount/identity confinement.
    path = tmp_path/'synthetic.txt'
    path.write_text('SYNTHETIC_ONLY')
    fd = os.open(path, os.O_RDONLY)
    env = dict(os.environ, PYTHONPATH=str(Path(module.__file__).parents[1]))
    code = 'from workbench.vm_identity import inspect_runtime_identity; print(inspect_runtime_identity().inherited_fds_closed)'
    try:
        kept = subprocess.check_output([sys.executable, '-c', code], env=env, pass_fds=(fd,), close_fds=True)
        closed = subprocess.check_output([sys.executable, '-c', code], env=env, close_fds=True)
        assert kept.strip() == b'False' and closed.strip() == b'True'
    finally:
        os.close(fd)


@pytest.mark.parametrize('error_number', [errno.EBADF, errno.EACCES, errno.EPERM, errno.EIO, None])
def test_descriptor_inspection_ignores_only_proven_stale_entries(tmp_path, monkeypatch, capsys, error_number):
    selected = profile(tmp_path)
    status = '\n'.join([*(name+': 0' for name in
                          ('CapInh','CapPrm','CapEff','CapBnd','CapAmb')), 'NoNewPrivs: 1'])
    monkeypatch.setattr(module.Path, 'read_text', lambda path:status if str(path) == '/proc/self/status'
                        else '0 0 4294967295\n')
    monkeypatch.setattr(module.os, 'listdir', lambda path:['0','1','2','3'])
    monkeypatch.setattr(module.os, 'getresuid', lambda:(selected.executor_uid,)*3)
    monkeypatch.setattr(module.os, 'getresgid', lambda:(selected.executor_gid,)*3)
    monkeypatch.setattr(module.os, 'getgroups', lambda:[])
    def failed(fd):
        assert fd == 3
        raise OSError(error_number, 'SYNTHETIC_INSPECTION_DENIED')
    monkeypatch.setattr(module.os, 'fstat', failed)
    if error_number == errno.EBADF:
        observed = module.inspect_runtime_identity()
        assert observed.inherited_fds_closed is True
        verify_runtime_identity(selected, observed)
    else:
        with pytest.raises(ProfileError, match='^Dedicated diagnostic profile unavailable$') as failure:
            module.inspect_runtime_identity()
        assert failure.value.__suppress_context__
        # The canonical grant opener cannot continue after unknown FD state.
        with pytest.raises(ProfileError, match='^Dedicated diagnostic profile unavailable$'):
            open_exact_grants(selected)
    assert capsys.readouterr() == ('', '')
