"""Install an extracted collector release as the current user; no provider control."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def install(release: Path, options: tuple[str, ...] = ()) -> None:
    release = release.resolve()
    home = Path.home()
    base = home / '.local/share/starforge-ai-workbench'
    if release.parent != base / 'collector-releases' or not (release / 'uv.lock').is_file():
        raise SystemExit('Use an extracted release under ~/.local/share/starforge-ai-workbench/collector-releases')

    config = home / '.config/starforge-ai-workbench'
    manifest = config / 'workbench.yaml'
    if manifest.is_symlink() or not manifest.is_file():
        raise SystemExit(f'Collector manifest must be a regular file before enabling the unit: {manifest}')
    next_link = base / 'collector-current.next'
    if next_link.is_symlink():
        next_link.unlink()
    elif next_link.exists():
        raise SystemExit(f'Refusing to replace non-symlink staging path: {next_link}')

    subprocess.run([str(home / '.local/bin/uv'), 'sync', '--frozen', '--no-dev',
                    '--no-editable', '--python', '3.12'], cwd=release, check=True)
    source = config / 'client.json'
    if source.is_symlink() or source.stat().st_uid != os.getuid() or source.stat().st_mode & 0o077:
        raise SystemExit('Client source must be private and owned by you')
    dest = config / 'collector.json'
    if not dest.exists():
        data = json.loads(source.read_text())
        if data.get('auth_type') != 'cloudflare':
            raise SystemExit('Expected Cloudflare credentials')
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump({k: data[k] for k in ('url', 'auth_type', 'collector')}, out)
    if dest.is_symlink() or dest.stat().st_uid != os.getuid() or dest.stat().st_mode & 0o077:
        raise SystemExit('Collector credential file must be private and owned by you')

    next_link.symlink_to(release)
    next_link.replace(base / 'collector-current')
    units = home / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    restarted = {'workbench-collector.service'}

    def enable_and_restart(name: str) -> None:
        shutil.copyfile(release / 'deploy' / name, units / name)
        subprocess.run(['systemd-analyze', '--user', 'verify', str(units / name)], check=True)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', name], check=True)
        subprocess.run(['systemctl', '--user', 'restart', name], check=True)
        restarted.add(name)

    enable_and_restart('workbench-collector.service')

    if '--with-observer' in options:
        if not (config / 'worklog-bootstrap.json').is_file():
            raise SystemExit('Create protected Worklog bootstrap config before enabling the observer')
        enable_and_restart('workbench-observer.service')
    if '--with-claude' in options:
        enable_and_restart('workbench-claude-observer.service')
    if '--with-opencode' in options:
        enable_and_restart('workbench-opencode-observer.service')

    for unit in sorted(units.glob('*.service')):
        if unit.name in restarted or 'collector-current' not in unit.read_text():
            continue
        enabled = subprocess.run(['systemctl', '--user', 'is-enabled', '--quiet', unit.name],
                                 check=False)
        if enabled.returncode == 0:
            print(f'Enabled unit still using the previous collector release until restarted: {unit.name}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('Usage: install-collector.py RELEASE [--with-observer] [--with-claude] [--with-opencode]')
    install(Path(sys.argv[1]), tuple(sys.argv[2:]))
