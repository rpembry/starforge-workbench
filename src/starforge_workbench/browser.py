"""Workbench-owned Chrome workspace configuration and local launcher."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlparse
from urllib.request import urlopen

import yaml


CONFIG_DIR = Path.home() / '.config' / 'starforge-ai-workbench'
CONFIG_PATH = CONFIG_DIR / 'browser-workspaces.yaml'
DEFAULT_WORKSPACE = 'default'
WORKSPACE_RE = re.compile(r'^[a-z][a-z0-9_-]{0,47}$')
NAME_RE = re.compile(r'^[^\x00\r\n]{1,120}$')
CHROME_NAMES = {'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'}


def _config_path(path: Path | str | None = None) -> Path:
    result = Path(path).expanduser() if path else Path(os.environ.get('WB_BROWSER_WORKSPACES_FILE', CONFIG_PATH)).expanduser()
    if not result.is_absolute() or result.is_symlink():
        raise ValueError('Browser workspace config must be an absolute, non-symlink path')
    return result


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError('Workspace URLs must be HTTP(S) URLs without embedded credentials')
    return url


def _validate_workspace(name: str) -> str:
    if not WORKSPACE_RE.fullmatch(name):
        raise ValueError('Workspace names must use lowercase letters, numbers, hyphens, or underscores')
    return name


def _validate_entry(entry: object) -> dict[str, str]:
    if not isinstance(entry, dict) or set(entry) - {'name', 'url', 'match'} or 'name' not in entry or 'url' not in entry:
        raise ValueError('Each browser entry requires name and url')
    name, url = entry['name'], entry['url']
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError('Browser entry names must be non-empty text up to 120 characters')
    if not isinstance(url, str):
        raise ValueError('Browser entry URL must be text')
    match = entry.get('match', 'origin')
    if match not in {'origin', 'url'}:
        raise ValueError("Browser entry match must be 'origin' or 'url'")
    return {'name': name, 'url': _validate_url(url), 'match': match}


def validate_document(document: object) -> dict[str, object]:
    if document is None:
        document = {'version': 1, 'workspaces': {DEFAULT_WORKSPACE: []}}
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('workspaces'), dict):
        raise ValueError('Browser workspace config must contain version: 1 and a workspaces mapping')
    workspaces: dict[str, list[dict[str, str]]] = {}
    for workspace, entries in document['workspaces'].items():
        _validate_workspace(workspace)
        if not isinstance(entries, list):
            raise ValueError(f'Workspace {workspace} must contain a list')
        normalized = [_validate_entry(entry) for entry in entries]
        names = [entry['name'].casefold() for entry in normalized]
        if len(names) != len(set(names)):
            raise ValueError(f'Workspace {workspace} contains duplicate entry names')
        workspaces[workspace] = normalized
    workspaces.setdefault(DEFAULT_WORKSPACE, [])
    return {'version': 1, 'workspaces': workspaces}


def load_config(path: Path | str | None = None) -> dict[str, object]:
    target = _config_path(path)
    if not target.exists():
        return validate_document(None)
    if target.is_symlink() or target.stat().st_uid != os.getuid() or target.stat().st_mode & 0o077:
        raise ValueError('Browser workspace config must be owned by you with mode 0600')
    try:
        document = yaml.safe_load(target.read_text())
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ValueError('Unable to read browser workspace config') from None
    return validate_document(document)


def save_config(document: dict[str, object], path: Path | str | None = None) -> dict[str, object]:
    target = _config_path(path)
    document = validate_document(document)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.stat().st_mode & 0o077:
        raise ValueError('Browser workspace config directory must be private with mode 0700')
    temporary = target.with_name('.' + target.name + '.tmp')
    temporary.write_text(yaml.safe_dump(document, sort_keys=False), encoding='utf-8')
    temporary.chmod(0o600)
    os.replace(temporary, target)
    return document


def entries(workspace: str = DEFAULT_WORKSPACE, path: Path | str | None = None) -> list[dict[str, str]]:
    _validate_workspace(workspace)
    return list(load_config(path)['workspaces'].get(workspace, []))


def add_entry(name: str, url: str, workspace: str = DEFAULT_WORKSPACE, match: str = 'origin', path=None) -> dict[str, str]:
    document = load_config(path)
    _validate_workspace(workspace)
    item = _validate_entry({'name': name, 'url': url, 'match': match})
    current = document['workspaces'].setdefault(workspace, [])
    if any(item['name'].casefold() == existing['name'].casefold() for existing in current):
        raise ValueError(f'Entry already exists in workspace {workspace}: {name}')
    current.append(item)
    save_config(document, path)
    return item


def remove_entry(name: str, workspace: str = DEFAULT_WORKSPACE, path=None) -> dict[str, str]:
    document = load_config(path)
    current = document['workspaces'].get(workspace, [])
    for index, item in enumerate(current):
        if item['name'].casefold() == name.casefold():
            removed = current.pop(index)
            save_config(document, path)
            return removed
    raise ValueError(f'No browser entry named {name!r} in workspace {workspace}')


def update_entry(name: str, url: str | None = None, new_name: str | None = None,
                 workspace: str = DEFAULT_WORKSPACE, match: str | None = None, path=None) -> dict[str, str]:
    document = load_config(path)
    current = document['workspaces'].get(workspace, [])
    for item in current:
        if item['name'].casefold() == name.casefold():
            candidate = {'name': new_name or item['name'], 'url': url or item['url'], 'match': match or item['match']}
            normalized = _validate_entry(candidate)
            if any(other is not item and other['name'].casefold() == normalized['name'].casefold() for other in current):
                raise ValueError(f'Entry already exists in workspace {workspace}: {normalized["name"]}')
            item.update(normalized)
            save_config(document, path)
            return item
    raise ValueError(f'No browser entry named {name!r} in workspace {workspace}')


def _running() -> bool:
    proc = Path('/proc')
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


def _tabs(port: int) -> list[dict[str, object]]:
    try:
        with urlopen(f'http://127.0.0.1:{port}/json/list', timeout=1) as response:
            value = json.loads(response.read())
    except Exception as exc:
        raise ValueError(f'Chrome refresh requires a local DevTools endpoint on port {port}') from exc
    return [item for item in value if item.get('type') == 'page' and isinstance(item.get('url'), str)]


def refresh(workspace: str = DEFAULT_WORKSPACE, port: int = 9222, path=None) -> dict[str, object]:
    configured = entries(workspace, path)
    tabs = _tabs(port)
    opened = []
    for item in configured:
        key = _canonical(item['url'], item['match'])
        if any(_canonical(tab['url'], item['match']) == key for tab in tabs):
            continue
        executable = _executable()
        subprocess.Popen([executable, '--new-tab', item['url']], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        opened.append(item['name'])
    return {'status': 'refreshed', 'workspace': workspace, 'existing_tabs': len(tabs), 'opened': opened}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='ai-workbench chrome')
    parser.add_argument('--workspace', default=DEFAULT_WORKSPACE)
    parser.add_argument('--config', type=Path)
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('list')
    add = sub.add_parser('add'); add.add_argument('values', nargs='+'); add.add_argument('--match', choices=('origin', 'url'), default='origin')
    remove = sub.add_parser('remove'); remove.add_argument('name')
    update = sub.add_parser('update'); update.add_argument('name'); update.add_argument('--url'); update.add_argument('--name', dest='new_name'); update.add_argument('--match', choices=('origin', 'url'))
    sub.add_parser('refresh').add_argument('--port', type=int, default=int(os.environ.get('WB_CHROME_CDP_PORT', '9222')))
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
        print(json.dumps(refresh(args.workspace, args.port, args.config), indent=2))
    return 0
