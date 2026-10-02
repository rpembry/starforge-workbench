"""Preview-first, exact desktop-entry restoration of private favorite applications."""
from __future__ import annotations

import argparse
import configparser
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
import uuid

import yaml


CONFIG = Path.home() / '.config/starforge-ai-workbench/favorite-apps.yaml'
DESKTOP_ID = re.compile(r'[A-Za-z0-9_.-]+\.desktop\Z')
KINDS = {'native', 'pwa', 'terminal', 'workbench'}


def _valid_boot_id(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _boot_generation() -> str:
    """A kernel boot identity makes an old launch receipt safely expire after reboot."""
    try:
        value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return str(uuid.UUID(value))
    except (OSError, ValueError):
        raise ValueError('Cannot verify current boot identity') from None


def _exact_pwa_flags(args: list[str], favorite: dict) -> bool:
    profiles = [arg for arg in args if arg == '--profile-directory' or arg.startswith('--profile-directory=')]
    apps = [arg for arg in args if arg == '--app-id' or arg.startswith('--app-id=')]
    return (profiles == ['--profile-directory=' + favorite['profile']] and
            apps == ['--app-id=' + favorite['app_id']])


def _wm_class_tuple(value: str) -> tuple[str, str] | None:
    """wmctrl joins the WM_CLASS instance and class with one unescaped dot."""
    if value.count('.') != 1:
        return None
    instance, class_name = value.split('.')
    if not instance or not class_name or any(c.isspace() for c in value):
        return None
    return instance, class_name


def _pwa_cmdline_identity(raw: bytes, favorite: dict) -> bool | None:
    """Return exact match, definite other app, or ambiguous cmdline evidence."""
    fields = raw.rstrip(b'\0').split(b'\0')
    if not fields or not fields[0]:
        return None
    try:
        if len(fields) == 1:
            # Some Chrome builds expose the entire command as one /proc field.
            # Parse only for an exact positive match; an apparent miss is unsafe.
            args = shlex.split(fields[0].decode('utf-8'))
        else:
            args = [field.decode('utf-8') for field in fields]
    except (UnicodeError, ValueError):
        return None
    profiles = [arg for arg in args if arg.startswith('--profile-directory=')]
    apps = [arg for arg in args if arg.startswith('--app-id=')]
    if '--profile-directory' in args or '--app-id' in args or len(profiles) > 1 or len(apps) > 1:
        return None
    expected_profile = '--profile-directory=' + favorite['profile']
    expected_app = '--app-id=' + favorite['app_id']
    if profiles == [expected_profile] and apps == [expected_app]:
        return True
    if len(fields) == 1 or len(profiles) != len(apps):
        return None
    return False


def _private_file(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Private favorites file must be a regular nonsymlink file')
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Private favorites file must be owned by you with mode 0600')


def load_config(path: Path = CONFIG) -> list[dict]:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError('Favorites config path must be absolute')
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
        raise ValueError('Favorites directory must be owned by you with mode 0700')
    _private_file(path)
    try:
        document = yaml.safe_load(path.read_text())
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ValueError('Cannot read private favorites config') from None
    if not isinstance(document, dict) or set(document) != {'version', 'favorites'} or document['version'] != 1 or not isinstance(document['favorites'], list):
        raise ValueError('Favorites config requires version 1 and a favorites list')
    favorites, names, desktop_ids = [], set(), set()
    for row in document['favorites']:
        if not isinstance(row, dict) or set(row) - {'name', 'desktop_id', 'kind', 'wm_class', 'executable', 'profile', 'app_id'}:
            raise ValueError('Invalid favorite entry')
        required = {'name', 'desktop_id', 'kind', 'wm_class', 'executable'}
        if not required <= set(row) or not isinstance(row['kind'], str) or row['kind'] not in KINDS:
            raise ValueError('Favorite requires exact name, desktop entry, kind, window class, and executable')
        if any(not isinstance(row[key], str) or not row[key] or any(ord(c) < 32 for c in row[key]) for key in required):
            raise ValueError('Favorite identity fields must be nonempty text')
        if not DESKTOP_ID.fullmatch(row['desktop_id']) or not Path(row['executable']).is_absolute():
            raise ValueError('Favorite requires an exact desktop ID and absolute executable identity')
        if '.' in row['wm_class'] or any(c.isspace() for c in row['wm_class']):
            raise ValueError('Window class with a dot or whitespace cannot be verified from wmctrl')
        if row['name'].casefold() in names:
            raise ValueError('Favorite names must be unique')
        names.add(row['name'].casefold())
        if row['desktop_id'] in desktop_ids:
            raise ValueError('Favorite desktop IDs must be unique')
        desktop_ids.add(row['desktop_id'])
        if row['kind'] == 'pwa':
            if not all(isinstance(row.get(key), str) and row[key] and not any(ord(c) < 32 for c in row[key])
                       for key in ('profile', 'app_id')):
                raise ValueError('PWA requires exact browser profile and application ID')
        elif {'profile', 'app_id'} & set(row):
            raise ValueError('Browser profile and application ID apply only to PWAs')
        favorites.append(dict(row))
    return favorites


def desktop_dirs(env=None) -> list[Path]:
    env = os.environ if env is None else env
    home = Path(env.get('XDG_DATA_HOME', str(Path.home() / '.local/share')))
    system = [Path(part) for part in env.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':') if part]
    return [root / 'applications' for root in [home, *system]]


def resolve_entry(favorite: dict, directories: list[Path]) -> dict:
    matches = [directory / favorite['desktop_id'] for directory in directories
               if (directory / favorite['desktop_id']).exists() or (directory / favorite['desktop_id']).is_symlink()]
    if len(matches) != 1:
        raise ValueError('Desktop entry is missing or ambiguous')
    path = matches[0]
    if path.is_symlink() or not path.is_file():
        raise ValueError('Desktop entry is not a regular nonsymlink file')
    try:
        raw = path.read_bytes()
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read_string(raw.decode('utf-8'))
        entry = parser['Desktop Entry']
        if entry.get('Type') != 'Application' or entry.getboolean('Hidden', fallback=False) or not entry.get('Exec'):
            raise ValueError('Desktop entry is not a launchable application')
        if entry.get('StartupWMClass') and entry['StartupWMClass'] != favorite['wm_class']:
            raise ValueError('Desktop window class changed')
        argv = shlex.split(entry['Exec'])
        if not argv:
            raise ValueError('Desktop entry has no executable')
        executable = shutil.which(argv[0]) if not Path(argv[0]).is_absolute() else argv[0]
        if (not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK) or
                Path(executable).resolve() != Path(favorite['executable']).resolve()):
            raise ValueError('Desktop executable identity changed')
        if favorite['kind'] == 'pwa':
            if not _exact_pwa_flags(argv, favorite):
                raise ValueError('PWA profile or application identity changed')
        return {'desktop_id': favorite['desktop_id'], 'path': str(path), 'digest': hashlib.sha256(raw).hexdigest(),
                'executable': str(Path(executable).resolve())}
    except (OSError, UnicodeError, KeyError, configparser.Error, ValueError) as exc:
        raise ValueError(str(exc) or 'Desktop entry cannot be verified') from None


