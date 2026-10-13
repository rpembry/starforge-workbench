"""Create a private, integrity-checked SQLite backup before a release upgrade."""
import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path('/var/lib/workbench/workbench.sqlite'))
    parser.add_argument('--dest', type=Path, default=Path('/var/lib/workbench/backups'))
    args = parser.parse_args()

    if args.database.is_symlink() or not args.database.is_file():
        raise SystemExit('Database must be an existing regular file, not a symlink')
    backups = args.dest
    if backups.is_symlink():
        raise SystemExit('Backup directory must be private and not a symlink')
    backups.mkdir(mode=0o700, exist_ok=True)
    if backups.is_symlink() or not backups.is_dir() or backups.stat().st_mode & 0o077:
        raise SystemExit('Backup directory must be private and not a symlink')
    path = backups / ('workbench-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(path) as dest:
            source.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise SystemExit('Backup integrity check failed')
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    print(path)


if __name__ == '__main__':
    main()
