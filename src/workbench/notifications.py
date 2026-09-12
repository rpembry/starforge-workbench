"""Opt-in, read-only attention notifications with private restart-safe state."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
import time
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .client import client

ENDPOINT = 'https://api.pushover.net/1/messages.json'
CATEGORIES = {'approval_needed', 'collector_health', 'agent_without_active_run'}
LABELS = {'approval_needed': 'approval request(s)', 'collector_health': 'collector visibility alert(s)',
          'agent_without_active_run': 'agent action(s) with unknown progress'}


class NotificationError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool = False
    dashboard_url: str | None = None
    api_client_file: str | None = None
    credentials_file: str | None = None
    state_dir: str = '~/.local/state/starforge-workbench-notifier'
    title: str = Field(default='Workbench', min_length=1, max_length=250)
    categories: list[Literal['approval_needed', 'collector_health', 'agent_without_active_run']] = Field(default_factory=lambda: sorted(CATEGORIES))
    include_details: bool = False
    poll_seconds: int = Field(default=30, ge=10, le=3600)
    min_interval_seconds: int = Field(default=300, ge=5, le=86400)
    max_attempts: int = Field(default=3, ge=1, le=5)

    @field_validator('dashboard_url')
    @classmethod
    def dashboard(cls, value):
        if value is not None:
            parsed = urlsplit(value)
            if (len(value) > 512 or parsed.scheme != 'https' or not parsed.hostname or
                    parsed.username or parsed.password or parsed.query or parsed.fragment or
                    any(ord(ch) < 33 for ch in value)):
                raise ValueError('Use a credential-free HTTPS dashboard URL without query or fragment')
        return value


class Entry(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    delivered: bool = False


class DeliveryState(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Literal[1] = 1
    scope: str
    entries: dict[str, Entry] = Field(default_factory=dict)
    attempts: int = Field(default=0, ge=0, le=5)
    not_before: float = Field(default=0, ge=0, allow_inf_nan=False)
    last_snapshot: float = Field(default=0, ge=0, allow_inf_nan=False)
    delivered_at: float | None = None
    blocked: bool = False


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def protected_read(path):
    """Never source credential files as shell code or echo their contents on error."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise NotificationError('unsafe_private_file')
        return stream.read()


def settings(path):
    try:
        return Settings.model_validate_json(protected_read(path))
    except FileNotFoundError:
        return Settings()  # No configuration never enables delivery.
    except (OSError, ValueError):
        raise NotificationError('invalid_notification_config') from None


def pushover_credentials(config):
    if not config.credentials_file:
        raise NotificationError('missing_pushover_credentials')
    try:
        values = {}
        for line in protected_read(Path(config.credentials_file).expanduser()).splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            if '=' not in line:
                raise ValueError()
            key, value = line.split('=', 1)
            key = key.strip()
            if key not in {'PUSHOVER_USER_KEY', 'PUSHOVER_API_TOKEN', 'PUSHOVER_TITLE'} or key in values:
                raise ValueError()
            # dotenv-style unquoted titles may contain spaces; no expansion or execution.
            values[key] = ' '.join(shlex.split(value, comments=True))
        if any(not re.fullmatch(r'[A-Za-z0-9]{30}', values.get(key, ''))
               for key in ('PUSHOVER_USER_KEY', 'PUSHOVER_API_TOKEN')):
            raise ValueError()
        title = values.get('PUSHOVER_TITLE') or config.title
        if len(title) > 250 or any(ord(ch) < 32 for ch in title):
            raise ValueError()
        return values['PUSHOVER_USER_KEY'], values['PUSHOVER_API_TOKEN'], title
    except (OSError, ValueError):
        raise NotificationError('invalid_pushover_credentials') from None


