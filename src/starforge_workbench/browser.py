"""Workbench-owned Chrome workspace configuration and local launcher."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from collections import Counter
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from urllib.parse import quote, urlparse
from urllib.request import urlopen

import yaml


CONFIG_DIR = Path.home() / '.config' / 'starforge-ai-workbench'
CONFIG_PATH = CONFIG_DIR / 'browser-workspaces.yaml'
DEFAULT_WORKSPACE = 'default'
WORKSPACE_RE = re.compile(r'^[a-z][a-z0-9_-]{0,47}$')
NAME_RE = re.compile(r'^[^\x00\r\n]{1,120}$')
CHROME_NAMES = {'chrome', 'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'}


class ChromeControlUnavailable(ValueError):
    """Chrome is present but its local browser-control endpoint is unavailable."""


class BrowserInputError(ValueError):
    """Invalid workspace name, entry, or URL supplied by a caller."""


class BrowserConflict(ValueError):
    """A named entry already exists."""


class BrowserNotFound(ValueError):
    """A named entry does not exist."""


class BrowserConfigError(ValueError):
    """Local desired-state storage is unsafe or unreadable."""


@dataclass(frozen=True)
class BrowserConnection:
    port: int
    browser_id: str
    profile: Path


def _config_path(path: Path | str | None = None) -> Path:
    result = Path(path).expanduser() if path else Path(os.environ.get('WB_BROWSER_WORKSPACES_FILE', CONFIG_PATH)).expanduser()
    if not result.is_absolute() or result.is_symlink():
        raise BrowserConfigError('Browser workspace config must be an absolute, non-symlink path')
    return result


def _validate_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise BrowserInputError('Workspace URLs must be valid HTTP(S) URLs') from exc
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password:
        raise BrowserInputError('Workspace URLs must be HTTP(S) URLs without embedded credentials')
    return url


def _validate_workspace(name: str) -> str:
    if not isinstance(name, str) or not WORKSPACE_RE.fullmatch(name):
        raise BrowserInputError('Workspace names must use lowercase letters, numbers, hyphens, or underscores')
    return name


def _validate_entry(entry: object) -> dict[str, str]:
    if not isinstance(entry, dict) or set(entry) - {'name', 'url', 'match'} or 'name' not in entry or 'url' not in entry:
        raise BrowserInputError('Each browser entry requires name and url')
    name, url = entry['name'], entry['url']
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise BrowserInputError('Browser entry names must be non-empty text up to 120 characters')
    if not isinstance(url, str):
        raise BrowserInputError('Browser entry URL must be text')
    match = entry.get('match', 'origin')
    if not isinstance(match, str) or match not in {'origin', 'url'}:
        raise BrowserInputError("Browser entry match must be 'origin' or 'url'")
    return {'name': name, 'url': _validate_url(url), 'match': match}


def validate_document(document: object) -> dict[str, object]:
    if document is None:
        document = {'version': 1, 'workspaces': {DEFAULT_WORKSPACE: []}}
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('workspaces'), dict):
        raise BrowserConfigError('Browser workspace config must contain version: 1 and a workspaces mapping')
    workspaces: dict[str, list[dict[str, str]]] = {}
    for workspace, entries in document['workspaces'].items():
        _validate_workspace(workspace)
        if not isinstance(entries, list):
            raise BrowserConfigError(f'Workspace {workspace} must contain a list')
        normalized = [_validate_entry(entry) for entry in entries]
        names = [entry['name'].casefold() for entry in normalized]
        if len(names) != len(set(names)):
            raise BrowserConfigError(f'Workspace {workspace} contains duplicate entry names')
        workspaces[workspace] = normalized
    workspaces.setdefault(DEFAULT_WORKSPACE, [])
    return {'version': 1, 'workspaces': workspaces}


def _private_parent(target: Path, create=False):
    if create:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.is_symlink() or (target.parent.exists() and
        (target.parent.stat().st_uid != os.getuid() or target.parent.stat().st_mode & 0o077)):
        raise BrowserConfigError('Browser workspace config directory must be caller-owned and private')


def load_config(path: Path | str | None = None) -> dict[str, object]:
    target = _config_path(path)
    _private_parent(target)
    if not target.exists():
        return validate_document(None)
    if (target.is_symlink() or target.stat().st_uid != os.getuid() or
        target.stat().st_mode & 0o077 or target.stat().st_nlink != 1):
        raise BrowserConfigError('Browser workspace config must be owned by you with mode 0600')
    try:
        document = yaml.safe_load(target.read_text())
    except (OSError, UnicodeError, yaml.YAMLError):
        raise BrowserConfigError('Unable to read browser workspace config') from None
    try:
        return validate_document(document)
    except (ValueError, TypeError) as exc:
        raise BrowserConfigError('Invalid stored browser workspace config') from exc


def save_config(document: dict[str, object], path: Path | str | None = None) -> dict[str, object]:
    target = _config_path(path)
    document = validate_document(document)
    _private_parent(target, create=True)
    if target.exists() and (target.stat().st_uid != os.getuid() or
                            target.stat().st_mode & 0o077 or target.stat().st_nlink != 1):
        raise BrowserConfigError('Browser workspace config must be owned by you with mode 0600')
    fd, temporary = tempfile.mkstemp(prefix='.' + target.name + '-', dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(yaml.safe_dump(document, sort_keys=False))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return document


@contextmanager
def _edit_lock(path):
    target = _config_path(path)
    _private_parent(target, create=True)
    lock = target.with_name('.' + target.name + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BrowserConfigError('Browser workspace lock must be caller-owned and private')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield target
    finally:
        os.close(fd)


def entries(workspace: str = DEFAULT_WORKSPACE, path: Path | str | None = None) -> list[dict[str, str]]:
    _validate_workspace(workspace)
    return list(load_config(path)['workspaces'].get(workspace, []))


def add_entry(name: str, url: str, workspace: str = DEFAULT_WORKSPACE, match: str = 'origin', path=None) -> dict[str, str]:
    with _edit_lock(path) as target:
        document = load_config(target)
        _validate_workspace(workspace)
        item = _validate_entry({'name': name, 'url': url, 'match': match})
        current = document['workspaces'].setdefault(workspace, [])
        if any(item['name'].casefold() == existing['name'].casefold() for existing in current):
            raise BrowserConflict(f'Entry already exists in workspace {workspace}: {name}')
        current.append(item)
        save_config(document, target)
        return item


def remove_entry(name: str, workspace: str = DEFAULT_WORKSPACE, path=None) -> dict[str, str]:
    with _edit_lock(path) as target:
        _validate_workspace(workspace)
        document = load_config(target)
        current = document['workspaces'].get(workspace, [])
        for index, item in enumerate(current):
            if item['name'].casefold() == name.casefold():
                removed = current.pop(index)
                save_config(document, target)
                return removed
        raise BrowserNotFound(f'No browser entry named {name!r} in workspace {workspace}')


def update_entry(name: str, url: str | None = None, new_name: str | None = None,
                 workspace: str = DEFAULT_WORKSPACE, match: str | None = None, path=None) -> dict[str, str]:
    with _edit_lock(path) as target:
        _validate_workspace(workspace)
        document = load_config(target)
        current = document['workspaces'].get(workspace, [])
        for item in current:
            if item['name'].casefold() == name.casefold():
                candidate = {'name': new_name or item['name'], 'url': url or item['url'], 'match': match or item['match']}
                normalized = _validate_entry(candidate)
                if any(other is not item and other['name'].casefold() == normalized['name'].casefold() for other in current):
                    raise BrowserConflict(f'Entry already exists in workspace {workspace}: {normalized["name"]}')
                item.update(normalized)
                save_config(document, target)
                return item
        raise BrowserNotFound(f'No browser entry named {name!r} in workspace {workspace}')


def _running(proc: Path = Path('/proc')) -> bool:
    try:
        for item in proc.iterdir():
            if not item.name.isdigit():
                continue
            try:
                if (item / 'comm').read_text().strip() in CHROME_NAMES:
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def _focus() -> bool:
    wmctrl = shutil.which('wmctrl')
    if not wmctrl:
        return False
    return subprocess.run([wmctrl, '-xa', 'google-chrome'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _executable() -> str:
    for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
        executable = shutil.which(name)
        if executable:
            return executable
    raise ValueError('No Chrome-compatible executable found; use google-chrome directly after installing it')


def launch(workspace: str = DEFAULT_WORKSPACE, path=None) -> dict[str, object]:
    configured = entries(workspace, path)
    if _running():
        focused = _focus()
        return {'status': 'focused' if focused else 'already_running', 'workspace': workspace, 'opened': []}
    executable = _executable()
    urls = [item['url'] for item in configured]
    subprocess.Popen([executable, *urls], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {'status': 'launched', 'workspace': workspace, 'opened': urls}


def _canonical(url: str, mode: str) -> str:
    parsed = urlparse(url)
    if mode == 'origin':
        return f'{parsed.scheme}://{parsed.netloc.lower()}'
    return url.rstrip('/')


def _json_endpoint(port: int, endpoint: str) -> object:
    try:
        with urlopen(f'http://127.0.0.1:{port}{endpoint}', timeout=2) as response:
            return json.loads(response.read())
    except Exception as exc:
        raise ChromeControlUnavailable(f'Chrome DevTools connection on loopback port {port} is unavailable') from exc


def _listener_inodes(port: int) -> set[str]:
    try:
        lines = Path('/proc/net/tcp').read_text().splitlines()[1:]
    except OSError as exc:
        raise ChromeControlUnavailable('Cannot verify the DevTools listener owner') from exc
    sockets = set()
    for line in lines:
        fields = line.split()
        address, number = fields[1].split(':')
        if int(number, 16) == port and fields[3] == '0A':
            if address != '0100007F':
                raise ChromeControlUnavailable('DevTools listener is not bound to IPv4 loopback only')
            sockets.add(fields[9])
    return sockets


def _chrome_cmdline(raw: bytes) -> list[str]:
    fields = [part.decode(errors='replace') for part in raw.split(b'\0') if part]
    if len(fields) != 1:
        return fields
    # Some Chrome builds expose a rewritten, space-separated process title as one field.
    try:
        return shlex.split(fields[0])
    except ValueError:
        return []


def _profile_from_cmdline(args: list[str], port: int) -> Path | None:
    if not args or Path(args[0]).name not in CHROME_NAMES:
        return None
    port_flags = [arg for arg in args[1:] if arg.startswith('--remote-debugging-port')]
    if port_flags != [f'--remote-debugging-port={port}']:
        return None
    profile_flags = [index for index, arg in enumerate(args[1:], start=1)
                     if arg.startswith('--user-data-dir')]
    if len(profile_flags) != 1:
        return None
    index = profile_flags[0]
    arg = args[index]
    if arg == '--user-data-dir':
        if index + 1 >= len(args):
            return None
        value = args[index + 1]
    elif arg.startswith('--user-data-dir='):
        value = arg.partition('=')[2]
    else:
        return None
    profile = Path(value).expanduser()
    if not profile.is_absolute():
        return None
    return profile.resolve(strict=True)


def _owner_profile(port: int, proc: Path = Path('/proc')) -> Path:
    sockets = _listener_inodes(port)
    if len(sockets) != 1:
        raise ChromeControlUnavailable('No unique verifiable Chrome DevTools listener owns this port')
    owners: list[Path | None] = []
    socket_refs = {f'socket:[{inode}]' for inode in sockets}
    try:
        processes = list(proc.iterdir())
    except OSError as exc:
        raise ChromeControlUnavailable('Cannot inspect the DevTools listener process') from exc
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            owns_listener = False
            for fd in (process / 'fd').iterdir():
                try:
                    if fd.readlink().as_posix() in socket_refs:
                        owns_listener = True
                        break
                except OSError:
                    continue
            if not owns_listener:
                continue
            owners.append(_profile_from_cmdline(_chrome_cmdline((process / 'cmdline').read_bytes()), port))
        except (OSError, ValueError):
            if owns_listener:
                owners.append(None)
            continue
    if len(owners) != 1 or owners[0] is None:
        raise ChromeControlUnavailable('Cannot verify the connected Chrome profile from its listener process')
    return owners[0]


def _connection(port: int, expected_profile: Path | str | None) -> BrowserConnection:
    if expected_profile is None:
        raise ChromeControlUnavailable('Specify --profile or WB_CHROME_PROFILE to identify the intended Chrome profile')
    try:
        expected = Path(expected_profile).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ChromeControlUnavailable('Expected Chrome profile does not exist') from exc
    version = _json_endpoint(port, '/json/version')
    if not isinstance(version, dict) or not isinstance(version.get('webSocketDebuggerUrl'), str):
        raise ChromeControlUnavailable('DevTools endpoint did not identify a browser')
    browser_url = urlparse(version['webSocketDebuggerUrl'])
    try:
        valid_browser_url = (browser_url.scheme == 'ws' and browser_url.hostname in {'127.0.0.1', 'localhost'}
                             and browser_url.port == port and browser_url.path.startswith('/devtools/browser/'))
    except ValueError:
        valid_browser_url = False
    if not valid_browser_url:
        raise ChromeControlUnavailable('DevTools endpoint did not identify the loopback browser')
    actual = _owner_profile(port)
    if actual != expected:
        raise ChromeControlUnavailable(f'DevTools port {port} belongs to a different Chrome profile')
    return BrowserConnection(port, version['webSocketDebuggerUrl'], actual)


def _tabs(connection: BrowserConnection) -> list[dict[str, object]]:
    if _connection(connection.port, connection.profile) != connection:
        raise ChromeControlUnavailable('Connected Chrome browser changed during the operation')
    value = _json_endpoint(connection.port, '/json/list')
    if _connection(connection.port, connection.profile) != connection:
        raise ChromeControlUnavailable('Connected Chrome browser changed while reading tabs')
    if not isinstance(value, list):
        raise ChromeControlUnavailable('Chrome returned an invalid tab list')
    return [item for item in value if isinstance(item, dict) and item.get('type') == 'page'
            and isinstance(item.get('id'), str) and isinstance(item.get('url'), str)]


def control_status(port: int = 9222, profile: Path | str | None = None) -> dict[str, object]:
    """Report whether Workbench's isolated-profile DevTools control is available."""
    try:
        connection = _connection(port, profile)
        return {'status': 'connected', 'transport': 'devtools-port', 'page_count': len(_tabs(connection)),
                'port': port, 'profile': str(connection.profile)}
    except ChromeControlUnavailable as exc:
        return {
            'status': 'unavailable',
            'transport': 'devtools-port',
            'port': port,
            'reason': str(exc),
        }


