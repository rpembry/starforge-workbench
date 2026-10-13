"""Read the server database path from a private systemd EnvironmentFile as data."""
import re
import sys
from pathlib import Path


def database_from_env(path: Path) -> Path:
    found = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if not re.match(r'^WB_DATABASE(?:\s|=|$)', stripped):
            continue
        match = re.fullmatch(r'WB_DATABASE=(/[A-Za-z0-9_./+\-]+)', stripped)
        if match is None:
            raise ValueError('WB_DATABASE must be one unquoted absolute path')
        found.append(match.group(1))
    if len(found) != 1:
        raise ValueError('Exactly one WB_DATABASE assignment is required')
    return Path(found[0])


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('Usage: service-database.py SERVICE_ENV')
        print(database_from_env(Path(sys.argv[1])))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f'Cannot determine configured database: {exc}')