def send_pushover(config, payload, transport=None):
    user, token, title = pushover_credentials(config)
    try:
        # A separate client never forwards Workbench credentials to Pushover.
        with httpx.Client(timeout=15, follow_redirects=False, transport=transport) as delivery:
            response = delivery.post(ENDPOINT, data={**payload, 'user': user, 'token': token, 'title': title})
        if 400 <= response.status_code < 500:
            raise NotificationError('delivery_rejected')
        if response.status_code != 200:
            raise NotificationError('delivery_unavailable', retryable=True)
        result = response.json()
        if not isinstance(result, dict) or type(result.get('status')) is not int or result['status'] != 1:
            raise NotificationError('delivery_rejected')
    except (httpx.HTTPError, ValueError):
        raise NotificationError('delivery_unavailable', retryable=True) from None


def selected(snapshot, config, current):
    try:
        stamp = datetime.fromisoformat(snapshot['generated_at'])
        if stamp.tzinfo is None or not -30 <= current-stamp.timestamp() <= 180:
            raise ValueError()
        if not isinstance(snapshot['items'], list):
            raise ValueError()
        result = {}
        for item in snapshot['items']:
            if not isinstance(item, dict) or not isinstance(item.get('kind'), str):
                raise ValueError()
            if item['kind'] not in CATEGORIES:
                continue
            if any(not isinstance(item.get(k), str) or not item[k] for k in ('id', 'progress', 'reason', 'title')):
                raise ValueError()
            if item['kind'] not in config.categories:
                continue
            key = digest(item['id'])
            if key in result:
                if result[key] != item:
                    raise ValueError()
                continue
            result[key] = item
        return stamp.timestamp(), result
    except (ValueError, KeyError, TypeError, OverflowError):
        raise NotificationError('invalid_or_stale_attention_snapshot') from None


def fingerprint(item, include_details):
    # Evidence timestamps, polling order and heartbeat age never rearm alerts.
    values = {key: item[key] for key in ('kind', 'progress', 'reason')}
    if include_details:
        values['title'] = item['title']
    return digest(values)


def message(items, config):
    if not config.dashboard_url:
        raise NotificationError('dashboard_url_required')
    counts = Counter(item['kind'] for item in items)
    lines = ['Workbench needs attention: '+', '.join(f'{counts[k]} {LABELS[k]}' for k in sorted(counts))+'.',
             'Review the dashboard. Missing visibility or progress is not confirmed task failure.']
    if config.include_details:
        lines.extend('- '+item['title'][:160].replace('\n', ' ') for item in items[:4])
    return dict(message='\n'.join(lines)[:1024], url=config.dashboard_url, url_title='Open Workbench', priority='0')


def scope(config):
    return digest([config.api_client_file, config.dashboard_url])


def read_state(folder, config):
    try:
        state = DeliveryState.model_validate_json(protected_read(folder/'delivery.json'))
        if state.scope != scope(config):
            raise NotificationError('notification_state_source_mismatch')
        if any(not re.fullmatch(r'[a-f0-9]{64}', key) for key in state.entries):
            raise ValueError()
        return state
    except FileNotFoundError:
        return DeliveryState(scope=scope(config))
    except (OSError, ValueError):
        raise NotificationError('invalid_notification_state') from None


