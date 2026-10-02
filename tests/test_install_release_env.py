"""Isolated checks for release service configuration; never run the installer."""
import os
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / 'deploy/check-service-env.sh'
INSTALLER = ROOT / 'deploy/install-release.sh'


def check(target, example, initialize='no', uid=None, gid=None):
    return subprocess.run(
        ['sh', str(CHECK), str(target), str(example),
         str(os.getuid() if uid is None else uid),
         str(os.getgid() if gid is None else gid), initialize],
        capture_output=True, text=True, check=False,
    )


def fixture(tmp_path):
    target = tmp_path / 'service.env'
    example = tmp_path / 'service.env.example'
    example.write_bytes(b'SYNTHETIC_EXAMPLE=only\n')
    return target, example


@pytest.mark.parametrize('initialize', ['no', 'yes'])
@pytest.mark.parametrize('mode', [0o600, 0o640])
def test_upgrade_preserves_existing_bytes_and_metadata(tmp_path, initialize, mode):
    target, example = fixture(tmp_path)
    original = b'REVIEWED_PRIVATE_SYNTHETIC=unchanged\n\x00opaque bytes\n'
    target.write_bytes(original)
    target.chmod(mode)
    before = target.stat()

    result = check(target, example, initialize)

    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == original
    after = target.stat()
    assert (after.st_ino, after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
        before.st_ino, before.st_uid, before.st_gid, mode)


def test_first_install_requires_explicit_choice_and_creates_private_file(tmp_path):
    target, example = fixture(tmp_path)
    refused = check(target, example)
    assert refused.returncode != 0
    assert not target.exists()

    created = check(target, example, 'yes')
    assert created.returncode == 0, created.stderr
    assert target.read_bytes() == example.read_bytes()
    assert (target.stat().st_uid, target.stat().st_gid, stat.S_IMODE(target.stat().st_mode)) == (
        os.getuid(), os.getgid(), 0o640)


@pytest.mark.parametrize('unsafe', ['symlink', 'broken_symlink', 'directory', 'world_readable',
                                    'group_writable', 'wrong_owner'])
def test_unsafe_existing_file_fails_without_replacement(tmp_path, unsafe):
    target, example = fixture(tmp_path)
    if unsafe in {'symlink', 'broken_symlink'}:
        destination = example if unsafe == 'symlink' else tmp_path / 'absent'
        target.symlink_to(destination)
    elif unsafe == 'directory':
        target.mkdir()
    else:
        target.write_bytes(b'SYNTHETIC_EXISTING=keep\n')
        target.chmod({'world_readable': 0o644, 'group_writable': 0o660}.get(unsafe, 0o600))
    expected = target.read_bytes() if target.is_file() and not target.is_symlink() else None

    result = check(target, example, 'yes', uid=os.getuid() + (unsafe == 'wrong_owner'))

    assert result.returncode != 0
    assert target.is_symlink() if 'symlink' in unsafe else target.exists()
    if expected is not None:
        assert target.read_bytes() == expected


@pytest.mark.parametrize('kind', ['directory', 'symlink_directory', 'regular_file', 'false_success'])
def test_first_install_race_fails_closed_without_replacing_target(tmp_path, kind):
    target, example = fixture(tmp_path)
    injected = tmp_path / 'bin'
    injected.mkdir()
    wrapper = injected / 'ln'
    wrapper.write_text('''#!/bin/sh
case "$RACE_KIND" in
  directory) mkdir -- "$RACE_TARGET" ;;
  symlink_directory) mkdir -- "${RACE_TARGET}.actual"; /usr/bin/ln -s -- "${RACE_TARGET}.actual" "$RACE_TARGET" ;;
  regular_file) printf 'CONCURRENT=keep\\n' > "$RACE_TARGET" ;;
  false_success) exit 0 ;;
esac
exec /usr/bin/ln "$@"
''')
    wrapper.chmod(0o755)
    result = subprocess.run(
        ['sh', str(CHECK), str(target), str(example), str(os.getuid()), str(os.getgid()), 'yes'],
        env={**os.environ, 'PATH': str(injected) + ':' + os.environ['PATH'],
             'RACE_KIND': kind, 'RACE_TARGET': str(target)},
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0, kind
    assert not list(tmp_path.glob('service.env.tmp.*'))
    if kind == 'directory':
        assert target.is_dir() and not target.is_symlink()
        assert list(target.iterdir()) == []
    elif kind == 'symlink_directory':
        assert target.is_symlink() and target.is_dir()
        assert target.resolve() == tmp_path / 'service.env.actual'
        assert list(target.iterdir()) == []
    elif kind == 'regular_file':
        assert target.is_file() and not target.is_symlink()
        assert target.read_bytes() == b'CONCURRENT=keep\n'
    else:
        assert not target.exists() and not target.is_symlink()


def test_validation_precedes_activation_and_example_is_never_installed_over_target():
    script = INSTALLER.read_text()
    check_at = script.index('/check-service-env.sh')
    final_check_at = script.rindex('/check-service-env.sh')
    assert final_check_at > check_at
    assert check_at < script.index('/opt/workbench-runtime/uv sync')
    assert final_check_at < script.index('install -o root -g root -m 0644 deploy/workbench.service')
    assert final_check_at < script.index('mv -Tf /opt/workbench/current.next')
    assert final_check_at < script.index('systemctl daemon-reload')
    assert 'install -o root -g workbench -m 0640 deploy/service.env.example' not in script
