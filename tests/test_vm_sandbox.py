"""Plan/config tests only; these do not create users, mount disks or boot VMs."""
from dataclasses import FrozenInstanceError
import os
from pathlib import Path

import pytest

from workbench.vm_sandbox import SyntheticVMPolicy, diagnostic_script, sandbox_plan


def fixture_policy(tmp_path, **kwargs):
    base = tmp_path / 'base.qcow2'
    overlay = tmp_path / 'overlay.qcow2'
    for path in (base, overlay):
        path.write_bytes(b'SYNTHETIC_NOT_A_REAL_IMAGE')
        path.chmod(0o600)
    return SyntheticVMPolicy(base, overlay, **kwargs)


def test_plan_mounts_only_exact_grants_and_fixes_network_and_resource_controls(tmp_path):
    policy = fixture_policy(tmp_path, protected_directories=(Path('/home'), Path('/etc')))
    with sandbox_plan(policy) as plan:
        argv = plan.argv
        assert argv[:2] == ('/usr/bin/bwrap', '--unshare-all')
        for flag in ('--disable-userns', '--die-with-parent', '--new-session', '--clearenv'):
            assert flag in argv
        assert argv[argv.index('-nic') + 1] == 'none'
        assert argv[argv.index('-monitor') + 1] == 'none'
        assert argv[argv.index('-serial') + 1] == 'stdio'
        assert argv[argv.index('-m') + 1] == '768'
        assert argv[argv.index('-smp') + 1] == '1'
        assert str(policy.base_image) not in argv and str(policy.overlay) not in argv
        assert len(plan.pass_fds) == 3
        assert argv.count('--ro-bind-fd') == 2 and argv.count('--bind-fd') == 1
        for fd in plan.pass_fds:
            os.fstat(fd)
        fds = plan.pass_fds
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize('overrides', [
    {'memory_mib': 1025}, {'memory_mib': True}, {'cpus': 3}, {'cpus': True},
    {'lifetime_seconds': 121}, {'lifetime_seconds': 0},
    {'protected_directories': [Path('/home')]}, {'protected_directories': (Path('relative'),)},
])
def test_invalid_configuration_fails_closed(tmp_path, overrides):
    with pytest.raises(ValueError, match='Invalid synthetic VM policy'):
        fixture_policy(tmp_path, **overrides)


@pytest.mark.parametrize('protection', ['same', 'parent', 'runtime', 'runtime_child'])
def test_contradictory_mount_and_protected_directory_grants_deny(tmp_path, protection):
    targets = {'same': tmp_path/'base.qcow2', 'parent': tmp_path,
               'runtime': Path('/usr'), 'runtime_child': Path('/usr/local/private')}
    policy = fixture_policy(tmp_path, protected_directories=(targets[protection],))
    with pytest.raises(ValueError, match='Synthetic VM sandbox unavailable'):
        with sandbox_plan(policy):
            pytest.fail('Should not produce a mount plan')


def test_symlink_images_and_symlink_parents_are_not_followed(tmp_path):
    policy = fixture_policy(tmp_path)
    link = tmp_path / 'link.qcow2'
    link.symlink_to(policy.base_image)
    with pytest.raises(ValueError, match='Synthetic VM sandbox unavailable'):
        with sandbox_plan(SyntheticVMPolicy(link, policy.overlay)):
            pytest.fail('Should not follow file symlink')
    parent = tmp_path / 'linkdir'
    parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='Synthetic VM sandbox unavailable'):
        with sandbox_plan(SyntheticVMPolicy(parent/'base.qcow2', policy.overlay)):
            pytest.fail('Should not follow directory symlink')


def test_file_descriptors_pin_image_inodes_across_path_replacement(tmp_path):
    policy = fixture_policy(tmp_path)
    with sandbox_plan(policy) as plan:
        policy.base_image.rename(tmp_path/'old.qcow2')
        policy.base_image.write_bytes(b'REPLACEMENT')
        assert os.read(plan.pass_fds[1], 100) == b'SYNTHETIC_NOT_A_REAL_IMAGE'


def test_image_access_permissions_and_policy_are_immutable(tmp_path):
    policy = fixture_policy(tmp_path)
    with pytest.raises(FrozenInstanceError):
        policy.cpus = 2
    policy.base_image.chmod(0o644)
    with pytest.raises(ValueError, match='Synthetic VM sandbox unavailable'):
        with sandbox_plan(policy):
            pytest.fail('Should refuse non-private image')


@pytest.mark.parametrize('operation', ['shell', 'qmp', 'connectivity;id', '$(id)', 'os_runtime\ncat /etc/shadow'])
def test_no_caller_selected_remote_commands(operation):
    with pytest.raises(ValueError, match='Unsupported synthetic diagnostic'):
        diagnostic_script(operation)


def test_fixed_diagnostics_have_only_exact_os_read_sources():
    assert '/proc/uptime' in diagnostic_script('os_runtime')
    assert '/proc/meminfo' in diagnostic_script('capacity')
    assert diagnostic_script('connectivity') == "printf '{\"reachable\":true}\\n'"