class X11Desktop:
    """Use complete X11 window evidence; unsupported desktop environments stay unknown."""

    def __init__(self, env=None, proc=Path('/proc')):
        self.env = os.environ if env is None else env
        self.proc = Path(proc)

    def observe(self, favorite: dict, entry: dict) -> tuple[str, str]:
        if self.env.get('XDG_SESSION_TYPE') != 'x11' or not self.env.get('DISPLAY') or not shutil.which('wmctrl') or not shutil.which('gtk-launch'):
            return 'unavailable', 'Exact window evidence or desktop launcher unavailable'
        try:
            windows = subprocess.run(['wmctrl', '-lpGx'], capture_output=True, text=True, timeout=3, check=True).stdout
            candidates = []
            relevant_ambiguity = False
            unknown_ambiguity = False
            for line in windows.splitlines():
                parts = line.split(maxsplit=8)
                if len(parts) < 9:
                    unknown_ambiguity = True
                    continue
                wm_identity = _wm_class_tuple(parts[7])
                if wm_identity is None:
                    try:
                        pid = int(parts[2])
                        if pid <= 0:
                            unknown_ambiguity = True
                            continue
                        process = self.proc / str(pid)
                        if process.joinpath('exe').resolve(strict=True) != Path(entry['executable']):
                            continue
                        if favorite['kind'] == 'pwa' and _pwa_cmdline_identity(
                                process.joinpath('cmdline').read_bytes(), favorite) is False:
                            continue
                    except (OSError, ValueError):
                        unknown_ambiguity = True
                        continue
                    relevant_ambiguity = True
                    continue
                if favorite['wm_class'] in wm_identity:
                    candidates.append(parts)
            for parts in candidates:
                pid = int(parts[2])
                if pid <= 0:
                    return 'uncertain', 'Window has no verifiable owner'
                process = self.proc / str(pid)
                if process.joinpath('exe').resolve(strict=True) != Path(entry['executable']):
                    return 'uncertain', 'Window class has a different process owner'
                if favorite['kind'] == 'pwa':
                    if _pwa_cmdline_identity(process.joinpath('cmdline').read_bytes(), favorite) is not True:
                        return 'uncertain', 'PWA window profile or application identity is unverified'
            if relevant_ambiguity:
                return 'uncertain', 'Selected process has an ambiguous window class'
            if candidates:
                return 'present', 'Exact window class and process identity verified'
            if unknown_ambiguity:
                return 'uncertain', 'Window inventory has an unverifiable class or owner'
            # A process without a window may still be starting; never infer absence.
            for process in self.proc.iterdir():
                if process.name.isdigit():
                    try:
                        if process.stat().st_uid != os.getuid():
                            continue
                        if process.joinpath('exe').resolve(strict=True) == Path(entry['executable']):
                            if favorite['kind'] == 'pwa':
                                match = _pwa_cmdline_identity(process.joinpath('cmdline').read_bytes(), favorite)
                                if match is False:
                                    continue
                            return 'uncertain', 'Matching process has no verified window'
                    except FileNotFoundError:
                        continue  # A process exited during inventory.
                    except (OSError, PermissionError):
                        return 'uncertain', 'Process inventory cannot be verified'
            return 'absent', 'Complete X11 inventory has no matching window or process'
        except (OSError, ValueError, subprocess.SubprocessError):
            return 'uncertain', 'Desktop or process inventory cannot be verified'

    def launch(self, desktop_id: str) -> bool:
        try:
            return subprocess.run(['gtk-launch', desktop_id], timeout=8, check=False,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


class FavoriteRestore:
    def __init__(self, config: Path = CONFIG, directories=None, desktop=None, sleeper=time.sleep,
                 generation=_boot_generation):
        self.config = Path(config)
        self.directories = desktop_dirs() if directories is None else list(directories)
        self.desktop = X11Desktop() if desktop is None else desktop
        self.sleeper = sleeper
        self.generation = generation
        self.state_path = self.config.with_name('favorite-app-restore-state.json')
        self.lock_path = self.config.with_name('favorite-app-restore.lock')

    def _state(self) -> dict:
        if self.state_path.is_symlink():
            raise ValueError('Private restore state must not be a symlink')
        if not self.state_path.exists():
            return {}
        _private_file(self.state_path)
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, UnicodeError, ValueError):
            raise ValueError('Cannot read private restore state') from None
        if (not isinstance(state, dict) or set(state) != {'version', 'pending'} or state['version'] != 1 or
                not isinstance(state['pending'], dict) or
                any(not isinstance(k, str) or not isinstance(v, dict) or
                    set(v) != {'digest', 'boot_id'} or
                    not isinstance(v['digest'], str) or not re.fullmatch(r'[0-9a-f]{64}', v['digest']) or
                    not isinstance(v['boot_id'], str) or not _valid_boot_id(v['boot_id'])
                    for k, v in state['pending'].items())):
            raise ValueError('Unversioned or invalid private restore receipts require operator review')
        return state['pending']

    def _save_state(self, state: dict):
        temporary = self.state_path.with_name('.favorite-app-restore-' + str(os.getpid()))
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'version': 1, 'pending': state}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            directory_fd = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _selection(self, names: list[str]):
        favorites = load_config(self.config)
        index = {row['name'].casefold(): row for row in favorites}
        if not names or len(names) != len(set(name.casefold() for name in names)):
            raise ValueError('Select one or more distinct favorite names')
        try:
            selected = [index[name.casefold()] for name in names]
        except KeyError:
            raise ValueError('Selected name is not in the private allowlist') from None
        return selected

    def _identity(self, selected):
        rows = []
        for favorite in selected:
            try:
                entry = resolve_entry(favorite, self.directories)
                rows.append((favorite, entry, None))
            except ValueError as exc:
                rows.append((favorite, None, str(exc)))
        boot_id = self.generation()
        if not _valid_boot_id(boot_id):
            raise ValueError('Cannot verify current boot identity')
        token_data = {'boot_id': boot_id, 'selection': [
            (favorite, (entry['path'], entry['digest']) if entry else error)
            for favorite, entry, error in rows]}
        token = hashlib.sha256(json.dumps(token_data, sort_keys=True).encode()).hexdigest()
        return rows, token, boot_id

    def preview(self, names: list[str]) -> dict:
        rows, token, boot_id = self._identity(self._selection(names))
        pending = self._state()
        items = []
        for favorite, entry, error in rows:
            pending_key = favorite['desktop_id']
            if error:
                action, evidence = 'refuse', error
            else:
                status, evidence = self.desktop.observe(favorite, entry)
                identity = entry['digest']
                if status == 'present':
                    action = 'preserve'
                elif pending_key in pending and pending[pending_key]['digest'] != identity:
                    action, evidence = 'refuse', 'Prior launch identity changed; resolve it before retry'
                elif pending_key in pending and pending[pending_key]['boot_id'] == boot_id:
                    action, evidence = 'skip', 'Prior launch is unresolved; ' + evidence
                elif status == 'absent':
                    action = 'launch'
                    if pending_key in pending:
                        evidence = 'Prior receipt belongs to a different verified boot; ' + evidence
                else:
                    action = 'refuse'
            items.append({'name': favorite['name'], 'kind': favorite['kind'],
                          'desktop_id': favorite['desktop_id'], 'action': action, 'evidence': evidence})
        return {'selection': names, 'token': token, 'items': items}

    def apply(self, names: list[str], token: str) -> dict:
        parent = self.config.parent
        if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
            raise ValueError('Favorites directory must be owned by you with mode 0700')
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_mode & 0o077:
                raise ValueError('Unsafe favorites restore lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            rows, current_token, boot_id = self._identity(self._selection(names))
            if token != current_token:
                raise ValueError('Selection or desktop identity changed; preview again')
            pending = self._state()
            items = []
            for favorite, entry, error in rows:
                name = favorite['name']
                pending_key = favorite['desktop_id']
                if error:
                    items.append({'name': name, 'result': 'refused', 'launch_requested': False,
                                  'launcher_accepted': None, 'evidence': error})
                    continue
                launch_requested = False
                launcher_accepted = None
                try:
                    if resolve_entry(favorite, self.directories) != entry:
                        raise ValueError('Desktop entry changed during apply')
                    status, evidence = self.desktop.observe(favorite, entry)
                    if status == 'present':
                        pending.pop(pending_key, None)
                        self._save_state(pending)
                        result = 'already_present'
                    elif pending_key in pending and pending[pending_key]['digest'] != entry['digest']:
                        result, evidence = 'refused', 'Prior launch identity changed; resolve it before retry'
                    elif pending_key in pending and pending[pending_key]['boot_id'] == boot_id:
                        result, evidence = 'uncertain', 'Prior launch remains unresolved; ' + evidence
                    elif status != 'absent':
                        result = 'refused'
                    else:
                        pending[pending_key] = {'digest': entry['digest'], 'boot_id': boot_id}
                        self._save_state(pending)
                        launch_requested = True
                        requested = self.desktop.launch(entry['desktop_id'])
                        launcher_accepted = requested
                        result = 'launch_requested' if requested else 'uncertain'
                        evidence = 'Desktop launcher accepted request; readiness not yet verified' if requested else 'Desktop launcher result is uncertain'
                        for _ in range(3):
                            observed, detail = self.desktop.observe(favorite, entry)
                            if observed == 'present':
                                pending.pop(pending_key, None)
                                self._save_state(pending)
                                result, evidence = 'verified_ready', detail
                                break
                            self.sleeper(0.2)
                        if result == 'launch_requested':
                            result, evidence = 'uncertain', 'Launch requested; readiness not verified'
                except ValueError as exc:
                    result, evidence = 'refused', str(exc)
                except OSError as exc:
                    result, evidence = 'uncertain', str(exc)
                items.append({'name': name, 'result': result, 'launch_requested': launch_requested,
                              'launcher_accepted': launcher_accepted, 'evidence': evidence})
            return {'selection': names, 'items': items}
        finally:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Preview or manually restore exact private favorite desktop applications')
    parser.add_argument('--config', type=Path, default=CONFIG)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    preview = sub.add_parser('preview')
    preview.add_argument('names', nargs='+')
    apply = sub.add_parser('apply')
    apply.add_argument('--token', required=True)
    apply.add_argument('names', nargs='+')
    args = parser.parse_args(argv)
    try:
        restorer = FavoriteRestore(args.config)
        if args.command == 'list':
            configured = load_config(args.config)
            result = {'items': restorer.preview([item['name'] for item in configured])['items'] if configured else []}
        elif args.command == 'preview':
            result = restorer.preview(args.names)
        else:
            result = restorer.apply(args.names, args.token)
        print(json.dumps(result, indent=2))
    except ValueError as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
