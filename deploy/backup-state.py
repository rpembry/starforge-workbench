"""Run as workbench on the server before upgrading its database schema."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

root = Path('/var/lib/workbench')
backups = root/'backups'
backups.mkdir(mode=0o700, exist_ok=True)
if backups.is_symlink() or backups.stat().st_mode & 0o077:
    raise SystemExit('Backup directory must be private and not a symlink')
path = backups/('workbench-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite')
fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
with sqlite3.connect('file:/var/lib/workbench/workbench.sqlite?mode=ro', uri=True) as source, sqlite3.connect(path) as dest:
    source.backup(dest)
    if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise SystemExit('Backup integrity check failed')
print(path)
