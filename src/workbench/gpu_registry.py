"""Private transactional state for a single host-local GPU policy adapter."""

from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import sqlite3
import stat

from .gpu_policy import AcquireRecord, Lease, PolicySnapshot


class RegistryError(RuntimeError):
    pass


def private_parent(path: Path) -> None:
    parent = path.parent
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            info.st_mode & 0o077):
        raise RegistryError("Registry parent must be an owned private directory")


def private_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            info.st_mode & 0o777 != 0o600 or info.st_size > 16_000_000):
        raise RegistryError("Registry must be a bounded owned 0600 regular file")


def encode(snapshot: PolicySnapshot) -> str:
    return json.dumps({'schema': 1, 'clock_id': snapshot.clock_id,
                       'leases': [asdict(row) for row in snapshot.leases],
                       'acquisitions': [asdict(row) for row in snapshot.acquisitions]},
                      sort_keys=True, separators=(',', ':'))


def decode(value: str) -> PolicySnapshot:
    try:
        data = json.loads(value)
        if set(data) != {'schema', 'clock_id', 'leases', 'acquisitions'} or data['schema'] != 1:
            raise ValueError("Unknown policy schema")
        if not isinstance(data['clock_id'], str) or not data['clock_id']:
            raise ValueError("Invalid clock identity")
        if (not isinstance(data['leases'], list) or not isinstance(data['acquisitions'], list) or
                len(data['leases']) > 4096 or len(data['acquisitions']) > 4096):
            raise ValueError("Invalid registry size")
        for row in data['leases']:
            if (not isinstance(row, dict) or set(row) != {'owner', 'token', 'expires_at', 'granted', 'stale'} or
                    not isinstance(row['owner'], str) or not row['owner'] or
                    not isinstance(row['token'], str) or not row['token'] or
                    type(row['expires_at']) not in (int, float) or
                    not math.isfinite(row['expires_at']) or
                    type(row['granted']) is not bool or type(row['stale']) is not bool):
                raise ValueError("Invalid lease record")
        for row in data['acquisitions']:
            if (not isinstance(row, dict) or
                    set(row) != {'owner', 'request_key', 'ttl', 'token', 'retain_until'} or
                    not all(isinstance(row[name], str) and row[name]
                            for name in ('owner', 'request_key', 'token')) or
                    type(row['ttl']) not in (int, float) or not (0 < row['ttl'] <= 3600) or
                    (row['retain_until'] is not None and
                     (type(row['retain_until']) not in (int, float) or
                      not math.isfinite(row['retain_until'])))):
                raise ValueError("Invalid acquisition record")
        leases = tuple(Lease(**row) for row in data['leases'])
        acquisitions = tuple(AcquireRecord(**row) for row in data['acquisitions'])
        if len({r.token for r in leases}) != len(leases):
            raise ValueError("Duplicate lease token")
        if len({(r.owner, r.request_key) for r in acquisitions}) != len(acquisitions):
            raise ValueError("Duplicate acquisition")
        return PolicySnapshot(data['clock_id'], leases, acquisitions)
    except (TypeError, KeyError, ValueError) as exc:
        raise RegistryError("Invalid private policy registry") from exc


class Registry:
    """One SQLite writer; callers serialize policy changes with an outer lock."""

    def __init__(self, path: Path, clock_id: str):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise RegistryError("Registry path must be absolute")
        private_parent(self.path)
        existed = self.path.exists() or self.path.is_symlink()
        if existed:
            private_file(self.path)
        else:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        private_file(self.path)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        try:
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK(id=1), snapshot TEXT NOT NULL, revision INTEGER NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS effect (id INTEGER PRIMARY KEY CHECK(id=1), action TEXT NOT NULL, revision INTEGER NOT NULL, acknowledged INTEGER NOT NULL)')
            row = self.db.execute('SELECT snapshot FROM policy WHERE id=1').fetchone()
            if row is None:
                if existed:
                    raise RegistryError("Existing registry has no policy state")
                self.db.execute('INSERT INTO policy(id,snapshot,revision) VALUES (1,?,0)',
                                (encode(PolicySnapshot(clock_id, (), ())),))
            else:
                decode(row[0])
        except (sqlite3.DatabaseError, RegistryError) as exc:
            self.db.close()
            raise RegistryError("Invalid private policy registry") from exc

    def close(self):
        self.db.close()

    def load(self) -> tuple[PolicySnapshot, int]:
        row = self.db.execute('SELECT snapshot,revision FROM policy WHERE id=1').fetchone()
        if row is None:
            raise RegistryError("Missing policy state")
        return decode(row[0]), row[1]

    def save(self, snapshot: PolicySnapshot, effect: str | None = None) -> int:
        if effect not in (None, 'START', 'STOP'):
            raise ValueError("Unsupported controller effect")
        self.db.execute('BEGIN IMMEDIATE')
        try:
            revision = self.db.execute('SELECT revision FROM policy WHERE id=1').fetchone()[0] + 1
            self.db.execute('UPDATE policy SET snapshot=?,revision=? WHERE id=1',
                            (encode(snapshot), revision))
            self.db.execute('DELETE FROM effect')
            if effect:
                self.db.execute('INSERT INTO effect(id,action,revision,acknowledged) VALUES (1,?,?,0)',
                                (effect, revision))
            self.db.commit()
            return revision
        except BaseException:
            self.db.rollback()
            raise

    def pending_effect(self) -> tuple[str, int] | None:
        row = self.db.execute('SELECT action,revision FROM effect WHERE id=1 AND acknowledged=0').fetchone()
        return (row[0], row[1]) if row else None

    def acknowledge(self, revision: int) -> None:
        self.db.execute('UPDATE effect SET acknowledged=1 WHERE id=1 AND revision=?', (revision,))