def _launch_connected(connection: BrowserConnection, flag: str, urls: list[str]) -> None:
    _connection(connection.port, connection.profile)
    subprocess.Popen([_executable(), f'--user-data-dir={connection.profile}', flag, *urls],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _verify_new_tabs(connection: BrowserConnection, before: list[dict[str, object]],
                     urls: list[str], timeout: float = 3.0) -> tuple[list[dict[str, object]], list[str]]:
    old_ids = {tab['id'] for tab in before}
    deadline = time.monotonic() + timeout
    last: list[dict[str, object]] = []
    while True:
        last = [tab for tab in _tabs(connection) if tab['id'] not in old_ids]
        available = Counter(tab['url'] for tab in last)
        if all(available[url] >= count for url, count in Counter(urls).items()):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    by_url: dict[str, list[dict[str, object]]] = {}
    for tab in last:
        by_url.setdefault(tab['url'], []).append(tab)
    verified = []
    partial = []
    for url in urls:
        matches = by_url.get(url, [])
        if matches:
            verified.append(matches.pop(0))
        else:
            partial.append(url)
    return verified, partial


def refresh(workspace: str = DEFAULT_WORKSPACE, port: int = 9222, path=None,
            profile: Path | str | None = None) -> dict[str, object]:
    configured = entries(workspace, path)
    connection = _connection(port, profile)
    tabs = _tabs(connection)
    result: dict[str, object] = {'status': 'verified', 'workspace': workspace, 'existing_tabs': len(tabs),
                                 'requested': [], 'verified': [], 'partial': [], 'uncertain': []}
    for item in configured:
        key = _canonical(item['url'], item['match'])
        if any(_canonical(tab['url'], item['match']) == key for tab in tabs):
            continue
        if any(_canonical(url, item['match']) == key for url in result['requested']):
            continue
        try:
            before = _tabs(connection)
            if any(_canonical(tab['url'], item['match']) == key for tab in before):
                result['verified'].append({'name': item['name'], 'url': item['url'], 'already_present': True})
                tabs = before
                continue
            result['requested'].append(item['url'])
            _launch_connected(connection, '--new-tab', [item['url']])
            verified, partial = _verify_new_tabs(connection, before, [item['url']])
            if verified:
                result['verified'].append({'name': item['name'], 'url': item['url'], 'id': verified[0]['id']})
                tabs = [*before, *verified]
            if partial:
                result['partial'].append({'name': item['name'], 'url': item['url'], 'reason': 'destination_not_verified'})
                break
        except ChromeControlUnavailable as exc:
            result['uncertain'].append({'name': item['name'], 'url': item['url'], 'reason': str(exc)})
            break
    if result['uncertain']:
        result['status'] = 'uncertain'
    elif result['partial']:
        result['status'] = 'partial'
    return result


def organization_preview(workspace: str = DEFAULT_WORKSPACE, port: int = 9222, path=None,
                         profile: Path | str | None = None) -> dict[str, object]:
    """Describe the live tabs matched by one named workspace without changing Chrome."""
    configured = entries(workspace, path)
    selected = []
    connection = _connection(port, profile)
    tabs = _tabs(connection)
    for tab in tabs:
        for entry in configured:
            if _canonical(tab['url'], entry['match']) != _canonical(entry['url'], entry['match']):
                continue
            selected.append({'id': tab.get('id'), 'url': tab['url'], 'entry': entry['name'], 'match': entry['match']})
            break
    token = hashlib.sha256(json.dumps({'browser': connection.browser_id, 'profile': str(connection.profile),
                                       'entries': configured, 'selected': selected}, sort_keys=True).encode()).hexdigest()
    return {
        'status': 'preview',
        'workspace': workspace,
        'selected': selected,
        'unmatched_tabs': len(tabs) - len(selected),
        'expect': token,
    }


def _close_tab(target_id: str, connection: BrowserConnection) -> None:
    if not target_id:
        raise ValueError('Chrome did not supply an ID for a selected tab; no tab was closed')
    try:
        _connection(connection.port, connection.profile)
        with urlopen(f'http://127.0.0.1:{connection.port}/json/close/{quote(target_id, safe="")}', timeout=2) as response:
            response.read()
    except ChromeControlUnavailable:
        raise
    except Exception as exc:
        raise ValueError(f'Chrome could not close selected tab {target_id}') from exc


def organize(workspace: str = DEFAULT_WORKSPACE, port: int = 9222, path=None,
             apply: bool = False, action: str = 'copy', profile: Path | str | None = None,
             expect: str | None = None) -> dict[str, object]:
    """Put matched tabs in a new workspace window, optionally replacing the originals."""
    if action not in {'copy', 'move'}:
        raise ValueError("Organization action must be 'copy' or 'move'")
    preview = organization_preview(workspace, port, path, profile)
    preview['action'] = action
    if not apply:
        return preview
    if not expect or expect != preview['expect']:
        raise ValueError('Chrome target set changed or --expect preview token is missing; preview again before applying')
    selected = preview['selected']
    result = {**preview, 'status': 'verified', 'requested': [item['url'] for item in selected],
              'verified': [], 'partial': [], 'uncertain': [], 'closed': [], 'preserved': []}
    if not selected:
        return result
    connection = _connection(port, profile)
    before = _tabs(connection)
    # The preview is a separate read; check the selected IDs and URLs again immediately before launch.
    current = {tab['id']: tab['url'] for tab in before}
    if any(current.get(item['id']) != item['url'] for item in selected):
        raise ValueError('Selected Chrome tabs changed before apply; preview again')
    urls = result['requested']
    # A separate window is the supported CDP-compatible workspace boundary. Chrome's
    # tab-group API is extension-only, so this CLI deliberately does not emulate it.
    _launch_connected(connection, '--new-window', urls)
    try:
        destinations, _ = _verify_new_tabs(connection, before, urls)
    except ChromeControlUnavailable as exc:
        result['uncertain'] = [{'source_id': item['id'], 'url': item['url'], 'reason': str(exc)} for item in selected]
        result['preserved'] = [item['id'] for item in selected]
        result['status'] = 'uncertain'
        return result
    # Match duplicate URLs one by one; a single new target never justifies closing two sources.
    by_url: dict[str, list[dict[str, object]]] = {}
    for destination in destinations:
        by_url.setdefault(destination['url'], []).append(destination)
    result['verified'] = []
    for source in selected:
        matches = by_url.get(source['url'], [])
        if matches:
            destination = matches.pop(0)
            result['verified'].append({'source_id': source['id'], 'destination_id': destination['id'], 'url': source['url']})
        else:
            result['partial'].append({'source_id': source['id'], 'url': source['url'], 'reason': 'destination_not_verified'})
            result['preserved'].append(source['id'])
    if action == 'move':
        close_attempted = set()
        for match in result['verified']:
            try:
                current = {tab['id']: tab['url'] for tab in _tabs(connection)}
                if current.get(match['source_id']) != match['url'] or current.get(match['destination_id']) != match['url']:
                    result['partial'].append({**match, 'reason': 'source_or_destination_changed'})
                    result['preserved'].append(match['source_id'])
                    continue
                close_attempted.add(match['source_id'])
                _close_tab(match['source_id'], connection)
                remaining = {tab['id'] for tab in _tabs(connection)}
                if match['source_id'] not in remaining:
                    result['closed'].append(match['source_id'])
                else:
                    result['partial'].append({**match, 'reason': 'source_still_open'})
                    result['preserved'].append(match['source_id'])
            except ChromeControlUnavailable as exc:
                result['uncertain'].append({**match, 'reason': str(exc)})
                break
            except ValueError as exc:
                result['uncertain'].append({**match, 'reason': str(exc)})
                break
    elif action == 'copy':
        result['preserved'] = [item['id'] for item in selected]
    if action == 'move':
        result['preserved'] = list(dict.fromkeys([*result['preserved'],
            *(item['id'] for item in selected if item['id'] not in result['closed']
              and item['id'] not in close_attempted)]))
    if result['uncertain']:
        result['status'] = 'uncertain'
    elif result['partial']:
        result['status'] = 'partial'
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='ai-workbench chrome')
    parser.add_argument('--workspace', default=DEFAULT_WORKSPACE)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--profile', type=Path, default=os.environ.get('WB_CHROME_PROFILE'),
                        help='Expected user-data directory of the connected Chrome browser')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('list')
    add = sub.add_parser('add'); add.add_argument('values', nargs='+'); add.add_argument('--match', choices=('origin', 'url'), default='origin')
    remove = sub.add_parser('remove'); remove.add_argument('name')
    update = sub.add_parser('update'); update.add_argument('name'); update.add_argument('--url'); update.add_argument('--name', dest='new_name'); update.add_argument('--match', choices=('origin', 'url'))
    sub.add_parser('refresh').add_argument('--port', type=int, default=int(os.environ.get('WB_CHROME_CDP_PORT', '9222')))
    sub.add_parser('control-status').add_argument('--port', type=int, default=int(os.environ.get('WB_CHROME_CDP_PORT', '9222')))
    organize_parser = sub.add_parser('organize', help='Preview or explicitly organize matching live tabs into a workspace window')
    organize_parser.add_argument('--port', type=int, default=int(os.environ.get('WB_CHROME_CDP_PORT', '9222')))
    organize_parser.add_argument('--apply', action='store_true', help='Open the selected tabs in a new workspace window')
    organize_parser.add_argument('--expect', help='Target-set token returned by organize preview; required with --apply')
    organize_parser.add_argument('--action', choices=('copy', 'move'), default='copy',
                                help='copy preserves originals; move closes a source only after its destination is verified')
    args = parser.parse_args(argv)
    if args.command in {None, 'list'}:
        if args.command is None:
            print(json.dumps(launch(args.workspace, args.config), indent=2))
        else:
            print(json.dumps({'workspace': args.workspace, 'entries': entries(args.workspace, args.config)}, indent=2))
    elif args.command == 'add':
        if len(args.values) == 1:
            url = args.values[0]; name = urlparse(url).netloc or url
        elif len(args.values) == 2:
            name, url = args.values
        else:
            parser.error('add accepts URL or NAME URL')
        print(json.dumps(add_entry(name, url, args.workspace, args.match, args.config), indent=2))
    elif args.command == 'remove':
        print(json.dumps(remove_entry(args.name, args.workspace, args.config), indent=2))
    elif args.command == 'update':
        print(json.dumps(update_entry(args.name, args.url, args.new_name, args.workspace, args.match, args.config), indent=2))
    elif args.command == 'refresh':
        result = refresh(args.workspace, args.port, args.config, args.profile)
        print(json.dumps(result, indent=2))
        return 0 if result['status'] == 'verified' else 2
    elif args.command == 'control-status':
        result = control_status(args.port, args.profile)
        print(json.dumps(result, indent=2))
        return 0 if result['status'] == 'connected' else 2
    elif args.command == 'organize':
        result = organize(args.workspace, args.port, args.config, args.apply, args.action,
                          args.profile, args.expect)
        print(json.dumps(result, indent=2))
        return 0 if result['status'] in {'preview', 'verified'} else 2
    return 0
