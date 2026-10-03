"""Read-only, fail-closed ExecCondition for the owned miner user unit.

This module does not install a drop-in or change a running unit. An operator
may use its exit code in a reviewed ExecCondition: 0 permits a start, 1 skips
it. State faults are indistinguishable from an active hold to the unit.
"""

import argparse
from pathlib import Path
import sqlite3
from urllib.parse import quote

from .gpu_registry import RegistryError, decode, private_file, private_parent


def start_permitted(path: Path) -> bool:
    """Return true only for a valid, owned registry with no durable leases."""
    path = Path(path)
    try:
        if not path.is_absolute():
            return False
        private_parent(path)
        private_file(path)
        # SQLite's read transaction, rather than an unlocked raw file read,
        # sees either the old or new complete snapshot during an acquire.
        with sqlite3.connect(f'file:{quote(str(path), safe="/")}?mode=ro',
                             uri=True, timeout=0.2) as db:
            db.execute('PRAGMA query_only=ON')
            row = db.execute('SELECT snapshot FROM policy WHERE id=1').fetchone()
            if row is None:
                return False
            return not decode(row[0]).leases
    except (OSError, sqlite3.Error, RegistryError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Private miner start gate')
    parser.add_argument('--registry', type=Path, required=True)
    args = parser.parse_args(argv)
    return 0 if start_permitted(args.registry) else 1


if __name__ == '__main__':
    raise SystemExit(main())
