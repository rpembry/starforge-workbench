"""Install an extracted collector release as the current user; no provider control."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

release = Path(sys.argv[1]).resolve()
base = Path.home()/'.local/share/starforge-ai-workbench'
if release.parent != base/'collector-releases' or not (release/'uv.lock').is_file():
    raise SystemExit('Use an extracted release under ~/.local/share/starforge-ai-workbench/collector-releases')
subprocess.run([str(Path.home()/'.local/bin/uv'), 'sync', '--frozen', '--no-dev', '--no-editable', '--python', '3.12'], cwd=release, check=True)
config = Path.home()/'.config/starforge-ai-workbench'
source = config/'client.json'
if source.is_symlink() or source.stat().st_uid != os.getuid() or source.stat().st_mode & 0o077:
    raise SystemExit('Client source must be private and owned by you')
dest = config/'collector.json'
if not dest.exists():
    data=json.loads(source.read_text())
    if data.get('auth_type') != 'cloudflare':
        raise SystemExit('Expected Cloudflare credentials')
    fd=os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump({k: data[k] for k in ('url', 'auth_type', 'collector')}, out)
if dest.is_symlink() or dest.stat().st_uid != os.getuid() or dest.stat().st_mode & 0o077:
    raise SystemExit('Collector credential file must be private and owned by you')
next_link=base/'collector-current.next'
next_link.symlink_to(release)
next_link.replace(base/'collector-current')
units=Path.home()/'.config/systemd/user'
units.mkdir(parents=True, exist_ok=True)
shutil.copyfile(release/'deploy/workbench-collector.service', units/'workbench-collector.service')
subprocess.run(['systemd-analyze', '--user', 'verify', str(units/'workbench-collector.service')], check=True)
subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
subprocess.run(['systemctl', '--user', 'enable', '--now', 'workbench-collector.service'], check=True)
subprocess.run(['systemctl', '--user', 'restart', 'workbench-collector.service'], check=True)

if '--with-observer' in sys.argv[2:]:
    if not (config/'worklog-bootstrap.json').is_file():
        raise SystemExit('Create protected Worklog bootstrap config before enabling the observer')
    shutil.copyfile(release/'deploy/workbench-observer.service', units/'workbench-observer.service')
    subprocess.run(['systemd-analyze', '--user', 'verify', str(units/'workbench-observer.service')], check=True)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'workbench-observer.service'], check=True)
    subprocess.run(['systemctl', '--user', 'restart', 'workbench-observer.service'], check=True)

if '--with-claude' in sys.argv[2:]:
    shutil.copyfile(release/'deploy/workbench-claude-observer.service', units/'workbench-claude-observer.service')
    subprocess.run(['systemd-analyze', '--user', 'verify', str(units/'workbench-claude-observer.service')], check=True)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'workbench-claude-observer.service'], check=True)
    subprocess.run(['systemctl', '--user', 'restart', 'workbench-claude-observer.service'], check=True)

if '--with-opencode' in sys.argv[2:]:
    shutil.copyfile(release/'deploy/workbench-opencode-observer.service', units/'workbench-opencode-observer.service')
    subprocess.run(['systemd-analyze', '--user', 'verify', str(units/'workbench-opencode-observer.service')], check=True)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'workbench-opencode-observer.service'], check=True)
    subprocess.run(['systemctl', '--user', 'restart', 'workbench-opencode-observer.service'], check=True)
