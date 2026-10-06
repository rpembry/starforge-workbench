"""Synthetic checks for the collector deployment recipe; never run systemd."""
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('install_collector', ROOT / 'deploy/install-collector.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
MANIFEST = '%h/.config/starforge-ai-workbench/workbench.yaml'


def test_shipped_collector_units_use_private_manifest():
    paths = list((ROOT / 'deploy').glob('*.service')) + list((ROOT / 'deploy').glob('*.conf.example'))
    manifest_flags = [line.split('--manifest ', 1)[1].split()[0]
                      for path in paths for line in path.read_text().splitlines()
                      if '--manifest ' in line]
    assert len(manifest_flags) >= 3
    assert set(manifest_flags) == {MANIFEST}


def setup_install(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    base = home / '.local/share/starforge-ai-workbench'
    release = base / 'collector-releases/v1'
    (release / 'deploy').mkdir(parents=True)
    (release / 'uv.lock').write_text('synthetic')
    (release / 'deploy/workbench-collector.service').write_text(
        (ROOT / 'deploy/workbench-collector.service').read_text())
    config = home / '.config/starforge-ai-workbench'
    config.mkdir(parents=True)
    source = config / 'client.json'
    source.write_text(json.dumps({'url': 'https://example.invalid', 'auth_type': 'cloudflare',
                                  'collector': {'client_id': 'synthetic', 'client_secret': 'synthetic'}}))
    source.chmod(0o600)
    monkeypatch.setattr(MODULE.Path, 'home', lambda: home)
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(MODULE.subprocess, 'run', fake_run)
    return home, base, release, config, calls


def test_installer_preflight_prevents_enabling_without_manifest(tmp_path, monkeypatch):
    home, base, release, config, calls = setup_install(tmp_path, monkeypatch)
    installed = home / '.config/systemd/user/workbench-collector.service'
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b'[Service]\nExecStart=/bin/old-collector\n')
    original = installed.read_bytes()
    with pytest.raises(SystemExit, match='Collector manifest must be a private, owned regular file'):
        MODULE.install(release)
    assert not any('enable' in call for call in calls)
    assert not (base / 'collector-current').exists()
    assert installed.read_bytes() == original
    (config / 'workbench.yaml').symlink_to(release / 'uv.lock')
    with pytest.raises(SystemExit, match='Collector manifest must be a private, owned regular file'):
        MODULE.install(release)
    assert not any('enable' in call for call in calls)
    assert installed.read_bytes() == original


def test_installer_replaces_stale_staging_link_and_reports_other_enabled_units(
        tmp_path, monkeypatch, capsys):
    home, base, release, config, calls = setup_install(tmp_path, monkeypatch)
    (config / 'workbench.yaml').write_text('contexts: []\n')
    (config / 'workbench.yaml').chmod(0o600)
    stale = base / 'collector-current.next'
    stale.symlink_to(base / 'collector-releases/old')
    units = home / '.config/systemd/user'
    units.mkdir(parents=True)
    (units / 'workbench-opencode-publisher.service').write_text('ExecStart=collector-current/bin/wb-collect\n')
    (units / 'unrelated.service').write_text('ExecStart=/bin/true\n')
    MODULE.install(release)
    assert (base / 'collector-current').resolve() == release
    assert not stale.exists() and not stale.is_symlink()
    assert any(call[-2:] == ('--now', 'workbench-collector.service') for call in calls)
    assert ('systemctl', '--user', 'is-enabled', '--quiet',
            'workbench-opencode-publisher.service') in calls
    assert 'workbench-opencode-publisher.service' in capsys.readouterr().out
    assert not any(call[-1] == 'workbench-opencode-publisher.service' and
                   'restart' in call for call in calls)


def test_installer_refuses_regular_staging_file(tmp_path, monkeypatch):
    home, base, release, config, calls = setup_install(tmp_path, monkeypatch)
    (config / 'workbench.yaml').write_text('contexts: []\n')
    (config / 'workbench.yaml').chmod(0o600)
    staging = base / 'collector-current.next'
    staging.write_text('do not replace')
    with pytest.raises(SystemExit, match='Refusing to replace non-symlink'):
        MODULE.install(release)
    assert staging.read_text() == 'do not replace'
    assert not any('enable' in call for call in calls)


def test_installer_uses_xdg_manifest_named_by_dropin(tmp_path, monkeypatch):
    home, base, release, config, calls = setup_install(tmp_path, monkeypatch)
    xdg = tmp_path / 'custom-config'
    xdg.mkdir()
    custom = xdg / 'starforge-ai-workbench'
    config.rename(custom)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    manifest = custom / 'workbench.yaml'
    manifest.write_text('contexts: []\n')
    manifest.chmod(0o600)
    dropin = home / '.config/systemd/user/workbench-collector.service.d/override.conf'
    dropin.parent.mkdir(parents=True)
    dropin.write_text(f'[Service]\nExecStart=\nExecStart=/bin/wb-collect --manifest {manifest}\n')
    MODULE.install(release)
    assert (base / 'collector-current').resolve() == release
    assert any(call[-2:] == ('--now', 'workbench-collector.service') for call in calls)


def test_installer_refuses_public_manifest_and_keeps_current_on_verify_failure(
        tmp_path, monkeypatch):
    home, base, release, config, calls = setup_install(tmp_path, monkeypatch)
    manifest = config / 'workbench.yaml'
    manifest.write_text('contexts: []\n')
    current = base / 'collector-current'
    current.symlink_to(base / 'collector-releases/old')
    installed = home / '.config/systemd/user/workbench-collector.service'
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b'[Service]\nExecStart=/bin/old-collector\n')
    original = installed.read_bytes()
    with pytest.raises(SystemExit, match='private, owned regular file'):
        MODULE.install(release)
    assert current.readlink() == base / 'collector-releases/old'
    assert installed.read_bytes() == original
    manifest.chmod(0o600)

    def failed_verify(args, **kwargs):
        calls.append(tuple(args))
        if args[0] == 'systemd-analyze':
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(MODULE.subprocess, 'run', failed_verify)
    with pytest.raises(subprocess.CalledProcessError):
        MODULE.install(release)
    assert current.readlink() == base / 'collector-releases/old'
    assert installed.read_bytes() == original
    assert not any('enable' in call for call in calls)


