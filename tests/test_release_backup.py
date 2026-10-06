"""Synthetic release tests; no live service or database is touched."""
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / 'deploy/backup-state.py'
INSTALLER = ROOT / 'deploy/install-release.sh'


def backup(database, dest):
    return subprocess.run([sys.executable, str(BACKUP), '--database', str(database), '--dest', str(dest)],
                          capture_output=True, text=True, check=False)


def test_backup_is_private_and_integrity_checked(tmp_path):
    database = tmp_path / 'synthetic.sqlite'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE sample (value TEXT)')
        db.execute("INSERT INTO sample VALUES ('synthetic')")
    result = backup(database, tmp_path / 'backups')
    assert result.returncode == 0, result.stderr
    saved = Path(result.stdout.strip())
    assert saved.parent == tmp_path / 'backups'
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    with sqlite3.connect(saved) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT value FROM sample').fetchone()[0] == 'synthetic'


def test_backup_refuses_symlink_and_group_readable_destination(tmp_path):
    database = tmp_path / 'synthetic.sqlite'
    sqlite3.connect(database).close()
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    link = tmp_path / 'linked'
    link.symlink_to(private, target_is_directory=True)
    assert backup(database, link).returncode != 0
    private.chmod(0o750)
    assert backup(database, private).returncode != 0
    assert not list(private.iterdir())


def _fake(bin_dir, name, script):
    path = bin_dir / name
    path.write_text('#!/bin/sh\nset -eu\n' + script)
    path.chmod(0o755)


def _run_installer(tmp_path, health_ok, backup_ok=True, first_install=False,
                   configured_db=None, env_body=None, db_present=True, default_decoy=False):
    opt = tmp_path / 'opt/workbench'
    release = opt / 'releases/new'
    old = opt / 'releases/old'
    release.mkdir(parents=True)
    old.mkdir()
    if not first_install:
        (opt / 'current').symlink_to(old)
    (release / 'uv.lock').write_text('synthetic')
    (release / 'deploy').mkdir()
    (release / '.venv/bin').mkdir(parents=True)
    (release / '.venv/bin/python').symlink_to(sys.executable)
    for name in ('workbench.service', 'workbench-tunnel.service', 'service.env.example'):
        (release / 'deploy' / name).write_text('synthetic')
    (release / 'deploy/service-database.py').write_bytes((ROOT / 'deploy/service-database.py').read_bytes())
    check = release / 'deploy/check-service-env.sh'
    check.write_text('#!/bin/sh\nexit 0\n')
    check.chmod(0o755)
    etc = tmp_path / 'etc/workbench'
    etc.mkdir(parents=True)
    for name in ('access.json', 'tunnel-token'):
        (etc / name).write_text('synthetic')
    (tmp_path / 'etc/systemd/system').mkdir(parents=True)
    state = tmp_path / 'var/lib/workbench'
    state.mkdir(parents=True)
    database = configured_db or state / 'workbench.sqlite'
    (etc / 'service.env').write_text(env_body if env_body is not None else f'WB_DATABASE={database}\n')
    if not first_install and db_present:
        database.parent.mkdir(parents=True, exist_ok=True)
        database.write_text('synthetic')
    if default_decoy:
        (state / 'workbench.sqlite').write_text('decoy')
    runtime = tmp_path / 'opt/workbench-runtime'
    runtime.mkdir()
    _fake(runtime, 'uv', 'exit 0\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    _fake(bin_dir, 'id', 'case "$1" in -u) if [ "$#" -eq 1 ]; then echo 0; else echo 1001; fi;; esac\n')
    _fake(bin_dir, 'getent', 'echo "workbench:x:1001:1001"\n')
    _fake(bin_dir, 'chown', 'exit 0\n')
    _fake(bin_dir, 'install', 'for arg do previous=${last-}; last=$arg; done\ncp "$previous" "$last"\n')
    _fake(bin_dir, 'systemctl', '''printf 'systemctl %s current=%s rollback=%s\n' "$1" "$(readlink "$CURRENT_LINK")" "$(cat "$ROLLBACK_RECORD" 2>/dev/null || echo missing)" >> "$EVENTS"
if [ "$FIRST_INSTALL" = yes ] && [ "$1" = stop ]; then exit 5; fi
exit 0
''')
    _fake(bin_dir, 'runuser', '''database=missing
while [ "$#" -gt 0 ]; do
    if [ "$1" = --database ]; then database=$2; break; fi
    shift
done
printf 'backup source=%s current=%s\n' "$database" "$(readlink "$CURRENT_LINK")" >> "$EVENTS"
if [ ! -f "$database" ]; then exit 1; fi
if [ "$BACKUP_OK" = no ]; then exit 1; fi
mkdir -p "$BACKUP_DIR"
: > "$BACKUP_DIR/synthetic-backup.sqlite"
printf '%s\n' "$BACKUP_DIR/synthetic-backup.sqlite"
''')
    _fake(bin_dir, 'curl', 'if [ "$HEALTH_OK" = yes ]; then exit 0; else exit 1; fi\n')
    _fake(bin_dir, 'sleep', 'exit 0\n')
    script = tmp_path / 'synthetic-installer.sh'
    script.write_text(INSTALLER.read_text().replace('/opt/workbench', str(opt))
                      .replace('/etc/workbench', str(etc))
                      .replace('/etc/systemd/system', str(tmp_path / 'etc/systemd/system'))
                      .replace('/var/lib/workbench', str(state)))
    events = tmp_path / 'events'
    result = subprocess.run(['sh', str(script), str(release)], capture_output=True, text=True,
                            env={**os.environ, 'PATH': str(bin_dir) + ':' + os.environ['PATH'],
                                 'EVENTS': str(events), 'CURRENT_LINK': str(opt / 'current'),
                                 'ROLLBACK_RECORD': str(opt / 'rollback-target'),
                                 'BACKUP_DIR': str(state / 'backups'),
                                 'BACKUP_OK': 'yes' if backup_ok else 'no',
                                 'FIRST_INSTALL': 'yes' if first_install else 'no',
                                 'HEALTH_OK': 'yes' if health_ok else 'no'}, check=False)
    return result, events.read_text().splitlines() if events.exists() else [], opt, old, release