@contextmanager
def locked_state(folder):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise NotificationError('unsafe_notification_state_directory')
    fd = os.open(folder/'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise NotificationError('unsafe_notification_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NotificationError('notifier_already_running') from None
        yield
    finally:
        os.close(fd)


def save_state(folder, state):
    fd, name = tempfile.mkstemp(prefix='.delivery-', dir=folder)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(state.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, folder/'delivery.json')
        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def reconcile(state, items, config, stamp):
    if stamp < state.last_snapshot:
        raise NotificationError('attention_snapshot_out_of_order')
    entries = {}
    for key, item in items.items():
        signature = fingerprint(item, config.include_details)
        prior = state.entries.get(key)
        entries[key] = prior if prior and prior.fingerprint == signature else Entry(fingerprint=signature)
    state.entries = entries  # Observed recovery rearms a later episode; API errors do not.
    state.last_snapshot = stamp
    return [item for key, item in items.items() if not entries[key].delivered]


def poll(config, snapshot, sender=None, current=None, preview=False, retry_pending=False):
    if not config.enabled and not preview:
        return {'status': 'disabled'}
    current = time.time() if current is None else current
    stamp, items = selected(snapshot, config, current)
    folder = Path(config.state_dir).expanduser()
    if preview:
        state = read_state(folder, config)
        pending = reconcile(state, items, config, stamp)
        return dict(status='preview', pending=len(pending), blocked=state.blocked,
                    not_before=state.not_before, payload=message(pending, config) if pending else None)
    with locked_state(folder):
        state = read_state(folder, config)
        pending = reconcile(state, items, config, stamp)
        if retry_pending:
            state.blocked, state.attempts = False, 0
        if not pending:
            state.attempts = 0
            save_state(folder, state)
            return {'status': 'quiet', 'pending': 0}
        if state.blocked or state.attempts >= config.max_attempts:
            state.blocked = True
            save_state(folder, state)
            return {'status': 'blocked', 'pending': len(pending)}
        if current < state.not_before:
            save_state(folder, state)
            return {'status': 'backoff', 'pending': len(pending), 'not_before': state.not_before}
        payload = message(pending, config)
        state.attempts += 1
        state.not_before = current+max(config.min_interval_seconds, 30*2**(state.attempts-1))
        # Persist the pending attempt/backoff before HTTP; only success marks delivery.
        save_state(folder, state)
        try:
            (sender or send_pushover)(config, payload)
        except NotificationError as exc:
            state.blocked = not exc.retryable or state.attempts >= config.max_attempts
            save_state(folder, state)
            return dict(status='blocked' if state.blocked else 'retry_pending', pending=len(pending), error=exc.code)
        for entry in state.entries.values():
            entry.delivered = True
        state.attempts, state.delivered_at = 0, current
        state.not_before = current+config.min_interval_seconds
        save_state(folder, state)
        return {'status': 'delivered', 'count': len(pending)}


def fetch_snapshot(config):
    if not config.api_client_file:
        raise NotificationError('api_client_file_required')
    try:
        with client(credentials_file=Path(config.api_client_file).expanduser()) as api:
            response = api.get('/api/attention')
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, OSError, ValueError, KeyError):
        raise NotificationError('attention_api_unavailable') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['once', 'watch', 'preview', 'test'])
    parser.add_argument('--config', type=Path, default=Path.home()/'.config/starforge-workbench/notifications.json')
    parser.add_argument('--snapshot', type=Path, help='Synthetic JSON snapshot for preview only')
    parser.add_argument('--send', action='store_true', help='Explicitly send one test notification (test command only)')
    parser.add_argument('--retry-pending', action='store_true', help='Explicitly unblock corrected delivery settings (once only)')
    args = parser.parse_args(argv)
    if args.snapshot and args.command != 'preview' or args.send and args.command != 'test' or args.retry_pending and args.command != 'once':
        parser.error('Snapshot, send, or retry flag used with the wrong command')
    while True:
        try:
            config = settings(args.config.expanduser())
            if args.command == 'test':
                payload = dict(message='Workbench notification delivery test. No task state was changed.',
                               url=config.dashboard_url, url_title='Open Workbench', priority='0')
                if not config.dashboard_url:
                    raise NotificationError('dashboard_url_required')
                if args.send:
                    send_pushover(config, payload)
                print(json.dumps({'status': 'test_delivered' if args.send else 'test_preview', 'payload': payload}))
                return 0
            if not config.enabled and args.command != 'preview':
                print(json.dumps({'status': 'disabled'}))
                return 0
            snapshot = json.loads(args.snapshot.read_text()) if args.snapshot else fetch_snapshot(config)
            result = poll(config, snapshot, preview=args.command == 'preview', retry_pending=args.retry_pending)
            print(json.dumps(result), flush=True)
        except (NotificationError, OSError, ValueError):
            # Never log response bodies, config input, keys, task data or HTTP exceptions.
            import sys
            exc = sys.exception()
            result = {'status': 'error', 'error': exc.code if isinstance(exc, NotificationError) else 'notification_local_error'}
            print(json.dumps(result), flush=True)
        if args.command != 'watch':
            return 2 if result['status'] in {'error', 'blocked', 'retry_pending'} else 0
        time.sleep(config.poll_seconds if 'config' in locals() else 30)


if __name__ == '__main__':
    raise SystemExit(main())