def test_real_systemd_verifier_rejects_invalid_dropin_executable(tmp_path, monkeypatch):
    if not shutil.which('systemd-analyze'):
        pytest.skip('systemd-analyze unavailable')
    real_run = subprocess.run
    home, base, release, config, calls = setup_install(tmp_path, monkeypatch)
    manifest = config / 'workbench.yaml'
    manifest.write_text('contexts: []\n')
    manifest.chmod(0o600)
    (release / 'deploy/workbench-collector.service').write_text(
        f'[Service]\nExecStart=/bin/true --manifest {manifest}\n')
    dropin = home / '.config/systemd/user/workbench-collector.service.d/override.conf'
    dropin.parent.mkdir(parents=True)
    dropin.write_text(f'[Service]\nExecStart=\n'
                      f'ExecStart=/nonexistent/collector-fixture --manifest {manifest}\n')
    installed = dropin.parent.parent / 'workbench-collector.service'
    installed.write_bytes(b'[Service]\nExecStart=/bin/old-collector\n')

    def verify_with_systemd(args, **kwargs):
        calls.append(tuple(args))
        if args[0] == 'systemd-analyze':
            return real_run(args, capture_output=True, text=True, **kwargs)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(MODULE.subprocess, 'run', verify_with_systemd)
    with pytest.raises(subprocess.CalledProcessError) as error:
        MODULE.install(release)
    if 'Operation not permitted' in (error.value.stderr or ''):
        pytest.skip('systemd verifier unavailable in this sandbox')
    assert '/nonexistent/collector-fixture' in (error.value.stderr or '')
    assert installed.read_bytes() == b'[Service]\nExecStart=/bin/old-collector\n'
    assert not (base / 'collector-current').exists()
    assert not any('enable' in call for call in calls)
