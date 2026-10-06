"""Install an extracted collector release as the current user; no provider control."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def unit_manifest(unit: Path, home: Path, *, dropins: Path | None = None) -> Path:
    """Read the effective user unit command, including local drop-in overrides."""
    command = None
    dropins = dropins or unit.with_name(unit.name + '.d')
    for path in [unit, *sorted(dropins.glob('*.conf'))]:
        for line in path.read_text().splitlines():
            if line.startswith('ExecStart='):
                command = line.partition('=')[2].strip() or None
    if not command:
        raise SystemExit('Collector unit needs an ExecStart command with --manifest')
    try:
        args = shlex.split(command)
        manifest = args[args.index('--manifest') + 1].replace('%h', str(home))
    except (ValueError, IndexError):
        raise SystemExit('Collector unit needs an ExecStart command with --manifest') from None
    path = Path(manifest)
    if not path.is_absolute():
        raise SystemExit('Collector unit manifest must be an absolute path')
    return path


def check_manifest(manifest: Path) -> None:
    if (manifest.is_symlink() or not manifest.is_file() or
            manifest.stat().st_uid != os.getuid() or manifest.stat().st_mode & 0o077):
        raise SystemExit(f'Collector manifest must be a private, owned regular file before enabling the unit: {manifest}')


def install(release: Path, options: tuple[str, ...] = ()) -> None:
    release = release.resolve()
    home = Path.home()
    base = home / '.local/share/starforge-ai-workbench'
    if release.parent != base / 'collector-releases' or not (release / 'uv.lock').is_file():
        raise SystemExit('Use an extracted release under ~/.local/share/starforge-ai-workbench/collector-releases')

    xdg_config = Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config')
    if not xdg_config.is_absolute():
        raise SystemExit('XDG_CONFIG_HOME must be an absolute path')
    config = xdg_config / 'starforge-ai-workbench'
    next_link = base / 'collector-current.next'

    units = home / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    selected = ['workbench-collector.service']
    if '--with-observer' in options:
        if not (config / 'worklog-bootstrap.json').is_file():
            raise SystemExit('Create protected Worklog bootstrap config before enabling the observer')
        selected.append('workbench-observer.service')
    if '--with-claude' in options:
        selected.append('workbench-claude-observer.service')
    if '--with-opencode' in options:
        selected.append('workbench-opencode-observer.service')
    with tempfile.TemporaryDirectory(prefix='.collector-units-', dir=units) as staging:
        stage = Path(staging)
        for name in selected:
            candidate = stage / name
            shutil.copyfile(release / 'deploy' / name, candidate)
            dropins = units / (name + '.d')
            if dropins.is_dir():
                shutil.copytree(dropins, stage / dropins.name)
        # Verify the canonical unit names with their drop-ins in an isolated
        # search path. Verifying a differently named staging file omits them.
        verify_env = {**os.environ, 'SYSTEMD_UNIT_PATH': str(stage) + ':'}
        for name in selected:
            subprocess.run(['systemd-analyze', '--user', 'verify', name],
                           check=True, env=verify_env)
        check_manifest(unit_manifest(stage / 'workbench-collector.service', home))
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

        for name in selected:
            (stage / name).replace(units / name)
        next_link.symlink_to(release)
        next_link.replace(base / 'collector-current')
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    for name in selected:
        subprocess.run(['systemctl', '--user', 'enable', '--now', name], check=True)
        subprocess.run(['systemctl', '--user', 'restart', name], check=True)

    for unit in sorted(units.glob('*.service')):
        if unit.name in selected or 'collector-current' not in unit.read_text():
            continue
        enabled = subprocess.run(['systemctl', '--user', 'is-enabled', '--quiet', unit.name],
                                 check=False)
        if enabled.returncode == 0:
            print(f'Enabled unit still using the previous collector release until restarted: {unit.name}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('Usage: install-collector.py RELEASE [--with-observer] [--with-claude] [--with-opencode]')
    install(Path(sys.argv[1]), tuple(sys.argv[2:]))