def test_installer_backs_up_and_records_target_before_switch_then_requires_health(tmp_path):
    result, events, opt, old, release = _run_installer(tmp_path, health_ok=False)
    assert result.returncode != 0, result.stderr
    assert (opt / 'current').resolve() == release
    assert (opt / 'rollback-target').read_text().strip() == str(old)
    assert stat.S_IMODE((opt / 'rollback-target').stat().st_mode) == 0o600
    assert events.index(f'backup source={tmp_path / "var/lib/workbench/workbench.sqlite"} current={old}') > events.index(
        f'systemctl stop current={old} rollback=missing')
    assert f'systemctl daemon-reload current={release} rollback={old}' in events
    assert 'health check failed' in result.stderr
    assert str(opt / 'rollback-target') in result.stderr
    assert str(tmp_path / 'var/lib/workbench/backups/synthetic-backup.sqlite') in result.stderr


def test_installer_accepts_health_response(tmp_path):
    result, _, _, _, _ = _run_installer(tmp_path, health_ok=True)
    assert result.returncode == 0, result.stderr
    assert 'health check passed' in result.stdout


def test_backup_failure_preserves_previous_release_and_restarts_service(tmp_path):
    result, events, opt, old, _ = _run_installer(tmp_path, health_ok=True, backup_ok=False)
    assert result.returncode != 0
    assert (opt / 'current').resolve() == old
    assert not (opt / 'rollback-target').exists()
    assert f'systemctl start current={old} rollback=missing' in events
    assert 'backup failed' in result.stderr


def test_first_install_tolerates_missing_prior_service_and_records_none(tmp_path):
    result, events, opt, _, release = _run_installer(tmp_path, health_ok=True, first_install=True)
    assert result.returncode == 0, result.stderr
    assert (opt / 'current').resolve() == release
    assert (opt / 'rollback-target').read_text().strip() == 'none'
    assert not any(event.startswith('backup ') for event in events)


def test_installer_backs_up_configured_database_not_default_decoy(tmp_path):
    custom = tmp_path / 'var/lib/workbench/custom.sqlite'
    result, events, opt, old, _ = _run_installer(
        tmp_path, health_ok=True, configured_db=custom, default_decoy=True)
    assert result.returncode == 0, result.stderr
    assert f'backup source={custom} current={old}' in events
    assert f'Database source: {custom}; backup:' in result.stdout
    assert (opt / 'rollback-target').read_text().strip() == str(old)


def test_installer_fails_before_stop_when_configured_database_missing(tmp_path):
    custom = tmp_path / 'var/lib/workbench/missing.sqlite'
    result, events, opt, old, _ = _run_installer(
        tmp_path, health_ok=True, configured_db=custom, db_present=False)
    assert result.returncode != 0
    assert not events
    assert (opt / 'current').resolve() == old
    assert 'Configured database is missing' in result.stderr


@pytest.mark.parametrize('env_body', [
    'WB_DATABASE="/var/lib/workbench/workbench.sqlite"\n',
    'WB_DATABASE=/var/lib/workbench/a.sqlite\nWB_DATABASE=/var/lib/workbench/b.sqlite\n',
    'WB_DATABASE=$(touch /tmp/synthetic-never-run)\n',
    'WB_OTHER=/var/lib/workbench/workbench.sqlite\n',
])
def test_installer_fails_before_stop_on_unparseable_database_config(tmp_path, env_body):
    result, events, opt, old, _ = _run_installer(tmp_path, health_ok=True, env_body=env_body)
    assert result.returncode != 0
    assert not events
    assert (opt / 'current').resolve() == old
    assert 'Cannot determine configured database' in result.stderr
