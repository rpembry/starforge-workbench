"""Private durable user-systemd invocation receipts for one exact unit.

Reviewed ExecStartPre/ExecStopPost hooks may call this module with systemd's
INVOCATION_ID. The adapter records its captured stop generation in the same
SQLite ledger before signaling. Missing, duplicate, corrupt, or reordered
evidence is never a completion proof. No hook is installed by this module.
"""

import argparse
from contextlib import closing
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from .gpu_registry import RegistryError, private_file, private_parent
from .gpu_unit_controller import ProcessGeneration, UnitGeneration


class ReceiptError(RuntimeError):
    pass


def _unit(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', value):
        raise ReceiptError('Exact user service required')
    return value


def _invocation(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ReceiptError('Systemd invocation identity unavailable')
    return value


class UnitReceiptLedger:
    def __init__(self, path: Path, *, boot_reader=None):
        self.path = Path(path)
        self._boot_reader = boot_reader or (lambda: Path('/proc/sys/kernel/random/boot_id').read_text())
        if not self.path.is_absolute():
            raise ReceiptError('Absolute private receipt path required')
        private_parent(self.path)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        private_file(self.path)
        try:
            with closing(sqlite3.connect(self.path, timeout=1)) as db:
                with db:
                    db.execute('PRAGMA journal_mode=DELETE')
                    db.execute('PRAGMA synchronous=FULL')
                    db.execute('CREATE TABLE IF NOT EXISTS receipts ('
                               'seq INTEGER PRIMARY KEY, boot TEXT NOT NULL, unit TEXT NOT NULL, '
                               'invocation TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, '
                               'UNIQUE(boot,unit,invocation,kind))')
        except sqlite3.Error as exc:
            raise ReceiptError('Receipt ledger unavailable') from exc

    def boot_id(self) -> str:
        try:
            value = self._boot_reader().strip()
        except OSError as exc:
            raise ReceiptError('Boot identity unavailable') from exc
        if not value or len(value) > 180:
            raise ReceiptError('Boot identity unavailable')
        return value

    def _connect(self, *, readonly=False):
        private_parent(self.path)
        private_file(self.path)
        if readonly:
            uri = f'file:{quote(str(self.path), safe="/")}?mode=ro'
            return sqlite3.connect(uri, uri=True, timeout=1)
        return sqlite3.connect(self.path, timeout=1)

    def _append(self, unit: str, invocation: str, kind: str, payload: str = '') -> None:
        _unit(unit)
        _invocation(invocation)
        if kind not in ('start', 'end', 'capture') or len(payload) > 16_000:
            raise ReceiptError('Invalid receipt')
        try:
            with closing(self._connect()) as db:
                with db:
                    db.execute('PRAGMA synchronous=FULL')
                    db.execute('INSERT INTO receipts(boot,unit,invocation,kind,payload) '
                               'VALUES(?,?,?,?,?)',
                               (self.boot_id(), unit, invocation, kind, payload))
        except (sqlite3.Error, RegistryError, OSError) as exc:
            raise ReceiptError('Receipt could not be committed') from exc

    def record_start(self, unit: str, invocation: str) -> None:
        self._append(unit, invocation, 'start')

    def record_end(self, unit: str, invocation: str) -> None:
        self._append(unit, invocation, 'end')

    def record_capture(self, captured: UnitGeneration) -> None:
        if (captured.state != 'active' or not captured.cgroup or
                captured.cgroup_inode <= 0 or not captured.processes or
                captured.main_pid not in {p.pid for p in captured.processes}):
            raise ReceiptError('Complete active generation required')
        payload = json.dumps(asdict(captured), sort_keys=True, separators=(',', ':'))
        prior = self.latest_capture(captured.unit)
        if prior == captured:
            return  # Retrying the same captured stop after an adapter crash.
        self._append(captured.unit, captured.invocation, 'capture', payload)

    def _events(self, unit: str):
        try:
            _unit(unit)
            with closing(self._connect(readonly=True)) as db:
                rows = db.execute('SELECT seq,invocation,kind,payload FROM receipts '
                                  'WHERE boot=? AND unit=? ORDER BY seq LIMIT 4097',
                                  (self.boot_id(), unit)).fetchall()
            if len(rows) > 4096:
                return None
            seen = set()
            for seq, invocation, kind, payload in rows:
                if (type(seq) is not int or seq <= 0 or
                        not isinstance(payload, str) or len(payload) > 16_000 or
                        kind not in ('start', 'end', 'capture')):
                    return None
                _invocation(invocation)
                if (invocation, kind) in seen:
                    return None
                seen.add((invocation, kind))
                if kind in ('start', 'end') and payload:
                    return None
                if kind == 'capture':
                    captured = self._decode_capture(payload)
                    if (captured is None or captured.unit != unit or
                            captured.invocation != invocation):
                        return None
            return rows
        except (ReceiptError, RegistryError, sqlite3.Error, OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _decode_capture(payload: str) -> UnitGeneration | None:
        try:
            data = json.loads(payload)
            if set(data) != {'unit', 'state', 'invocation', 'cgroup', 'main_pid',
                             'processes', 'start_job', 'cgroup_inode'}:
                return None
            _unit(data['unit'])
            _invocation(data['invocation'])
            if (data['state'] != 'active' or not isinstance(data['cgroup'], str) or
                    not data['cgroup'].startswith('/user.slice/') or
                    type(data['main_pid']) is not int or data['main_pid'] <= 0 or
                    type(data['cgroup_inode']) is not int or data['cgroup_inode'] <= 0 or
                    type(data['start_job']) not in (bool, type(None)) or
                    not isinstance(data['processes'], list) or
                    not 0 < len(data['processes']) <= 4096):
                return None
            processes = []
            for row in data['processes']:
                if (set(row) != {'pid', 'start_ticks'} or
                        any(type(row[name]) is not int or row[name] <= 0
                            for name in ('pid', 'start_ticks'))):
                    return None
                processes.append(ProcessGeneration(**row))
            if (len({p.pid for p in processes}) != len(processes) or
                    data['main_pid'] not in {p.pid for p in processes}):
                return None
            return UnitGeneration(data['unit'], data['state'], data['invocation'],
                                  data['cgroup'], data['main_pid'], tuple(processes),
                                  data['start_job'], data['cgroup_inode'])
        except (TypeError, KeyError, ValueError, ReceiptError):
            return None

    def latest_capture(self, unit: str) -> UnitGeneration | None:
        events = self._events(unit)
        if events is None:
            return None
        captures = [row for row in events if row[2] == 'capture']
        if not captures:
            return None
        captured = self._decode_capture(captures[-1][3])
        return captured if captured is not None and captured.unit == unit else None

    def started(self, unit: str, invocation: str) -> bool:
        events = self._events(unit)
        if events is None:
            return False
        starts = [row for row in events if row[2] == 'start']
        return bool(starts and starts[-1][1] == invocation and
                    not any(row[2] == 'end' and row[1] == invocation for row in events))

    def completed(self, unit: str, invocation: str, *, captured: UnitGeneration | None = None) -> bool:
        """Require matching start/end and no later replacement start."""
        events = self._events(unit)
        if events is None:
            return False
        starts = [row for row in events if row[2] == 'start']
        if not starts or starts[-1][1] != invocation:
            return False
        start_seq = starts[-1][0]
        ends = [row for row in events if row[2] == 'end' and row[1] == invocation]
        if len(ends) != 1 or ends[0][0] <= start_seq:
            return False
        if captured is not None:
            if captured.unit != unit or captured.invocation != invocation:
                return False
            matches = [row for row in events if row[2] == 'capture' and
                       row[1] == invocation and self._decode_capture(row[3]) == captured]
            if len(matches) != 1 or not start_seq < matches[0][0] < ends[0][0]:
                return False
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Private systemd lifecycle receipt hook')
    parser.add_argument('--ledger', required=True, type=Path)
    parser.add_argument('--unit', required=True)
    parser.add_argument('--event', required=True, choices=('start', 'end'))
    args = parser.parse_args(argv)
    try:
        ledger = UnitReceiptLedger(args.ledger)
        invocation = os.environ.get('INVOCATION_ID', '')
        if args.event == 'start':
            ledger.record_start(args.unit, invocation)
        else:
            ledger.record_end(args.unit, invocation)
    except (ReceiptError, RegistryError, OSError):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
